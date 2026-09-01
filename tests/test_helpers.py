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

import contextlib
import io
import os
import sys
import tempfile
import time
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
    """够用的 subprocess 替身，模拟一台 X 服务器

    _xdotool_click / _calibrate_screen_offset 内部自己做视口→屏幕换算，
    所以测试必须打在它们**下面**这一层，否则换算逻辑会被 mock 掉，
    等于没测。

    window 为 None 时 `xdotool search` 返回空 —— 对应「找不到浏览器窗口」，
    校准只能退回推算偏移。
    """

    DEVNULL = -3
    WID = "12345"

    def __init__(self, on_click, window=None, on_move=None, exit_code=0):
        self.on_click = on_click
        self.on_move = on_move
        self.window = window
        self.exit_code = exit_code
        self.target = None
        self.commands = []

    def run(self, args, **kwargs):
        self.commands.append(list(args))
        out = ""
        if args[:2] == ["xdotool", "mousemove"]:
            self.target = (int(args[-2]), int(args[-1]))
            if self.on_move:
                self.on_move(*self.target)
        elif args[:2] == ["xdotool", "click"] and self.target:
            self.on_click(*self.target)
        elif args[1:2] == ["search"] and self.window:
            out = self.WID + "\n"
        elif args[1:2] == ["getwindowgeometry"] and self.window:
            w = self.window
            out = (f"WINDOW={self.WID}\nX={w['x']}\nY={w['y']}\n"
                   f"WIDTH={w['w']}\nHEIGHT={w['h']}\n")
        return types.SimpleNamespace(
            returncode=self.exit_code, stdout=out, stderr="")


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

    deliver=False 复现 run 33451554684 的真实故障: 坐标算得分毫不差，
    但 xdotool 的事件根本没进到浏览器窗口 —— 指针探针读不到任何东西，
    widget 也全程不动。
    """

    BOX = {"x": 486, "y": 437, "w": 308, "h": 71, "vw": 1280, "vh": 812}
    WININFO = {"sx": 0, "sy": 0, "oh": 900, "ih": 812}
    # 窗口在屏幕上的真实几何 + 由此得到的真实「视口→屏幕」偏移
    WINDOW = {"x": 0, "y": 0, "w": 1280, "h": 900}
    OFFSET = (0, 88)
    # checkbox 在屏幕坐标系里的真实范围（已含 88px 工具栏偏移）
    CHECKBOX = {"x1": 499, "y1": 550, "x2": 515, "y2": 570}

    def __init__(self, auto_pass=False, solvable=True, deliver=True):
        self.token = "x" * 40 if auto_pass else ""
        self.solvable = solvable
        self.deliver = deliver
        self.clicks = []
        self.resets = 0
        self.probe = None

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
        if expr == renew._MOUSE_PROBE_JS.strip():
            self.probe = None
            return True
        if expr == renew._MOUSE_PROBE_READ_JS.strip():
            return dict(self.probe) if self.probe else None
        if "turnstile.reset" in expr:
            self.resets += 1
            self.token = ""
            return "reset"
        if expr == renew._TS_DIAG_JS.strip():
            return {"title": "Log in", "has_container": True, "token_len": 0}
        raise AssertionError(f"未预期的 JS 调用: {expr[:60]}")

    def move(self, x, y):
        """指针移到屏幕点 (x, y)，顶层文档据此记下 clientX/clientY"""
        if not self.deliver:
            return
        dx, dy = self.OFFSET
        cx, cy = x - dx, y - dy
        if not (0 <= cx < self.BOX["vw"] and 0 <= cy < self.BOX["vh"]):
            return                      # 落在视口外，页面收不到
        b = self.BOX
        if (b["x"] <= cx <= b["x"] + b["w"]
                and b["y"] <= cy <= b["y"] + b["h"]):
            return                      # widget 是跨域 iframe，事件不冒泡到顶层
        self.probe = {"cx": cx, "cy": cy}

    def click(self, x, y):
        """收到的是屏幕坐标 —— 由 _xdotool_click 真实换算得出"""
        if not self.deliver:
            return
        self.clicks.append((x, y))
        cb = self.CHECKBOX
        if self.solvable and cb["x1"] <= x <= cb["x2"] and cb["y1"] <= y <= cb["y2"]:
            self.token = "y" * 40

    def hit_checkbox(self):
        cb = self.CHECKBOX
        return [(x, y) for x, y in self.clicks
                if cb["x1"] <= x <= cb["x2"] and cb["y1"] <= y <= cb["y2"]]


class TestHandleTurnstile(unittest.TestCase):
    """handle_turnstile 端到端（无浏览器），含真实的坐标换算与指针校准"""

    def _run(self, harness, on_reload=None, cdp=None):
        sb = FakeBrowser()
        patch_renew(
            self,
            _js=harness.js,
            _ensure_connected=lambda sb: None,
            _save_artifacts=lambda sb, tag: None,
            _CALIBRATED_OFFSET=None,
            subprocess=FakeSubprocess(
                harness.click, window=harness.WINDOW, on_move=harness.move),
            time=FakeClock(),
            **({"_cdp_click": cdp} if cdp else {}),
        )
        return renew.handle_turnstile(sb, on_reload=on_reload), sb

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

    def test_calibration_measures_the_real_offset(self):
        """偏移必须是量出来的，而且要量准"""
        harness = TurnstileHarness()
        self._run(harness)
        self.assertEqual(renew._CALIBRATED_OFFSET, harness.OFFSET)

    def test_calibration_avoids_sampling_on_the_widget(self):
        """取样点落在 widget 上就永远读不到数 —— widget 是跨域 iframe"""
        harness = TurnstileHarness()
        self._run(harness)
        self.assertIsNotNone(renew._CALIBRATED_OFFSET,
                             "取样点选在了 widget 上，顶层文档收不到事件")

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

    def test_reload_refills_the_form(self):
        """重载会清空邮箱/密码，必须靠 on_reload 补回来

        少了这一步，就算第 2 轮把 Turnstile 过了，紧接着提交的也是
        一份空表单 —— run 33451554684 的截图正是这样。
        """
        harness = TurnstileHarness(solvable=False)
        refills = []
        _, sb = self._run(harness, on_reload=lambda: refills.append(1))
        self.assertEqual(sb.reloads, 1)
        self.assertEqual(len(refills), 1, "重载后没有重新填表单")

    def test_undeliverable_clicks_fall_back_to_cdp(self):
        """OS 点击进不了浏览器时，别再对着空气点满 8 次"""
        harness = TurnstileHarness(solvable=False, deliver=False)
        cdp_calls = []
        ok, _ = self._run(
            harness, cdp=lambda sb, x, y: cdp_calls.append((x, y)))
        self.assertFalse(ok)
        self.assertIsNone(renew._CALIBRATED_OFFSET,
                          "事件送不达时不该报出一个「实测」偏移")
        self.assertTrue(cdp_calls, "校准失败后没有退回 CDP 兜底")
        self.assertEqual(harness.clicks, [])


class TestXdotoolClickConvertsCoordinates(unittest.TestCase):
    """_xdotool_click 必须自己完成视口→屏幕换算

    这是防止 bug 复发的关键一层: 换算放在唯一的 OS 边界里，
    任何新调用点都不可能忘记加偏移。
    """

    WININFO = {"sx": 0, "sy": 0, "oh": 900, "ih": 812}

    def _click(self, wininfo, vx, vy, calibrated=None):
        got = []
        harness = types.SimpleNamespace(
            js=lambda sb, expr: dict(wininfo) if wininfo else None)
        patch_renew(
            self,
            _js=harness.js,
            _activate_window=lambda: None,
            _CALIBRATED_OFFSET=calibrated,
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

    def test_measured_offset_beats_the_estimate(self):
        """实测偏移在手时就不该再用推算值 —— 推算值可能整个是错的"""
        self.assertEqual(
            self._click(self.WININFO, 507, 472, calibrated=(20, 141)),
            [(527, 613)])


class TestXdotoolReportsFailure(unittest.TestCase):
    """xdotool 失败必须出声

    原来 stderr 全进 DEVNULL 且不看返回码，DISPLAY 不对时 32 次点击
    一次都没落地，日志却一行「异常」都没有。
    """

    def _run(self, exit_code):
        patch_renew(
            self,
            subprocess=FakeSubprocess(lambda x, y: None, exit_code=exit_code),
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok, _ = renew._xdotool("mousemove", "--sync", 1, 2)
        return ok, buf.getvalue()

    def test_nonzero_exit_is_reported(self):
        ok, printed = self._run(1)
        self.assertFalse(ok)
        self.assertIn("失败", printed, "xdotool 失败却没有任何输出")
        self.assertIn("mousemove", printed, "没说清是哪条命令失败")

    def test_success_is_quiet(self):
        ok, printed = self._run(0)
        self.assertTrue(ok)
        self.assertEqual(printed, "")


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


class TestNodeStats(unittest.TestCase):
    """跨运行的节点战绩: 记住谁过过 Turnstile，下次优先用

    cookie 总会过期，过期那次就得重新从几百个节点里摸一个能过的。
    run 33451554684 连挂 5 个节点就是这么来的。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patch_renew(self, STATE_DIR=self.dir,
                    NODE_STATS_PATH=os.path.join(self.dir, "good_nodes.json"))

    def test_records_and_prefers_a_winner(self):
        renew.record_node_result("DE_5", True)
        self.assertEqual(renew.prefer_known_good_nodes(["BE_1", "DE_5", "US_1"]),
                         ["DE_5", "BE_1", "US_1"])

    def test_most_recent_winner_goes_first(self):
        clock = FakeClock()       # last_ok 是秒级时间戳，别为此真睡一秒
        patch_renew(self, time=clock)
        renew.record_node_result("old", True)
        clock.sleep(60)
        renew.record_node_result("new", True)
        self.assertEqual(
            renew.prefer_known_good_nodes(["a", "old", "b", "new"])[:2],
            ["new", "old"])

    def test_net_losing_node_is_not_promoted(self):
        renew.record_node_result("flaky", True)
        renew.record_node_result("flaky", False)
        # 1 胜 1 负算不上「过得去」，不该占队首
        self.assertEqual(renew.prefer_known_good_nodes(["a", "flaky"]),
                         ["a", "flaky"])

    def test_forgets_a_node_that_keeps_losing(self):
        for _ in range(renew._NODE_FORGET_AFTER):
            renew.record_node_result("dead", False)
        self.assertNotIn("dead", renew._load_node_stats())

    def test_only_promotes_nodes_still_in_the_pool(self):
        """订阅里节点会增减，记住的名字不一定还在、更不一定还测得通"""
        renew.record_node_result("gone", True)
        self.assertEqual(renew.prefer_known_good_nodes(["a", "b"]), ["a", "b"])

    def test_no_history_changes_nothing(self):
        pool = ["a", "b", "c"]
        self.assertEqual(renew.prefer_known_good_nodes(pool), pool)

    def test_survives_a_corrupt_file(self):
        with open(renew.NODE_STATS_PATH, "w") as f:
            f.write("{ not json")
        self.assertEqual(renew.prefer_known_good_nodes(["a"]), ["a"])
        renew.record_node_result("a", True)     # 坏文件也不该让记录失败
        self.assertIn("a", renew._load_node_stats())

    def test_file_is_capped(self):
        for i in range(renew._NODE_STATS_MAX + 15):
            renew.record_node_result(f"n{i}", True)
        self.assertLessEqual(len(renew._load_node_stats()), renew._NODE_STATS_MAX)

    def test_unnamed_node_is_ignored(self):
        renew.record_node_result(None, True)    # 不走代理时 node_name 是 None
        self.assertEqual(renew._load_node_stats(), {})

    def test_survives_unwritable_state_dir(self):
        patch_renew(self, NODE_STATS_PATH="/proc/nope/good_nodes.json",
                    STATE_DIR="/proc/nope")
        renew.record_node_result("a", True)     # 落盘失败不该中断整个流程


