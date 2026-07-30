#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""renew.py 里纯计算部分的回归测试。

这些用例锁死几个曾经把整个流程带进沟里的 bug:

  1. Mihomo 的 COMPATIBLE 占位节点被当成真实节点，导致「有健康节点」的
     假象，实际流量从 runner 自己的数据中心 IP 直连出去。
  2. xdotool 点击直接用视口坐标，少加浏览器工具栏高度，每一次点击都
     落在 Turnstile widget 上方 80~140px 处。
  3. 一个解析不了的节点让 mihomo 丢弃整份 196 节点的订阅。

不需要浏览器/网络: seleniumbase 在本地通常没装，import 前先塞个假模块。
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault("seleniumbase", types.SimpleNamespace(SB=object))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import renew  # noqa: E402


def patch_renew(testcase, **attrs):
    """把 renew 模块里的若干属性替换掉，测试结束自动还原"""
    for name, value in attrs.items():
        p = mock.patch.object(renew, name, value)
        p.start()
        testcase.addCleanup(p.stop)


class TestFilterProxyNodes(unittest.TestCase):
    """代理组里的占位节点必须被排除"""

    def test_drops_compatible_placeholder(self):
        # provider 加载失败时，代理组里只剩 mihomo 的 COMPATIBLE 占位符，
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
        # CI 实测就是这一档: 窗口在 (20,20)，工具栏 121px
        self.assertEqual(renew._screen_offset({"sx": 20, "sy": 20, "oh": 900, "ih": 779}),
                         (20, 141))

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

    实测容器: rect={'h': 71, 'w': 308, 'x': 486, 'y': 437}
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


class FakeSubprocess:
    """够用的 subprocess 替身，把 xdotool 的目标坐标记下来

    _xdotool_click 内部自己做视口→屏幕换算，所以测试必须打在它**下面**
    这一层，否则换算逻辑会被 mock 掉，等于没测。
    """

    DEVNULL = -3

    def __init__(self, on_click):
        self.on_click = on_click
        self.target = None

    def run(self, args, **kwargs):
        if args[:2] == ["xdotool", "mousemove"]:
            self.target = (int(args[-2]), int(args[-1]))
        elif args[:2] == ["xdotool", "click"] and self.target:
            self.on_click(*self.target)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


class FakeClock:
    """虚拟时钟: 让等待循环瞬间跑完且结果确定"""

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
        """收到的是屏幕坐标 —— 由 _xdotool_click 真实换算得出"""
        self.clicks.append((x, y))
        cb = self.CHECKBOX
        if self.solvable and cb["x1"] <= x <= cb["x2"] and cb["y1"] <= y <= cb["y2"]:
            self.token = "y" * 40

    def hit_checkbox(self):
        cb = self.CHECKBOX
        return [(x, y) for x, y in self.clicks
                if cb["x1"] <= x <= cb["x2"] and cb["y1"] <= y <= cb["y2"]]


class TestHandleTurnstile(unittest.TestCase):
    """handle_turnstile 端到端（无浏览器），含真实的坐标换算"""

    def _run(self, harness):
        sb = FakeBrowser()
        patch_renew(
            self,
            _js=harness.js,
            _ensure_connected=lambda sb: None,
            _save_artifacts=lambda sb, tag: None,
            _activate_window=lambda: None,
            subprocess=FakeSubprocess(harness.click),
            time=FakeClock(),
        )
        return renew.handle_turnstile(sb), sb

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
        # 2 轮 × 4 个候选偏移，中间重载 1 次
        self.assertEqual(len(harness.clicks), 8)
        self.assertEqual(sb.reloads, 1)

    def test_no_reset_after_final_attempt_of_a_round(self):
        """每轮最后一次点击后紧接着重载/收摊，不必再 reset"""
        harness = TurnstileHarness(solvable=False)
        self._run(harness)
        # 每轮 4 次点击只在前 3 次后 reset → 2 轮共 6 次
        self.assertEqual(harness.resets, 6)


