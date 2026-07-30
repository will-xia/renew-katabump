#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""renew.py 里纯计算部分的回归测试。

这些用例锁死两个曾经把整个流程带进沟里的 bug:

  1. Mihomo 的 COMPATIBLE 占位节点被当成真实节点，导致「有健康节点」的
     假象，实际流量从 runner 自己的数据中心 IP 直连出去。
  2. xdotool 点击直接用视口坐标，少加浏览器工具栏高度，每一次点击都
     落在 Turnstile widget 上方约 88px 处。

不需要浏览器/网络: seleniumbase 在本地通常没装，import 前先塞个假模块。
"""

import os
import sys
import types
import unittest

sys.modules.setdefault("seleniumbase", types.SimpleNamespace(SB=object))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import renew  # noqa: E402


class TestFilterProxyNodes(unittest.TestCase):
    """代理组里的占位节点必须被排除"""

    def test_drops_compatible_placeholder(self):
        # provider 加载失败(503)时，代理组里只剩 mihomo 的 COMPATIBLE 占位符，
        # 它的行为等同 DIRECT。当成真实节点用 = 无代理直连。
        self.assertEqual(renew._filter_proxy_nodes(["COMPATIBLE"]), [])

    def test_drops_builtin_nodes(self):
        nodes = ["DIRECT", "REJECT", "REJECT-DROP", "PASS", "GLOBAL", "COMPATIBLE"]
        self.assertEqual(renew._filter_proxy_nodes(nodes), [])

    def test_drops_group_name_itself(self):
        self.assertEqual(
            renew._filter_proxy_nodes(["MyGroup", "香港01"], group="MyGroup"),
            ["香港01"],
        )

    def test_keeps_real_nodes_in_order(self):
        nodes = ["香港01", "COMPATIBLE", "日本02", "DIRECT", "新加坡03"]
        self.assertEqual(
            renew._filter_proxy_nodes(nodes),
            ["香港01", "日本02", "新加坡03"],
        )


class TestScreenOffset(unittest.TestCase):
    """视口坐标 → 屏幕坐标的偏移量"""

    def test_adds_browser_chrome_height(self):
        # 窗口 1280x900，可视区 812 → 工具栏 + 标签栏共 88px
        self.assertEqual(renew._screen_offset({"sx": 0, "sy": 0, "oh": 900, "ih": 812}),
                         (0, 88))

    def test_adds_window_position(self):
        self.assertEqual(renew._screen_offset({"sx": 12, "sy": 30, "oh": 900, "ih": 812}),
                         (12, 118))

    def test_falls_back_when_wininfo_unavailable(self):
        # 取不到窗口信息时不能退化成 (0,0) —— 那正是原来的 bug
        for bad in (None, {}, {"sx": 0, "sy": 0}, {"oh": 0, "ih": 0}):
            dx, dy = renew._screen_offset(bad)
            self.assertGreater(dy, 0, f"{bad!r} 不该退化成零偏移")

    def test_never_negative_bar(self):
        # ih > oh 属于异常读数，不能算出负偏移把点击推到窗口外
        _, dy = renew._screen_offset({"sx": 0, "sy": 0, "oh": 800, "ih": 900})
        self.assertGreaterEqual(dy, 0)


class TestTurnstileClickPoint(unittest.TestCase):
    """用日志里的真实数值验证点击落点

    实测容器: rect={'x': 486, 'y': 437, 'w': 308, 'h': 71}
    窗口 1280x900，工具栏 88px。
    """

    BOX = {"x": 486, "y": 437, "w": 308, "h": 71}
    WININFO = {"sx": 0, "sy": 0, "oh": 900, "ih": 812}
    # 容器在屏幕坐标系里的范围，写成常量而不是用被测函数算，
    # 否则一旦换算逻辑退化，期望值会跟着一起错，测试就永远不会红。
    SCREEN_RECT = (486, 525, 794, 596)   # = 视口 rect + 88px 工具栏

    def test_click_point_lands_inside_widget(self):
        left, top, right, bottom = self.SCREEN_RECT
        for dx in renew._TS_CHECKBOX_X_OFFSETS:
            px, py = renew._to_screen_point(
                self.WININFO, self.BOX["x"] + dx, self.BOX["y"] + self.BOX["h"] / 2)
            self.assertTrue(left <= px <= right and top <= py <= bottom,
                            f"偏移 {dx}px 的落点 ({px},{py}) 掉在容器 "
                            f"({left},{top})-({right},{bottom}) 外面")

    def test_untranslated_point_misses_widget(self):
        """回归断言: 不换算就是原来的错误行为，落点在 widget 上方"""
        top = self.SCREEN_RECT[1]
        raw_y = self.BOX["y"] + self.BOX["h"] / 2   # 472.5，日志里点的就是 472
        self.assertLess(raw_y, top,
                        "视口坐标本该落在 widget 上方，测试前提有问题")

    def test_checkbox_offset_covers_measured_position(self):
        # 文件里实测的 checkbox 中心约在容器左侧 21px 处，
        # 候选偏移必须把这个位置罩住（旧的 8~18px 全在它左边）
        self.assertTrue(any(18 <= dx <= 32 for dx in renew._TS_CHECKBOX_X_OFFSETS),
                        f"候选偏移 {renew._TS_CHECKBOX_X_OFFSETS} 没覆盖实测的 ~21px")


class FakeMihomo:
    """模拟 mihomo 的 provider 加载行为

    真实行为（已由 CI 日志确认）: 从头扫 proxies，遇到第一个解析不了的
    节点就整份 provider 失败，并在响应体里报那个节点的**下标**:
      {"message":"proxy 34 error: encoding/hex: invalid byte: U+0068 'h'"}
    """

    def __init__(self, bad_names=(), fail_message=None):
        self.bad_names = set(bad_names)
        self.fail_message = fail_message
        self.written = []
        self.refreshes = 0

    def write(self, proxies):
        self.written = list(proxies)

    def refresh(self):
        self.refreshes += 1
        if self.fail_message is not None:
            return 503, self.fail_message
        for i, p in enumerate(self.written):
            if p.get("name") in self.bad_names:
                return 503, '{"message":"proxy %d error: encoding/hex: invalid byte"}' % i
        return 204, ""


class TestPruneBadProxies(unittest.TestCase):
    """坏节点剔除 —— 一个坏节点不该废掉整份订阅"""

    def _proxies(self, n=6):
        return [{"name": f"n{i}", "type": "ss", "password": f"secret{i}"} for i in range(n)]

    def _run(self, fake, proxies, **kw):
        saved = (renew._write_sub_file, renew._refresh_provider)
        renew._write_sub_file, renew._refresh_provider = fake.write, fake.refresh
        try:
            return renew._prune_bad_proxies(proxies, **kw)
        finally:
            renew._write_sub_file, renew._refresh_provider = saved

    def test_no_bad_nodes_drops_nothing(self):
        fake = FakeMihomo()
        kept, dropped = self._run(fake, self._proxies())
        self.assertEqual(len(kept), 6)
        self.assertEqual(dropped, [])
        self.assertEqual(fake.refreshes, 1, "全好的订阅不该反复重载")

    def test_drops_single_bad_node(self):
        fake = FakeMihomo(bad_names={"n2"})
        kept, dropped = self._run(fake, self._proxies())
        self.assertEqual([p["name"] for p in kept], ["n0", "n1", "n3", "n4", "n5"])
        self.assertEqual([p["name"] for p in dropped], ["n2"])

    def test_handles_index_shift_across_multiple_drops(self):
        # 删掉一个之后后面的下标会前移，必须每轮重新读 mihomo 报的下标
        fake = FakeMihomo(bad_names={"n2", "n4"})
        kept, dropped = self._run(fake, self._proxies())
        self.assertEqual([p["name"] for p in kept], ["n0", "n1", "n3", "n5"])
        self.assertEqual(sorted(p["name"] for p in dropped), ["n2", "n4"])

    def test_drops_first_and_last(self):
        fake = FakeMihomo(bad_names={"n0", "n5"})
        kept, dropped = self._run(fake, self._proxies())
        self.assertEqual([p["name"] for p in kept], ["n1", "n2", "n3", "n4"])
        self.assertEqual(sorted(p["name"] for p in dropped), ["n0", "n5"])

    def test_stops_on_unrelated_error(self):
        # 不是坏节点的问题（比如文件读不到），不该盲删节点
        fake = FakeMihomo(fail_message='{"message":"open ./sub1.yaml: no such file"}')
        kept, dropped = self._run(fake, self._proxies())
        self.assertEqual(len(kept), 6, "非坏节点错误不该删掉任何节点")
        self.assertEqual(dropped, [])

    def test_stops_on_out_of_range_index(self):
        fake = FakeMihomo(fail_message='{"message":"proxy 999 error: boom"}')
        kept, dropped = self._run(fake, self._proxies())
        self.assertEqual(len(kept), 6)
        self.assertEqual(dropped, [])

    def test_respects_max_drops(self):
        # 所有节点都坏时不能无限删下去
        fake = FakeMihomo(bad_names={f"n{i}" for i in range(6)})
        kept, dropped = self._run(fake, self._proxies(), max_drops=3)
        self.assertEqual(len(dropped), 3)
        self.assertEqual(len(kept), 3)

    def test_writes_file_before_each_reload(self):
        fake = FakeMihomo(bad_names={"n2"})
        self._run(fake, self._proxies())
        # 最后写进去的必须是剔除后的列表
        self.assertNotIn("n2", [p["name"] for p in fake.written])


class TestDescribeProxy(unittest.TestCase):
    """节点描述只能给字段名，绝不能带字段值（里面是密码）"""

    def test_omits_field_values(self):
        out = renew._describe_proxy(
            {"name": "HK01", "type": "ss", "password": "supersecret123",
             "server": "1.2.3.4", "uuid": "abcd-efgh"})
        self.assertNotIn("supersecret123", out)
        self.assertNotIn("1.2.3.4", out)
        self.assertNotIn("abcd-efgh", out)
        # 字段名要保留，否则诊断没意义
        self.assertIn("password", out)
        self.assertIn("uuid", out)
        self.assertIn("HK01", out)
        self.assertIn("ss", out)

    def test_survives_non_dict(self):
        self.assertIn("非字典", renew._describe_proxy("garbage"))


class TestRedaction(unittest.TestCase):
    """凭据抹除 —— 诊断输出会进公开 CI 日志，这里错了就是泄密"""

    SUB = "https://sub.example.com/link/abc123def456xyz?token=secretvalue123&flag=1"

    def setUp(self):
        self._saved = set(renew._SECRETS)
        renew._SECRETS.clear()
        renew._register_secret(self.SUB)

    def tearDown(self):
        renew._SECRETS.clear()
        renew._SECRETS.update(self._saved)

    def test_redacts_full_url(self):
        out = renew._redact(f"failed to fetch {self.SUB}: timeout")
        self.assertNotIn("secretvalue123", out)
        self.assertNotIn("abc123def456xyz", out)
        self.assertIn("***", out)

    def test_redacts_token_alone(self):
        # mihomo 的报错里可能只带 token 那一段
        self.assertNotIn("secretvalue123", renew._redact("bad token secretvalue123"))

    def test_redacts_path_segment_alone(self):
        self.assertNotIn("abc123def456xyz", renew._redact("path=/link/abc123def456xyz"))

    def test_redacts_host(self):
        self.assertNotIn("sub.example.com", renew._redact("dial tcp sub.example.com:443"))

    def test_redacts_url_encoded_form(self):
        import urllib.parse
        quoted = urllib.parse.quote(self.SUB, safe="")
        self.assertNotIn("secretvalue123", renew._redact(f"url={quoted}"))

    def test_keeps_unrelated_text(self):
        msg = "HTTP 503, 0 字节, content-type=text/html"
        self.assertEqual(renew._redact(msg), msg)

    def test_handles_none_and_exceptions(self):
        self.assertEqual(renew._redact(None), "")
        self.assertNotIn("secretvalue123",
                         renew._redact(RuntimeError(f"connect {self.SUB} refused")))

    def test_ignores_too_short_values(self):
        renew._SECRETS.clear()
        renew._register_secret("abc")          # 太短，登记会被忽略
        self.assertEqual(renew._redact("abc def"), "abc def")


class FakeClock:
    """虚拟时钟: 让 handle_turnstile 的等待循环瞬间跑完且结果确定"""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def strftime(self, *args):
        return "00:00:00"


class FakeBrowser:
    """够用的 sb 替身。

    刻意不提供 uc_gui_click_cf / uc_gui_click_captcha:
    handle_turnstile 若还在调它们，这里会直接 AttributeError。
    """

    def __init__(self):
        self.reloads = 0

    def get_current_url(self):
        return "https://dashboard.katabump.com/auth/login"

    def uc_open_with_reconnect(self, url, reconnect_time=None):
        self.reloads += 1


class TurnstileHarness:
    """模拟一个「只有点在 checkbox 上才会通过」的 Turnstile

    数值取自实际失败日志: 容器 rect x=486 y=437 w=308 h=71，窗口 1280x900。
    """

    BOX = {"x": 486, "y": 437, "w": 308, "h": 71, "vw": 1280, "vh": 900}
    WININFO = {"sx": 0, "sy": 0, "oh": 900, "ih": 812}
    # checkbox 在屏幕坐标系里的真实范围（已含 88px 工具栏偏移）
    CHECKBOX = {"x1": 499, "y1": 550, "x2": 515, "y2": 570}

    def __init__(self, auto_pass=False, solvable=True):
        self.token = "x" * 40 if auto_pass else ""
        self.solvable = solvable
        self.clicks = []
        self.resets = 0

    def js(self, sb, expression):
        expr = expression.strip()
        if expr == renew._TS_BOX_JS.strip():
            return dict(self.BOX)
        if expr == renew._SOLVED_JS.strip():
            return len(self.token) > 20
        if expr == renew._TOKEN_LEN_JS.strip():
            return len(self.token)
        if expr == renew._WININFO_JS.strip():
            return dict(self.WININFO)
        if "turnstile.reset" in expr:
            self.resets += 1
            self.token = ""
            return "reset"
        if expr == renew._TS_DIAG_JS.strip():
            return {"title": "Log in", "has_container": True, "token_len": 0}
        raise AssertionError(f"未预期的 JS 调用: {expr[:60]}")

    def click(self, x, y):
        self.clicks.append((x, y))
        cb = self.CHECKBOX
        if self.solvable and cb["x1"] <= x <= cb["x2"] and cb["y1"] <= y <= cb["y2"]:
            self.token = "y" * 40

    def hit_checkbox(self):
        cb = self.CHECKBOX
        return [(x, y) for x, y in self.clicks
                if cb["x1"] <= x <= cb["x2"] and cb["y1"] <= y <= cb["y2"]]


class TestHandleTurnstile(unittest.TestCase):
    """handle_turnstile 端到端（无浏览器）"""

    def _run(self, harness):
        clock = FakeClock()
        sb = FakeBrowser()
        patches = {
            "_js": harness.js,
            "_xdotool_click": harness.click,
            "_ensure_connected": lambda sb: None,
            "_save_artifacts": lambda sb, tag: None,
            "time": clock,
        }
        saved = {k: getattr(renew, k) for k in patches}
        for k, v in patches.items():
            setattr(renew, k, v)
        try:
            return renew.handle_turnstile(sb), sb
        finally:
            for k, v in saved.items():
                setattr(renew, k, v)

    def test_clicks_land_on_checkbox_and_pass(self):
        harness = TurnstileHarness()
        ok, _ = self._run(harness)
        self.assertTrue(ok, "点击落点正确时应当通过")
        self.assertTrue(harness.hit_checkbox(),
                        f"没有一次点击命中 checkbox，落点: {harness.clicks}")

    def test_first_click_is_already_correct(self):
        """第一次就该命中，不该靠后面的偏移候选去凑"""
        harness = TurnstileHarness()
        self._run(harness)
        self.assertEqual(len(harness.clicks), 1,
                         f"第一次点击就该成功，实际点了 {len(harness.clicks)} 次")

    def test_auto_pass_skips_clicking(self):
        harness = TurnstileHarness(auto_pass=True)
        ok, _ = self._run(harness)
        self.assertTrue(ok)
        self.assertEqual(harness.clicks, [], "已自动通过时不该再点")

    def test_gives_up_after_two_rounds(self):
        harness = TurnstileHarness(solvable=False)
        ok, sb = self._run(harness)
        self.assertFalse(ok)
        # 2 轮 × 4 次点击，中间重载 1 次
        self.assertEqual(len(harness.clicks), 8)
        self.assertEqual(sb.reloads, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