class TestRetryBudget(unittest.TestCase):
    """换节点重试的时间闸门

    多试几个节点是过 Turnstile 唯一有效的手段，但不能试到被 workflow 的
    硬超时砍掉 —— 那样汇总、TG 通知、cookie 和战绩的保存全都不会发生。
    """

    def test_plenty_of_time_left(self):
        now = 1000.0
        deadline = now + renew.ATTEMPT_TIME_BUDGET * 3
        self.assertFalse(renew.retry_budget_exhausted(deadline, now))

    def test_stops_when_one_attempt_would_not_fit(self):
        now = 1000.0
        deadline = now + renew.ATTEMPT_TIME_BUDGET - 1
        self.assertTrue(renew.retry_budget_exhausted(deadline, now))

    def test_past_deadline(self):
        self.assertTrue(renew.retry_budget_exhausted(500.0, 1000.0))

    def test_no_deadline_means_no_limit(self):
        # 本地跑不设预算，不该被这道闸门拦住
        for empty in (0, None):
            self.assertFalse(renew.retry_budget_exhausted(empty, 1e12))

    def test_attempt_cap_uses_the_job_budget(self):
        """上限得留得下不止几次尝试，否则这道闸门根本没机会生效"""
        self.assertGreaterEqual(
            renew.JOB_TIME_BUDGET,
            renew.ATTEMPT_TIME_BUDGET * 2,
            "job 预算连两次尝试都放不下")
        self.assertGreater(renew.MAX_LOGIN_ATTEMPTS, 5,
                           "5 次尝试只用掉 90 分钟预算里的一小半")