class TestXdotoolClickConvertsCoordinates(unittest.TestCase):
    """_xdotool_click 必须自己完成视口→屏幕换算

    这是防止 bug 复发的关键一层: 换算放在唯一的 OS 边界里，
    任何新调用点都不可能忘记加偏移。
    """

    WININFO = {"sx": 0, "sy": 0, "oh": 900, "ih": 812}

    def _click(self, wininfo, vx, vy):
        got = []
        harness = types.SimpleNamespace(
            js=lambda sb, expr: dict(wininfo) if wininfo else None)
        patch_renew(
            self,
            _js=harness.js,
            _activate_window=lambda: None,
            subprocess=FakeSubprocess(lambda x, y: got.append((x, y))),
            time=FakeClock(),
        )
        renew._xdotool_click(object(), vx, vy)
        return got

    def test_adds_toolbar_offset(self):
        self.assertEqual(self._click(self.WININFO, 507, 472), [(507, 560)])

    def test_uses_fallback_when_wininfo_missing(self):
        # 读不到窗口信息也不能退化成零偏移
        (x, y), = self._click(None, 507, 472)
        self.assertEqual(x, 507)
        self.assertGreater(y, 472, "没有窗口信息时也必须加上兜底的工具栏偏移")


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
        patch_renew(self, _write_sub_file=fake.write, _refresh_provider=fake.refresh)
        return renew._prune_bad_proxies(proxies, **kw)

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
        self.addCleanup(self._restore)

    def _restore(self):
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

    def test_mihomo_log_redacts_by_default(self):
        """抹除必须在 _mihomo_log 这个漏斗里做，不能靠调用点自觉"""
        printed = []
        patch_renew(self, MIHOMO_LOG="/dev/null")
        with mock.patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
            renew._mihomo_log(f"fetch {self.SUB} failed")
        self.assertNotIn("secretvalue123", printed[0])
        self.assertIn("***", printed[0])


class TestPoll(unittest.TestCase):
    """_poll: 统一的「等条件成立」循环"""

    def test_returns_first_truthy_result(self):
        patch_renew(self, time=FakeClock())
        seq = iter([None, None, "ok"])
        self.assertEqual(renew._poll(lambda: next(seq), 10), "ok")

    def test_returns_none_on_timeout(self):
        patch_renew(self, time=FakeClock())
        self.assertIsNone(renew._poll(lambda: None, 3))

    def test_treats_exception_as_not_ready(self):
        patch_renew(self, time=FakeClock())
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("boom")
            return "ok"

        self.assertEqual(renew._poll(flaky, 10), "ok")

    def test_checks_condition_at_least_once(self):
        patch_renew(self, time=FakeClock())
        self.assertEqual(renew._poll(lambda: "ok", 0), "ok")