class FakeExits:
    """按节点给出「连查两次出口 IP」的结果，用来模拟落地轮换

    ips[节点] 给一个序列: 每查一次弹一个，最后一个留着重复用。
    """

    def __init__(self, ips, switch_fail=()):
        self.ips = {name: list(seq) for name, seq in ips.items()}
        self.switch_fail = set(switch_fail)
        self.active = None
        self.switched = []
        self.queries = 0

    def switch(self, name):
        if name in self.switch_fail:
            return False
        self.active = name
        self.switched.append(name)
        return True

    def check(self, proxy=None, timeout=None):
        self.queries += 1
        seq = self.ips.get(self.active) or ["(查询失败: 没配)"]
        return seq.pop(0) if len(seq) > 1 else seq[0]


class TestSortByExitStability(unittest.TestCase):
    """落地会漂的节点要排到队尾。

    katabump 的会话认出口 IP: 每条连接都换落地的节点能测速通过、也能登录，
    却会在点 'See' 进详情页那一下把会话丢掉（表现成「找不到 Renew 按钮」）。
    测速只问通不通，问不出这件事。
    """

    PROXY = "http://127.0.0.1:7890"

    def _install(self, ips, switch_fail=()):
        fake = FakeExits(ips, switch_fail)
        patch_renew(self, switch_mihomo_proxy=fake.switch,
                    _check_exit_ip=fake.check,
                    time=mock.MagicMock())  # 不要真的 sleep
        return fake

    def test_drifting_node_goes_last(self):
        self._install({"a": ["1.1.1.1"], "b": ["5.5.5.5", "9.9.9.9"]})
        # 没抽检过的 c/d 排在已知会漂的 b 前面: 未知好过已知会漂
        self.assertEqual(
            renew.sort_by_exit_stability(["a", "b", "c", "d"], self.PROXY, sample=2),
            ["a", "c", "d", "b"])

    def test_stable_pool_keeps_order(self):
        self._install({"a": ["1.1.1.1"], "b": ["2.2.2.2"]})
        self.assertEqual(
            renew.sort_by_exit_stability(["a", "b"], self.PROXY, sample=2),
            ["a", "b"])

    def test_unqueryable_ip_counts_as_stable(self):
        # 抽检自己失败说明不了节点有问题，不能凭这个把测速通过的节点降权
        self._install({"a": ["(查询失败: timeout)"], "b": ["2.2.2.2"]})
        self.assertEqual(
            renew.sort_by_exit_stability(["a", "b"], self.PROXY, sample=2),
            ["a", "b"])

    def test_gives_up_after_first_failed_query(self):
        # 第一次就查不到时不该再等第二轮超时
        fake = self._install({"a": ["(查询失败: timeout)"]})
        renew.sort_by_exit_stability(["a", "b"], self.PROXY, sample=1)
        self.assertEqual(fake.queries, 1)

    def test_switch_failure_skips_probe(self):
        fake = self._install({"a": ["1.1.1.1"]}, switch_fail=["a"])
        self.assertEqual(
            renew.sort_by_exit_stability(["a", "b"], self.PROXY, sample=1),
            ["a", "b"])
        self.assertEqual(fake.queries, 0)

    def test_single_node_pool_is_not_probed(self):
        # 只有一个节点时排序毫无意义，别白花一次切换 + 两次查询
        fake = self._install({"a": ["1.1.1.1", "2.2.2.2"]})
        self.assertEqual(renew.sort_by_exit_stability(["a"], self.PROXY), ["a"])
        self.assertEqual((fake.switched, fake.queries), ([], 0))

    def test_sample_larger_than_pool(self):
        self._install({"a": ["1.1.1.1", "8.8.8.8"], "b": ["2.2.2.2"]})
        self.assertEqual(
            renew.sort_by_exit_stability(["a", "b"], self.PROXY, sample=99),
            ["b", "a"])

    def test_sample_covers_the_nodes_actually_used(self):
        # 抽检范围要盖住一次 job 真正会用到的节点数（max_retries 上限 5）
        self.assertGreaterEqual(renew._STABILITY_SAMPLE, 5)


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


class FakeDriver:
    def __init__(self, connected=True):
        self._connected = connected
        self.connect_calls = 0

    def is_connected(self):
        return self._connected

    def connect(self):
        self.connect_calls += 1
        self._connected = True


class FakeSB:
    """只实现 _load_cookies 用到的那几个 webdriver 命令。

    断连状态下调用任何一个都记进 illegal_calls —— 真实驱动上这些命令
    会打到已经停掉的 chromedriver 上。
    """

    def __init__(self, url="", title="", connected=True, get_error=None):
        self.driver = FakeDriver(connected)
        self.url = url
        self.title = title
        self.get_error = get_error
        self.added = []
        self.illegal_calls = []

    def _guard(self, name):
        if not self.driver.is_connected():
            self.illegal_calls.append(name)

    def get(self, url):
        self._guard("get")
        if self.get_error:
            raise self.get_error

    def add_cookie(self, cookie):
        self._guard("add_cookie")
        self.added.append(cookie)

    def get_current_url(self):
        self._guard("get_current_url")
        return self.url

    def get_title(self):
        self._guard("get_title")
        return self.title


class TestLoadCookies(unittest.TestCase):
    """cookie 快速通道: 不能碰断连的驱动，也不能因为节点不通就删掉会话"""

    EMAIL = "cookie-test@example.com"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch_renew(self, COOKIES_DIR=tmp.name)
        self.path = renew._cookies_path(self.EMAIL)
        with open(self.path, "w") as f:
            f.write('[{"name": "session", "value": "abc"}]')
        patch_renew(self, time=mock.MagicMock())  # 不要真的 sleep

    def test_reconnects_driver_before_cookie_ops(self):
        # 断连时 sb.get() 会被换成 sb.cdp.get()，超时只打一行日志不抛异常，
        # 后面的 add_cookie 就全打在停掉的 chromedriver 上了
        sb = FakeSB(url=renew.BASE_URL + "/dashboard", connected=False)
        self.assertTrue(renew._load_cookies(sb, self.EMAIL))
        self.assertEqual(sb.illegal_calls, [])
        self.assertEqual(sb.driver.connect_calls, 1)

    def test_recognizes_dashboard_by_title(self):
        sb = FakeSB(url="https://dashboard.katabump.com/", title="Dashboard | Katabump")
        self.assertTrue(renew._load_cookies(sb, self.EMAIL))
        self.assertEqual(len(sb.added), 1)

    def test_keeps_file_when_site_unreachable(self):
        # 出口节点打不开站点 ≠ 会话过期。删掉的话下次重试就只能硬闯 Turnstile
        sb = FakeSB(get_error=RuntimeError("timeout"))
        self.assertFalse(renew._load_cookies(sb, self.EMAIL))
        self.assertTrue(os.path.exists(self.path))

    def test_drops_file_when_session_expired(self):
        # 站点打开了却被踢回登录页 → cookie 真的失效了
        sb = FakeSB(url=renew.BASE_URL + "/auth/login", title="Login | Katabump")
        self.assertFalse(renew._load_cookies(sb, self.EMAIL))
        self.assertFalse(os.path.exists(self.path))

    def test_drops_unreadable_file(self):
        with open(self.path, "w") as f:
            f.write("{ not json")
        sb = FakeSB()
        self.assertFalse(renew._load_cookies(sb, self.EMAIL))
        self.assertFalse(os.path.exists(self.path))

    def test_drops_empty_cookie_list_without_touching_browser(self):
        with open(self.path, "w") as f:
            f.write("[]")
        sb = FakeSB()
        self.assertFalse(renew._load_cookies(sb, self.EMAIL))
        self.assertEqual(sb.added, [])
        self.assertFalse(os.path.exists(self.path))

    def test_missing_file_is_not_an_error(self):
        os.remove(self.path)
        self.assertFalse(renew._load_cookies(FakeSB(), self.EMAIL))