class TestLoadMihomoNodes(unittest.TestCase):
    """取节点: 轮询等加载结果，且只打一条日志"""

    def _run(self, reads, timeout=5):
        """reads: 每次 get_mihomo_proxies() 的返回值（或要抛的异常）"""
        logs = []
        seq = iter(reads)

        def fake_get():
            v = next(seq)
            if isinstance(v, Exception):
                raise v
            return v

        patch_renew(self, time=FakeClock(), get_mihomo_proxies=fake_get,
                    _mihomo_log=logs.append)
        return renew.load_mihomo_nodes(timeout), logs

    def test_returns_nodes_once_available(self):
        nodes, logs = self._run([(["香港01", "日本02"], [])])
        self.assertEqual(nodes, ["香港01", "日本02"])
        self.assertEqual(len(logs), 1, f"应只打一条日志，实际: {logs}")
        self.assertIn("2 个候选节点", logs[0])

    def test_waits_for_slow_load(self):
        # 前两次代理组还只有占位符，第三次才出真实节点
        nodes, _ = self._run([([], ["COMPATIBLE"]),
                              ([], ["COMPATIBLE"]),
                              (["香港01"], [])])
        self.assertEqual(nodes, ["香港01"])

    def test_reports_placeholders_when_nothing_real(self):
        nodes, logs = self._run([([], ["COMPATIBLE"])] * 10)
        self.assertEqual(nodes, [])
        # 不管轮询多少次，日志只在最后打一条
        self.assertEqual(len(logs), 1, f"应只打一条日志，实际: {logs}")
        self.assertIn("COMPATIBLE", logs[0])

    def test_survives_api_errors(self):
        nodes, logs = self._run([RuntimeError("connection refused")] * 10)
        self.assertEqual(nodes, [])
        self.assertTrue(any("失败" in m for m in logs))


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class TestHealthCheck(unittest.TestCase):
    """整组测速: /group/{组名}/delay 只返回测通的节点

    v1.19 起 provider 里的节点不再单独暴露成 /proxies/{name}，
    逐个测速会 195/195 全 404 —— 这组用例把接口契约钉住。
    """

    def _run(self, response, names):
        logs = []
        captured = {}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs.get("params")
            if isinstance(response, Exception):
                raise response
            return response

        patch_renew(self, _mihomo_log=logs.append,
                    requests=types.SimpleNamespace(
                        get=fake_get,
                        utils=types.SimpleNamespace(quote=lambda s, safe='': s)))
        return renew.test_mihomo_proxies(names), logs, captured

    def test_uses_group_delay_endpoint(self):
        """必须打整组接口，不能回到逐个节点的 /proxies/{name}/delay"""
        _, _, captured = self._run(
            FakeResponse(200, {"n0": 100}), ["n0"])
        self.assertIn(f"/group/{renew.MIHOMO_GROUP}/delay", captured["url"])
        self.assertNotIn("/proxies/", captured["url"])

    def test_keeps_only_healthy_in_input_order(self):
        # 响应里只有测通的节点，没出现的就是不通
        healthy, _, _ = self._run(
            FakeResponse(200, {"n2": 80, "n0": 120}),
            ["n0", "n1", "n2", "n3"])
        self.assertEqual(healthy, ["n0", "n2"])

    def test_ignores_nodes_not_asked_for(self):
        # 组里可能混着 DIRECT 之类，结果必须只包含入参里的节点
        healthy, _, _ = self._run(
            FakeResponse(200, {"DIRECT": 60, "n0": 90}), ["n0"])
        self.assertEqual(healthy, ["n0"])

    def test_reports_non_200_loudly(self):
        # 504 = 全部超时；其它状态码通常意味着接口变了，必须能看出来
        healthy, logs, _ = self._run(
            FakeResponse(404, None, '{"message":"Resource not found"}'), ["n0"])
        self.assertEqual(healthy, [])
        joined = "\n".join(logs)
        self.assertIn("HTTP 404", joined)
        self.assertIn("Resource not found", joined)

    def test_survives_request_exception(self):
        healthy, logs, _ = self._run(RuntimeError("boom"), ["n0"])
        self.assertEqual(healthy, [])
        self.assertTrue(any("测速请求失败" in m for m in logs))

    def test_survives_non_json_body(self):
        healthy, logs, _ = self._run(FakeResponse(200, None, "<html>"), ["n0"])
        self.assertEqual(healthy, [])
        self.assertTrue(any("不是 JSON" in m for m in logs))

    def test_rejects_non_numeric_and_zero_delays(self):
        healthy, _, _ = self._run(
            FakeResponse(200, {"n0": 0, "n1": None, "n2": True, "n3": 50}),
            ["n0", "n1", "n2", "n3"])
        self.assertEqual(healthy, ["n3"])

    def test_logs_delay_range(self):
        _, logs, _ = self._run(
            FakeResponse(200, {"n0": 50, "n1": 250}), ["n0", "n1"])
        self.assertTrue(any("50~250ms" in m for m in logs))

    def test_empty_input_makes_no_request(self):
        healthy, logs, captured = self._run(FakeResponse(200, {}), [])
        self.assertEqual(healthy, [])
        self.assertEqual(captured, {}, "没有节点时不该发请求")