class FakeLoginSB:
    """只实现 login() 走「登录页打不开」这条路会用到的命令。

    关键是复刻两个真实行为:
      * uc_open_with_reconnect 导航超时时**不抛异常**，只 print 一行
        "Timeout loading <url>" 就返回；
      * 之后 sb.get_page_source() 走 CDP 通道，页面没加载出来时抛
        "Element {html} was not found after 7 seconds"，而不是返回空串。
    """

    def __init__(self, page_source=None, dead_tab=False):
        self.driver = FakeDriver()
        self.page_source = page_source
        self.dead_tab = dead_tab
        self.source_calls = 0
        self.opened = []

    def uc_open_with_reconnect(self, url, reconnect_time=None):
        self.opened.append(url)

    def get_page_source(self):
        self.source_calls += 1
        if self.page_source is None:
            raise Exception("\n Element {html} was not found after 7 seconds!")
        return self.page_source

    def save_screenshot(self, name, folder=None):
        pass

    def wait_for_element(self, selector, timeout=None):
        raise Exception(f"Element {{{selector}}} was not found!")

    def get_current_url(self):
        if self.dead_tab:
            raise Exception("tab 已经不可用")
        return renew.BASE_URL + "/auth/login"

    def get_title(self):
        if self.dead_tab:
            raise Exception("tab 已经不可用")
        return "Just a moment..."


class TestLoginPageUnreachable(unittest.TestCase):
    """导航超时不能变成「未预期异常」。

    uc_open_with_reconnect 打不开页面时只打一行日志就返回，接着那次
    sb.get_page_source() 会抛 Element {html} not found。这个异常一路上抛会
    被 run_account 包成 RenewError，而 RenewError 绕过了换节点重试 ——
    本该「这个出口节点不行，换一个」的失败，变成了账号直接告吹。
    所以 login() 必须自己收住，返回 False。
    """

    EMAIL = "login-test@example.com"
    PASSWORD = "pw"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # _save_artifacts 自己也要读一次源码，会干扰下面数 source_calls 的用例
        patch_renew(self, COOKIES_DIR=os.path.join(tmp.name, "cookies"),
                    _save_artifacts=mock.MagicMock(),
                    time=mock.MagicMock())  # 不要真的 sleep

    def test_unreachable_page_fails_login_instead_of_raising(self):
        sb = FakeLoginSB(page_source=None)
        self.assertFalse(renew.login(sb, self.EMAIL, self.PASSWORD))
        tags = [c.args[1] for c in renew._save_artifacts.call_args_list]
        self.assertEqual(tags, ["login_page_unreachable"])

    def test_gives_up_early_on_unreachable_page(self):
        # 取不到源码的那次调用自己要在 CDP 里空等 7 秒，45 轮足够烧掉
        # 一次尝试的全部时间预算，剩下的节点连试的机会都没有
        sb = FakeLoginSB(page_source=None)
        renew.login(sb, self.EMAIL, self.PASSWORD)
        self.assertLessEqual(sb.source_calls, renew._LOGIN_UNREACHABLE_POLLS)

    def test_detects_login_form_in_page_source(self):
        # 页面里出现 email 输入框就该立刻跳出等待循环（只查一次源码）
        sb = FakeLoginSB(page_source='<form><input name="email"></form>')
        renew.login(sb, self.EMAIL, self.PASSWORD)
        self.assertEqual(sb.source_calls, 1)

    def test_dead_tab_diagnostics_do_not_raise(self):
        # 页面能取到源码但没有表单 → 走 wait_for_element 失败的诊断分支，
        # 这时 get_current_url / get_title 自己也可能抛，不能让诊断代码
        # 把一次干净的 return False 变回「未预期异常」
        sb = FakeLoginSB(page_source="<html>Just a moment...</html>",
                         dead_tab=True)
        self.assertFalse(renew.login(sb, self.EMAIL, self.PASSWORD))


class FakeLink:
    def __init__(self, href="", text="See"):
        self.href = href
        self.text = text
        self.clicks = 0

    def get_attribute(self, name):
        return self.href if name == "href" else ""

    def click(self):
        self.clicks += 1


class FakeRenewSB:
    """续期流程用到的那几个命令；页面元素按选择器查表返回"""

    def __init__(self, url, elements=None):
        self.driver = FakeDriver()
        self.url = url
        self.elements = elements or {}
        self.screenshots = []

    def get_current_url(self):
        return self.url

    def get_title(self):
        return ""

    def find_element(self, selector, timeout=None):
        if selector in self.elements:
            return self.elements[selector]
        raise Exception(f"Element {{{selector}}} was not found!")

    def find_elements(self, selector):
        return []

    def execute_script(self, script):
        return None

    def save_screenshot(self, name, folder=None):
        self.screenshots.append(name)

    def get_page_source(self):
        return "<html></html>"


class TestSessionLost(unittest.TestCase):
    """会话中途失效必须被认出来，而不是报成「页面结构变了」

    真实发生过: dashboard 正常渲染、'See' 也点到了，落地页却是 /auth/login。
    之后每一步都在登录页上找元素，最后报「服务器详情页找不到 Renew 按钮」——
    结论和真实原因无关，而且因为是 RenewError 还绕过了换节点重试。
    """

    EMAIL = "session-test@example.com"
    LOGIN_URL = renew.BASE_URL + "/auth/login"
    DASH_URL = renew.BASE_URL + "/dashboard"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch_renew(self, COOKIES_DIR=tmp.name,
                    _save_artifacts=mock.MagicMock(),
                    _check_exit_ip=mock.MagicMock(return_value="1.2.3.4"),
                    time=mock.MagicMock())  # 不要真的 sleep
        self.path = renew._cookies_path(self.EMAIL)
        with open(self.path, "w") as f:
            f.write('[{"name": "session", "value": "abc"}]')

    def test_not_a_renew_error(self):
        # 继承了 RenewError 就会被 run_account 直接上抛，丧失换节点重试
        self.assertFalse(issubclass(renew.SessionLost, renew.RenewError))

    def test_login_page_raises_and_drops_cookies(self):
        sb = FakeRenewSB(self.LOGIN_URL)
        with self.assertRaises(renew.SessionLost):
            renew._assert_session_alive(sb, self.EMAIL, "测试")
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(renew._save_artifacts.call_args.args[1], "session_lost")

    def test_normal_page_is_a_noop(self):
        sb = FakeRenewSB(self.DASH_URL)
        renew._assert_session_alive(sb, self.EMAIL, "测试")
        self.assertTrue(os.path.exists(self.path))
        renew._save_artifacts.assert_not_called()

    def test_unavailable_url_does_not_raise(self):
        sb = FakeRenewSB(self.DASH_URL)
        sb.get_current_url = mock.MagicMock(side_effect=Exception("tab 没了"))
        renew._assert_session_alive(sb, self.EMAIL, "测试")   # 交回原失败路径

    def test_see_click_landing_on_login_page(self):
        link = FakeLink(href=renew.BASE_URL + "/servers/edit?id=1")
        # 真实日志里点击后的落地页就是登录页，链接本身还在 dashboard 上找到了
        sb = FakeRenewSB(self.LOGIN_URL,
                         {'a[href*="/servers/edit?id="]': link})
        with self.assertRaises(renew.SessionLost):
            renew._goto_server_detail(sb, email=self.EMAIL)
        self.assertEqual(link.clicks, 1)

    def test_missing_renew_button_on_login_page(self):
        # 登录页上当然没有 Renew 按钮，别把它报成详情页结构问题
        sb = FakeRenewSB(self.LOGIN_URL)
        with self.assertRaises(renew.SessionLost):
            renew._open_renew_modal(sb, email=self.EMAIL)

    def test_missing_renew_button_while_logged_in_is_fatal(self):
        # 会话还在却找不到按钮 → 页面真的变了，这才是 RenewError
        sb = FakeRenewSB(renew.BASE_URL + "/servers/edit?id=1")
        with self.assertRaises(renew.RenewError):
            renew._open_renew_modal(sb, email=self.EMAIL)


class FakeSubmitButton:
    def __init__(self, page):
        self.page = page

    def click(self):
        # 灰掉的按钮在真实浏览器里点了也不会触发任何行为
        if self.page.disabled:
            self.page.blocked_clicks += 1
            return
        self.page.click_submit()