class TestFetchSubscription(unittest.TestCase):
    """订阅下载: 各种「不是 Clash 配置」的情况要给出能看懂的报错"""

    def _fetch(self, status=200, body=b"", headers=None):
        resp = types.SimpleNamespace(
            status_code=status, content=body,
            text=body.decode("utf-8", "replace"),
            headers=headers or {})
        logs = []
        patch_renew(self, _mihomo_log=logs.append,
                    requests=types.SimpleNamespace(get=lambda *a, **k: resp))
        return renew.fetch_subscription("https://sub.example.com/x"), logs

    def test_parses_clash_yaml(self):
        body = b"proxies:\n  - name: HK01\n    type: ss\n"
        proxies, logs = self._fetch(body=body, headers={"content-type": "text/plain"})
        self.assertEqual(proxies, [{"name": "HK01", "type": "ss"}])
        self.assertTrue(any("2 个节点" in m or "1 个节点" in m for m in logs))

    def test_rejects_html_with_actionable_message(self):
        # 实际遇到过: 被限流后返回 308KB 的网页，直接 yaml 解析会报
        # "mapping values are not allowed here: --tab-size-preference: 4"
        body = b"<!DOCTYPE html>\n<html><style>:root{--tab-size-preference: 4;}</style>"
        with self.assertRaises(RuntimeError) as cm:
            self._fetch(body=body, headers={"content-type": "text/html; charset=utf-8"})
        msg = str(cm.exception)
        self.assertIn("HTML", msg)
        self.assertIn("限流", msg)

    def test_detects_html_even_without_content_type(self):
        with self.assertRaises(RuntimeError) as cm:
            self._fetch(body=b"  <html><body>nope</body></html>", headers={})
        self.assertIn("HTML", str(cm.exception))

    def test_reports_http_error_status(self):
        with self.assertRaises(RuntimeError) as cm:
            self._fetch(status=503, body=b'{"error":"expired"}')
        self.assertIn("503", str(cm.exception))

    def test_rejects_base64_v2ray_subscription(self):
        with self.assertRaises(ValueError) as cm:
            self._fetch(body=b"dm1lc3M6Ly9leUp2Iiwi", headers={"content-type": "text/plain"})
        self.assertIn("proxies", str(cm.exception))

    def test_logs_subscription_userinfo(self):
        _, logs = self._fetch(
            body=b"proxies: []\n",
            headers={"content-type": "text/plain",
                     "subscription-userinfo": "upload=0; download=1; total=2; expire=3"})
        self.assertTrue(any("subscription-userinfo" in m for m in logs))

    def test_empty_proxies_list_is_valid_but_empty(self):
        proxies, _ = self._fetch(body=b"proxies: []\n",
                                 headers={"content-type": "text/plain"})
        self.assertEqual(proxies, [])

    def test_logs_top_level_field_sizes(self):
        body = b"proxies:\n  - name: a\n    type: ss\nrules:\n  - MATCH,DIRECT\n"
        _, logs = self._fetch(body=body, headers={"content-type": "text/plain"})
        joined = "\n".join(logs)
        self.assertIn("proxies=1", joined)
        self.assertIn("rules=1", joined)

    def test_field_sizes_explain_a_large_body_with_few_nodes(self):
        """实测过的场景: 405KB 只有 15 个节点，大头是 rules

        这条日志要能让人一眼看出「体积大但节点少」是正常还是异常。
        """
        body = (b"proxies:\n  - name: a\n    type: ss\n"
                b"proxy-groups:\n  - name: G\n    type: select\n"
                b"rules:\n" + b"  - MATCH,DIRECT\n" * 20)
        _, logs = self._fetch(body=body, headers={"content-type": "text/plain"})
        joined = "\n".join(logs)
        self.assertIn("proxies=1", joined)
        self.assertIn("rules=20", joined)
        self.assertIn("proxy-groups=1", joined)


class TestPickNode(unittest.TestCase):
    """节点挑选: 优先复用已验证能过 Turnstile 的出口"""

    POOL = ["n0", "n1", "n2"]

    def test_rotates_when_nothing_known_good(self):
        self.assertEqual(renew._pick_node(self.POOL, 0, 0, None), ("n0", 1, False))
        self.assertEqual(renew._pick_node(self.POOL, 1, 0, None), ("n1", 2, False))

    def test_reuses_known_good_on_first_attempt(self):
        # 复用时不推进轮换下标，免得白白跳过一个还没试过的节点
        self.assertEqual(renew._pick_node(self.POOL, 1, 0, "good"), ("good", 1, True))

    def test_falls_back_to_rotation_on_retry(self):
        # 重试说明复用的那个也不灵了，别死磕
        self.assertEqual(renew._pick_node(self.POOL, 1, 1, "good"), ("n1", 2, False))

    def test_wraps_around_pool(self):
        name, index, _ = renew._pick_node(self.POOL, 3, 0, None)
        self.assertEqual((name, index), ("n0", 4))

    def test_known_good_need_not_be_in_pool(self):
        # 复用的节点来自上一个账号，不要求它还在当前轮换位置上
        name, _, reused = renew._pick_node(self.POOL, 0, 0, "从别处来的节点")
        self.assertEqual(name, "从别处来的节点")
        self.assertTrue(reused)


class TestCookiesPath(unittest.TestCase):
    """cookie 文件按账号隔离 —— 撞名会导致串号登录"""

    def test_distinguishes_same_local_part(self):
        # 只取 @ 前面那段的老做法会让这两个账号共用一个文件
        a = renew._cookies_path("account@example.com")
        b = renew._cookies_path("account@other.com")
        self.assertNotEqual(a, b)

    def test_stable_for_same_email(self):
        self.assertEqual(renew._cookies_path("a@b.com"),
                         renew._cookies_path("a@b.com"))

    def test_case_and_space_insensitive(self):
        self.assertEqual(renew._cookies_path("A@B.com"),
                         renew._cookies_path(" a@b.com "))

    def test_lands_in_cookies_dir_as_json(self):
        p = renew._cookies_path("someone@example.com")
        self.assertEqual(os.path.dirname(p), renew.COOKIES_DIR)
        self.assertTrue(p.endswith(".json"))

    def test_survives_weird_local_part(self):
        # 邮箱本地部分全是点号时也不能生成隐藏文件或空名
        p = renew._cookies_path("...@example.com")
        self.assertTrue(os.path.basename(p).startswith("_"),
                        f"意外的文件名: {os.path.basename(p)}")

    def test_real_accounts_do_not_collide(self):
        paths = {renew._cookies_path(e) for e in
                 ("xiawei.email@gmail.com", "katabump@i20.de5.net")}
        self.assertEqual(len(paths), 2)


class TestVersionConstants(unittest.TestCase):
    """版本号/provider 名只能有一个来源"""

    def test_sub_ua_derives_from_version(self):
        self.assertIn(renew.MIHOMO_VERSION, renew._SUB_UA)

    def test_sub_file_derives_from_provider_name(self):
        self.assertTrue(
            os.path.basename(renew.MIHOMO_SUB_FILE).startswith(renew.MIHOMO_PROVIDER))

    def test_provider_api_uses_provider_name(self):
        self.assertTrue(renew.MIHOMO_PROVIDER_API.endswith(renew.MIHOMO_PROVIDER))


if __name__ == "__main__":
    unittest.main(verbosity=2)