class FakeRenewPage:
    """改版后的续期弹窗替身: 只认 renew.py 里那几段 JS 常量

    按脚本原文分派而不是靠关键字猜，脚本一改这里就会炸出来。

    构造参数描述页面的「性格」:
      mode          —— _ALTCHA_MODE_JS 的返回值
      autosolve     —— 点了提交后第几次轮询时 PoW 算完（None = 页面脚本压根没跑）
      autosubmit    —— 算完后页面会不会自己把表单发出去
      trigger_works —— 我们手动踢 widget 时能不能解出 payload
      alert_after   —— 表单发出去后第几次读取才渲染出提示
    """

    def __init__(self, mode="onsubmit", autosolve=2, autosubmit=True,
                 disabled=False, trigger_works=True, alert="Server renewed!",
                 alert_after=0, url=None):
        self.driver = FakeDriver()
        self.url = url or (renew.BASE_URL + "/servers/edit?id=1")
        self.mode = mode
        self.autosolve = autosolve
        self.autosubmit = autosubmit
        self.disabled = disabled
        self.trigger_works = trigger_works
        self.alert = alert
        self.alert_after = alert_after
        self.running = disabled      # 按钮一开始就灰着 = 验证已经在跑
        self.payload = 0
        self.marked = False
        self.submitted = False       # 表单发出去了，页面重载
        self.forced = False          # 走了 form.submit() 兜底
        self.clicks = 0
        self.blocked_clicks = 0
        self.triggered = 0
        self.screenshots = []
        self._ticks = 0
        self._alert_reads = 0

    # --- 页面自身的行为 ---

    def click_submit(self):
        self.clicks += 1
        self.running = True
        self.disabled = True         # altcha-progress.js 立刻置灰成 Verifying...

    def _advance(self):
        """验证跑起来之后，每轮询一次往前走一步"""
        if not self.running or self.submitted:
            return
        self._ticks += 1
        if self.autosolve is not None and self._ticks >= self.autosolve:
            self.payload = 96
            if self.autosubmit:
                self.submitted = True

    @property
    def label(self):
        if not self.disabled:
            return "Renew"
        return "Verifying... 100%" if self.payload else "Verifying... 0%"

    # --- sb 接口 ---

    def execute_script(self, script):
        body = script[len("return "):] if script.startswith("return ") else script
        body = body.strip()
        if body == renew._MARK_JS.strip():
            self.marked = True
            return 1
        if body == renew._SUBMITTED_JS.strip():
            self._advance()
            # 表单一发出去页面就重载，window 上的记号跟着没了
            return "navigated" if self.submitted else None
        if body == renew._RENEW_FORM_JS.strip():
            return {"disabled": self.disabled, "label": self.label,
                    "payload": self.payload}
        if body == renew._CLICK_SUBMIT_JS.strip():
            self.click_submit()
            return "button"
        if body == renew._FORCE_SUBMIT_JS.strip():
            self.forced = True
            self.submitted = True
            return True
        if body == renew._ALTCHA_MODE_JS.strip():
            return self.mode
        if body == renew._ALTCHA_TRIGGER_JS.strip():
            self.triggered += 1
            if self.trigger_works:
                self.payload = 96
            return "verify()"
        if body == renew._ALTCHA_SOLVED_JS.strip():
            return bool(self.payload)
        if body == renew._ALERT_TEXT_JS.strip():
            if not self.submitted:
                return ""
            self._alert_reads += 1
            return self.alert if self._alert_reads > self.alert_after else ""
        raise AssertionError(f"意料之外的脚本: {body[:60]}")

    def find_element(self, selector, timeout=None):
        if selector == 'div.modal.show form button[type="submit"]':
            return FakeSubmitButton(self)
        raise Exception(f"Element {{{selector}}} was not found!")

    def find_elements(self, selector):
        return []

    def get_current_url(self):
        return self.url

    def get_title(self):
        return ""

    def save_screenshot(self, name, folder=None):
        self.screenshots.append(name)

    def get_page_source(self):
        return "<html></html>"


class TestRenewSubmit(unittest.TestCase):
    """提交续期表单: 点完按钮要等到表单真的发出去

    锁死 run 33020798364 的失败: 站点把 ALTCHA 改成 auto="onsubmit" 且整行
    藏了起来，我们仍抢先点复选框 —— 验证跑完后提交按钮永远停在 disabled 的
    「Verifying... 100%」，我们那一下点击成了空操作，payload 生成了却没人提交
    表单，最后报成「未检测到任何提示框」。
    """

    EMAIL = "renew-test@example.com"

    def setUp(self):
        patch_renew(self, time=FakeClock(),
                    _save_artifacts=mock.MagicMock(),
                    send_tg_message=mock.MagicMock())

    def test_hidden_widget_is_not_touched(self):
        # onsubmit 形态下任何手动点击都会把按钮锁死，这里连点击脚本都不该发
        page = FakeRenewPage(mode="onsubmit")
        self.assertTrue(renew._solve_altcha(page))
        self.assertEqual(page.triggered, 0)
        self.assertEqual(page.clicks, 0)

    def test_missing_widget_defers_to_submit(self):
        # 没有 widget 就别自己判「验证失败」，交给提交结果说话
        page = FakeRenewPage(mode="none")
        self.assertTrue(renew._solve_altcha(page))

    def test_visible_widget_still_uses_the_click_path(self):
        # 老版可见复选框的处理逻辑必须原样保留
        page = FakeRenewPage(mode="interactive")
        page.payload = 96                      # 走到「已自动通过」那一支
        self.assertTrue(renew._solve_altcha(page))

    def test_waits_for_the_page_to_submit(self):
        page = FakeRenewPage(autosolve=3, autosubmit=True)
        renew._submit_renew(page, email=self.EMAIL)
        self.assertEqual(page.clicks, 1)
        self.assertTrue(page.submitted)
        self.assertFalse(page.forced)          # 页面自己提交了，不必兜底

    def test_stuck_at_verifying_falls_back_to_direct_submit(self):
        # 就是线上那次: payload 有了，按钮灰着，页面脚本再也不动
        page = FakeRenewPage(autosolve=2, autosubmit=False)
        renew._submit_renew(page, email=self.EMAIL)
        self.assertTrue(page.forced)
        self.assertEqual(page.triggered, 0)    # payload 现成的，别再验证一次
        renew._save_artifacts.assert_not_called()

    def test_disabled_button_is_never_clicked(self):
        # 进来时按钮已经灰着（验证在跑），点它只是空操作
        page = FakeRenewPage(autosolve=2, autosubmit=False, disabled=True)
        renew._submit_renew(page, email=self.EMAIL)
        self.assertEqual(page.clicks, 0)
        self.assertEqual(page.blocked_clicks, 0)
        self.assertTrue(page.forced)

    def test_triggers_altcha_when_the_page_script_never_runs(self):
        page = FakeRenewPage(autosolve=None, trigger_works=True)
        renew._submit_renew(page, email=self.EMAIL)
        self.assertEqual(page.triggered, 1)
        self.assertTrue(page.forced)

    def test_reports_the_button_state_when_nothing_works(self):
        page = FakeRenewPage(autosolve=None, trigger_works=False)
        with self.assertRaises(renew.RenewError) as caught:
            renew._submit_renew(page, email=self.EMAIL)
        self.assertIn("Verifying", str(caught.exception))
        self.assertEqual(renew._save_artifacts.call_args.args[1],
                         "renew_submit_stuck")

    def test_session_lost_on_submit_is_recoverable(self):
        # 提交时被弹回登录页 → SessionLost，可以换节点重来，不是 RenewError
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch_renew(self, COOKIES_DIR=tmp.name,
                    _check_exit_ip=mock.MagicMock(return_value="1.2.3.4"))
        page = FakeRenewPage(autosolve=None, trigger_works=False,
                             url=renew.BASE_URL + "/auth/login")
        with self.assertRaises(renew.SessionLost):
            renew._submit_renew(page, email=self.EMAIL)

    def test_result_waits_for_the_alert_to_render(self):
        # 表单刚发出去，提示要等页面重载完才出现，别读一次就下结论
        page = FakeRenewPage(autosolve=1, alert_after=3,
                             alert="You can't renew your server yet.")
        renew._submit_renew(page, email=self.EMAIL)
        self.assertEqual(renew._check_renew_result(page, email=self.EMAIL),
                         "skip")

    def test_result_gives_up_when_no_alert_ever_shows(self):
        page = FakeRenewPage(autosolve=1, alert="")
        renew._submit_renew(page, email=self.EMAIL)
        self.assertEqual(renew._check_renew_result(page, email=self.EMAIL),
                         "error")
        self.assertEqual(renew._save_artifacts.call_args.args[1],
                         "renew_no_alert")


class TestExitIpDrift(unittest.TestCase):
    """会话失效时报出口 IP 变化: 查不到就别报，免得把排查引偏"""

    def test_reports_change(self):
        patch_renew(self, _EXIT_IP="1.1.1.1", _EXIT_PROXY=None,
                    _check_exit_ip=mock.MagicMock(return_value="2.2.2.2"))
        self.assertEqual(renew._exit_ip_drift(), "1.1.1.1 → 2.2.2.2")

    def test_same_ip_is_silent(self):
        patch_renew(self, _EXIT_IP="1.1.1.1", _EXIT_PROXY=None,
                    _check_exit_ip=mock.MagicMock(return_value="1.1.1.1"))
        self.assertEqual(renew._exit_ip_drift(), "")

    def test_failed_lookup_is_not_a_change(self):
        patch_renew(self, _EXIT_IP="1.1.1.1", _EXIT_PROXY=None,
                    _check_exit_ip=mock.MagicMock(return_value="(查询失败: boom)"))
        self.assertEqual(renew._exit_ip_drift(), "")

    def test_unknown_baseline_is_not_a_change(self):
        patch_renew(self, _EXIT_IP="(查询失败: boom)", _EXIT_PROXY=None,
                    _check_exit_ip=mock.MagicMock(return_value="2.2.2.2"))
        self.assertEqual(renew._exit_ip_drift(), "")


class TestHardTimeout(unittest.TestCase):
    """卡死的一次尝试必须被闹钟打断，否则整个 job 挂到被 GitHub 取消"""

    def test_interrupts_a_stuck_call(self):
        with self.assertRaises(renew.AttemptTimeout):
            with renew.hard_timeout(1, "测试"):
                time.sleep(5)

    def test_restores_previous_handler_and_cancels_alarm(self):
        import signal
        sentinel = signal.getsignal(signal.SIGALRM)
        with renew.hard_timeout(30, "测试"):
            pass
        self.assertIs(signal.getsignal(signal.SIGALRM), sentinel)
        # 闹钟已撤: alarm(0) 返回上一个闹钟的剩余秒数
        self.assertEqual(signal.alarm(0), 0)

    def test_zero_means_no_limit(self):
        with renew.hard_timeout(0, "测试"):
            pass  # 不设闹钟，也不该抛


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
