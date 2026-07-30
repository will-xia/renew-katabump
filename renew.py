#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import subprocess
import json
import traceback
import requests
from seleniumbase import SB


class RenewError(Exception):
    """致命错误: 立即中断整个流程，并以非零退出码结束（CI 会标红）。

    「未到续期时间」不属于错误，走 skip 状态，不抛这个异常。
    """

# ==========================================
# ========== Mihomo 代理池模块 ==============
# ==========================================
MIHOMO_PATH = os.path.join(os.getcwd(), "mihomo")
MIHOMO_CONFIG = os.path.join(os.getcwd(), "config.yaml")
MIHOMO_LOG = os.path.join(os.getcwd(), "mihomo.log")
MIHOMO_CONTROLLER = "http://127.0.0.1:9090"
MIHOMO_MIXED_PORT = 7890
MIHOMO_GROUP = "MyGroup"

_mihomo_proc = None  # 用于退出时清理


def _mihomo_log(msg):
    print(f"[Mihomo] {msg}")
    try:
        with open(MIHOMO_LOG, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def setup_mihomo(sub_url: str) -> bool:
    """下载 Mihomo 二进制、生成配置、启动代理引擎"""
    global _mihomo_proc

    if not sub_url:
        return False

    _mihomo_log(f"检测到 SUB_URL，准备启动代理池...")

    # 1. 下载二进制（若不存在）
    if not os.path.exists(MIHOMO_PATH):
        _mihomo_log("下载 mihomo-linux-amd64...")
        try:
            url = "https://github.com/MetaCubeX/mihomo/releases/download/v1.18.9/mihomo-linux-amd64-v1.18.9.gz"
            subprocess.run(["curl", "-L", "-o", "mihomo.gz", url], check=True, timeout=120)
            subprocess.run(["gzip", "-d", "mihomo.gz"], check=True)
            subprocess.run(["chmod", "+x", MIHOMO_PATH], check=True)
            _mihomo_log("下载完成")
        except Exception as e:
            _mihomo_log(f"下载失败: {e}")
            return False

    # 1.5 检查二进制是否可执行
    try:
        result = subprocess.run(["file", MIHOMO_PATH], capture_output=True, text=True, timeout=5)
        _mihomo_log(f"二进制信息: {result.stdout.strip()}")
        result2 = subprocess.run([MIHOMO_PATH, "-v"], capture_output=True, text=True, timeout=5)
        _mihomo_log(f"版本: {result2.stdout.strip()[:200]}")
    except Exception as e:
        _mihomo_log(f"二进制检查异常: {e}")

    # 2. 生成配置
    config_yaml = f"""
mixed-port: {MIHOMO_MIXED_PORT}
allow-lan: false
mode: rule
log-level: debug
external-controller: 127.0.0.1:9090
proxy-providers:
  sub1:
    type: http
    url: "{sub_url}"
    interval: 3600
    path: ./sub1.yaml
    headers:
      User-Agent: ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"]
    health-check:
      enable: true
      interval: 600
      url: http://www.gstatic.com/generate_204
proxy-groups:
  - name: {MIHOMO_GROUP}
    type: select
    use:
      - sub1
rules:
  - MATCH,{MIHOMO_GROUP}
"""
    try:
        with open(MIHOMO_CONFIG, "w") as f:
            f.write(config_yaml)
        _mihomo_log(f"配置已写入 config.yaml ({len(config_yaml)} 字节)")
        _mihomo_log(f"配置内容:\n{config_yaml[:500]}")
    except Exception as e:
        _mihomo_log(f"写入配置失败: {e}")
        return False

    # 3. 语法检查
    try:
        result = subprocess.run(
            [MIHOMO_PATH, "-d", os.getcwd(), "-f", MIHOMO_CONFIG, "-t"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            _mihomo_log(f"配置语法错误: {result.stderr}")
            return False
        _mihomo_log("配置语法检查通过")
    except Exception as e:
        _mihomo_log(f"配置检查异常: {e}")

    # 4. 启动 Mihomo
    try:
        with open(MIHOMO_LOG, "w") as log_f:
            _mihomo_proc = subprocess.Popen(
                [MIHOMO_PATH, "-d", os.getcwd(), "-f", MIHOMO_CONFIG],
                stdout=log_f, stderr=log_f,
                start_new_session=True
            )
        _mihomo_log(f"Mihomo 已启动 (PID={_mihomo_proc.pid})")
    except Exception as e:
        _mihomo_log(f"启动失败: {e}")
        return False

    # 5. 等待就绪（最多 15 秒）
    mihomo_ready = False
    for i in range(15):
        time.sleep(1)
        # 检查进程是否还活着
        if _mihomo_proc and _mihomo_proc.poll() is not None:
            _mihomo_log(f"⚠️ Mihomo 进程已退出 (code={_mihomo_proc.returncode})")
            try:
                with open(MIHOMO_LOG) as f:
                    _mihomo_log(f"日志内容:\n{f.read()[:2000]}")
            except Exception:
                pass
            return False
        try:
            r = requests.get(f"{MIHOMO_CONTROLLER}/version", timeout=2)
            _mihomo_log(f"Mihomo 就绪 (耗时 {i+1}s)，版本: {r.text[:80]}")
            mihomo_ready = True
            break
        except Exception:
            if i % 5 == 4:
                _mihomo_log(f"等待 Mihomo 就绪... ({i+1}s)")

    if not mihomo_ready:
        _mihomo_log("⚠️ Mihomo 15 秒内未就绪，打印日志:")
        try:
            with open(MIHOMO_LOG) as f:
                _mihomo_log(f"日志内容:\n{f.read()[:2000]}")
        except Exception:
            pass
        # 仍然尝试继续（可能 API 延迟绑定）

    # 6. 刷新 provider（多等几秒确保完全就绪）
    if mihomo_ready:
        time.sleep(3)
        try:
            r = requests.put(f"{MIHOMO_CONTROLLER}/providers/proxies/sub1", timeout=15)
            _mihomo_log(f"刷新 provider: {r.status_code}")
            time.sleep(3)
        except Exception as e:
            _mihomo_log(f"刷新 provider 失败: {e}")

    return True


def get_mihomo_proxies() -> list:
    """从 Mihomo API 获取可用节点列表"""
    try:
        r = requests.get(f"{MIHOMO_CONTROLLER}/proxies/{MIHOMO_GROUP}", timeout=10)
        data = r.json()
        all_nodes = data.get("all", [])
        # 过滤掉 DIRECT / REJECT / 组名自身
        filtered = [n for n in all_nodes if n not in ("DIRECT", "REJECT", MIHOMO_GROUP)]
        _mihomo_log(f"获取到 {len(filtered)} 个候选节点")
        return filtered
    except Exception as e:
        _mihomo_log(f"获取节点列表失败: {e}")
        return []


def test_mihomo_proxies(proxy_names: list) -> list:
    """并发测速，返回健康节点列表"""
    _mihomo_log(f"开始对 {len(proxy_names)} 个节点测速...")
    healthy = []
    batch_size = 15
    for i in range(0, len(proxy_names), batch_size):
        batch = proxy_names[i:i + batch_size]
        for name in batch:
            try:
                r = requests.get(
                    f"{MIHOMO_CONTROLLER}/proxies/{requests.utils.quote(name, safe='')}/delay",
                    params={"timeout": "3000", "url": "http://www.gstatic.com/generate_204"},
                    timeout=5
                )
                data = r.json()
                if data.get("delay"):
                    healthy.append(name)
            except Exception:
                pass
    _mihomo_log(f"测速完成，健康节点: {len(healthy)}/{len(proxy_names)}")
    return healthy


def switch_mihomo_proxy(name: str) -> bool:
    """切换 Mihomo 活跃节点"""
    try:
        requests.put(
            f"{MIHOMO_CONTROLLER}/proxies/{MIHOMO_GROUP}",
            json={"name": name},
            timeout=5
        )
        _mihomo_log(f"切换到节点: {name}")
        return True
    except Exception as e:
        _mihomo_log(f"切换节点失败: {e}")
        return False


def stop_mihomo():
    """停止 Mihomo 进程"""
    global _mihomo_proc
    if _mihomo_proc:
        try:
            _mihomo_proc.terminate()
            _mihomo_proc.wait(timeout=5)
            _mihomo_log("Mihomo 已停止")
        except Exception:
            try:
                _mihomo_proc.kill()
            except Exception:
                pass
        _mihomo_proc = None

# 单账号回退配置（当未配置 USERS_JSON 时使用）
EMAIL        = os.environ.get("KATABUMP_EMAIL") or ""    # 登录邮箱
PASSWORD     = os.environ.get("KATABUMP_PASSWORD") or "" # 账号密码
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID") or ""        # tg通知 chat id(可选)
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""      # tg通知bot token(可选)

BASE_URL = "https://dashboard.katabump.com"  # 网站链接

# 从 repository secret 读取多账号配置:
# USERS_JSON = '[{"username": "a@b.com", "password": "pwd"}, ...]'
def load_users():
    raw = os.environ.get("USERS_JSON") or ""
    if raw.strip():
        try:
            users = json.loads(raw)
            if isinstance(users, list) and users:
                return users
        except Exception as e:
            print(f"⚠️ 解析 USERS_JSON 失败: {e}")
    # 回退到单账号环境变量
    if EMAIL and PASSWORD:
        return [{"username": EMAIL, "password": PASSWORD}]
    return []


#   Telegram 推送模块
def send_tg_message(status_icon, status_text, time_left="", email=EMAIL):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 推送。")
        return

    # 获取北京时间 (UTC+8)
    local_time = time.gmtime(time.time() + 8 * 3600)
    current_time_str = time.strftime("%Y-%m-%d %H:%M:%S", local_time)

    # 邮箱脱敏：保留用户名前2位和后2位，中间用****代替
    if '@' in email:
        name, domain = email.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = email[:2] + '****'

    text = (
        f"🇫🇷 katabump 续期通知\n\n"
        f"{status_icon} {status_text}\n"
        f"👤 续期账户: {masked_email}\n"
        f"⏱️ 续期时间: {current_time_str}"
    )

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print("📩 Telegram 通知发送成功！")
        else:
            print(f"⚠️ Telegram 通知发送失败: {r.text}")
    except Exception as e:
        print(f"⚠️ Telegram 通知发送异常: {e}")

#  页面注入脚本
# 下面这些常量统一写成「表达式」形式，取值必须走 _js()，不要直接
# sb.execute_script()。原因见 _js() 的注释: 两条通道的语义正好相反。
_EXISTS_JS = """
(function(){
    return document.querySelector('.cf-turnstile') !== null
        || document.querySelector('input[name="cf-turnstile-response"]') !== null;
})()
"""

_SOLVED_JS = """
(function(){
    var i = document.querySelector('input[name="cf-turnstile-response"]');
    return !!(i && i.value && i.value.length > 20);
})()
"""

_TOKEN_LEN_JS = """
(function(){
    var i = document.querySelector('input[name="cf-turnstile-response"]');
    return i ? (i.value || '').length : -1;
})()
"""

_WININFO_JS = """
(function(){
    return {
        sx: window.screenX || 0,
        sy: window.screenY || 0,
        oh: window.outerHeight,
        ih: window.innerHeight
    };
})()
"""

# ===== 自动续期相关 =====

# 在模态框内查找 iframe 并展开，返回点击坐标
_ALTCHA_EXPAND_JS = """
(function() {
    var modal = document.querySelector('div.modal.show') || document;
    var iframes = modal.querySelectorAll('iframe');
    for (var i = 0; i < iframes.length; i++) {
        var r = iframes[i].getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
            iframes[i].style.width  = '300px';
            iframes[i].style.height = '150px';
            iframes[i].style.minWidth  = '300px';
            iframes[i].style.minHeight = '150px';
            iframes[i].style.visibility = 'visible';
            iframes[i].style.opacity = '1';
            var el = iframes[i];
            for (var j = 0; j < 10; j++) {
                el = el.parentElement;
                if (!el) break;
                el.style.overflow = 'visible';
            }
            var r2 = iframes[i].getBoundingClientRect();
            return { cx: Math.round(r2.x + 30), cy: Math.round(r2.y + r2.height / 2) };
        }
    }
    return null;
})()
"""

# 检测 ALTCHA 是否已验证通过
_ALTCHA_SOLVED_JS = """
(function(){
    var modal = document.querySelector('div.modal.show') || document;
    // hidden input 有值
    var inputs = modal.querySelectorAll('input[type="hidden"]');
    for (var i = 0; i < inputs.length; i++) {
        var n = (inputs[i].name || '').toLowerCase();
        if ((n.includes('altcha') || n.includes('captcha')) &&
            inputs[i].value && inputs[i].value.length > 20) return true;
    }
    // checkbox 变为 disabled
    var cbs = modal.querySelectorAll('input[type="checkbox"]');
    for (var j = 0; j < cbs.length; j++) {
        if (cbs[j].disabled) return true;
    }
    // widget data-state 属性
    var w = modal.querySelector('[data-state="verified"],.altcha--verified,.altcha-verified');
    if (w) return true;
    return false;
})()
"""

#  底层 JS 取值工具
def _js(sb, expression):
    """执行 JS 表达式并取回返回值。

    必须用这个包装，不要直接 sb.execute_script() 取值 —— UC 模式下
    sb.execute_script 会在两条语义**完全相反**的通道之间自动切换:

      * 驱动已连接 → selenium driver.execute_script: 脚本当「函数体」跑，
        少了 return 就静默返回 None（判断永远为假，最难查的一类 bug）；
      * 驱动断连 → sb.cdp.evaluate: 脚本当「表达式」求值，
        带 return 会直接抛 SyntaxError: Illegal return statement。

    连接状态在一次运行里会来回变化，所以这里先按函数体形式发，
    命中 CDP 通道报语法错时再按表达式形式重发。语法错发生在解析阶段，
    不会造成重复执行。

    注意 strip(): 表达式常量都以换行开头，`return` 后面直接跟换行会被
    JS 的自动分号插入(ASI)截断成 `return;`，结果又变成静默的 None。
    """
    expression = expression.strip()
    try:
        return sb.execute_script("return " + expression)
    except Exception as first_err:
        try:
            return sb.execute_script(expression)
        except Exception:
            raise first_err


def _check_exit_ip(proxy: str = None) -> str:
    """查询出口 IP（不经过浏览器，避免打乱驱动连接状态）"""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        return (r.text or "").strip() or "(空响应)"
    except Exception as e:
        return f"(查询失败: {e})"


def _ensure_connected(sb):
    """UC 模式断连时恢复连接。

    断连状态下 driver.execute_cdp_cmd 无法下发（点击验证码要用它），
    所以点击前必须确认连接是活着的。
    """
    try:
        if hasattr(sb.driver, "is_connected") and not sb.driver.is_connected():
            print("🔌 驱动处于断连状态，重新连接...")
            sb.driver.connect()
            time.sleep(0.5)
    except Exception as e:
        print(f"⚠️ 重新连接驱动失败: {e}")


#  底层输入工具
def js_fill_input(sb, selector: str, text: str):
    safe_text = text.replace('\\', '\\\\').replace('"', '\\"')
    sb.execute_script(f"""
    (function(){{
        var el = document.querySelector('{selector}');
        if (!el) return;
        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
        if (nativeInputValueSetter) {{
            nativeInputValueSetter.call(el, "{safe_text}");
        }} else {{
            el.value = "{safe_text}";
        }}
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }})()
    """)


def _activate_window():
    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls], capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]], timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"], timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _xdotool_click(x: int, y: int):
    _activate_window()
    try:
        subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)], timeout=3, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
    except Exception:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")

# 定位 Turnstile widget 并返回其视口坐标。
#
# 关键事实(已实测): Turnstile 把 widget 的 iframe 渲染在 **closed shadow root** 里，
# 所以 document.querySelectorAll('iframe') 只能看到 Cloudflare 那个 1x1 的
# jsd 探测 iframe，永远找不到 widget；element.shadowRoot 对 closed 模式返回 null，
# switch_to.frame() 也进不去。
# 但宿主容器 <div class="cf-turnstile"> 本身在普通 DOM 里，位置/尺寸完全可读，
# checkbox 固定在容器左侧（实测偏移约 21px，垂直居中），据此即可精确点击。
_TS_BOX_JS = """
(function() {
    var c = document.querySelector('.cf-turnstile');
    if (!c) return null;
    c.scrollIntoView({block: 'center'});
    var r = c.getBoundingClientRect();
    if (r.width < 30 || r.height < 20) return null;
    return {x: r.x, y: r.y, w: r.width, h: r.height,
            vw: window.innerWidth, vh: window.innerHeight};
})()
"""

# 失败时的诊断信息（不要 switch_to.frame，那会在 UC 模式下打断驱动连接）
_TS_DIAG_JS = r"""
(function(){
    var c = document.querySelector('.cf-turnstile');
    var i = document.querySelector('input[name="cf-turnstile-response"]');
    var out = {
        title: document.title,
        url: location.href,
        has_container: !!c,
        container_rect: null,
        container_html: c ? c.outerHTML.slice(0, 300) : null,
        has_input: !!i,
        token_len: i ? (i.value || '').length : -1,
        turnstile_api: typeof window.turnstile,
        light_dom_iframes: [],
        cf_resources: []
    };
    if (c) {
        var r = c.getBoundingClientRect();
        out.container_rect = {x: Math.round(r.x), y: Math.round(r.y),
                              w: Math.round(r.width), h: Math.round(r.height)};
    }
    document.querySelectorAll('iframe').forEach(function(f){
        var fr = f.getBoundingClientRect();
        out.light_dom_iframes.push(
            (f.src || '(empty)').slice(0, 80) + ' ' +
            Math.round(fr.width) + 'x' + Math.round(fr.height));
    });
    try {
        out.cf_resources = performance.getEntriesByType('resource')
            .filter(function(e){ return /challenge-platform|turnstile/.test(e.name); })
            .map(function(e){ return e.name.replace(/^https:\/\/[^\/]+/, '').slice(0, 100); });
    } catch (e) {}
    return out;
})()
"""


def _cdp_click(sb, x, y):
    """通过 CDP 派发受信任鼠标事件（无需聚焦/可见窗口，Xvfb CI 可用）。

    先 mouseMoved 再 press/release: Turnstile 会检查 checkbox 上是否有过
    指针移动，只发 press/release 容易被判定为非人类操作。
    """
    _ensure_connected(sb)
    try:
        # 分几步移到目标，纯跳变的指针轨迹更容易被判为脚本操作
        for step in (0.45, 0.75, 0.92, 1.0):
            sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": max(0.0, float(x) - 60 * (1 - step)),
                "y": max(0.0, float(y) - 35 * (1 - step)),
            })
            time.sleep(0.05)
        time.sleep(0.12)
        sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": float(x), "y": float(y),
            "button": "left", "clickCount": 1
        })
        time.sleep(0.05 + (time.time() % 1) * 0.08)
        sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": float(x), "y": float(y),
            "button": "left", "clickCount": 1
        })
        return True
    except Exception as e:
        print(f"⚠️ CDP 点击异常: {e}")
        return False


def _turnstile_box(sb):
    """返回 .cf-turnstile 容器的视口坐标，取不到返回 None"""
    try:
        return _js(sb, _TS_BOX_JS)
    except Exception as e:
        print(f"⚠️ 读取 Turnstile 容器坐标异常: {e}")
        return None


def _altcha_checkbox_coords(sb):
    """读取 ALTCHA widget 内 checkbox 的视口坐标。

    优先读 open shadow root 内的 checkbox；若是 closed shadow root
    （.shadowRoot 为 null，和 Turnstile 一样），退回宿主元素左侧偏移。
    """
    try:
        return _js(sb, """
            (function(){
                var w = document.querySelector('altcha-widget')
                     || document.querySelector('.altcha, [class*="altcha"]');
                if (!w) return null;
                var root = w.shadowRoot;
                var cb = root ? root.querySelector('input[type="checkbox"],[role="checkbox"]') : null;
                if (!cb) cb = w.querySelector('input[type="checkbox"],[role="checkbox"]');
                if (cb) {
                    cb.scrollIntoView({block:'center'});
                    var r = cb.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0)
                        return {x: r.x + r.width/2, y: r.y + r.height/2};
                }
                // closed shadow root: 按宿主元素左侧 + 垂直居中估算
                w.scrollIntoView({block:'center'});
                var hr = w.getBoundingClientRect();
                if (hr.width > 30 && hr.height > 15)
                    return {x: hr.x + 24, y: hr.y + hr.height/2};
                return null;
            })()
        """)
    except Exception:
        return None


#  失败证据留档（workflow 只上传 output/ 目录，写到别处等于丢弃）
ARTIFACT_DIR = os.path.join(os.getcwd(), "output", "screenshots")


def _save_artifacts(sb, tag: str):
    """保存截图 + 页面 HTML 到 output/screenshots/，供 CI artifact 上传"""
    try:
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
    except Exception:
        pass
    try:
        sb.save_screenshot(f"{tag}.png", folder=ARTIFACT_DIR)
        print(f"  🖼️ 截图已保存: output/screenshots/{tag}.png")
    except Exception as e:
        print(f"  ⚠️ 截图保存失败: {e}")
    try:
        html = sb.get_page_source() or ""
        with open(os.path.join(ARTIFACT_DIR, f"{tag}.html"), "w") as f:
            f.write(html)
        print(f"  📄 页面已保存: output/screenshots/{tag}.html ({len(html)} 字节)")
    except Exception as e:
        print(f"  ⚠️ 页面保存失败: {e}")


def _turnstile_diagnostics(sb, tag: str):
    """打印 Turnstile 失败诊断，并把截图/HTML 落到 output/screenshots/"""
    print("  ❌ Turnstile 处理失败，输出诊断信息:")
    try:
        d = _js(sb, _TS_DIAG_JS) or {}
        print(f"     标题: {d.get('title')} | URL: {d.get('url')}")
        print(f"     容器存在: {d.get('has_container')} rect={d.get('container_rect')}")
        print(f"     input 存在: {d.get('has_input')}, token 长度: {d.get('token_len')}, "
              f"window.turnstile: {d.get('turnstile_api')}")
        print(f"     容器 HTML: {d.get('container_html')}")
        print(f"     普通 DOM 内 iframe ({len(d.get('light_dom_iframes') or [])} 个, "
              f"widget 在 closed shadow root 里，这里看不到是正常的):")
        for line in (d.get('light_dom_iframes') or []):
            print(f"       - {line}")
        print("     Cloudflare 请求:")
        for line in (d.get('cf_resources') or []):
            print(f"       - {line}")
    except Exception as e:
        print(f"     诊断获取异常: {e}")
    _save_artifacts(sb, tag)


#  人机验证处理（使用 SeleniumBase UC Mode 内置方法，比手动 CDP 点击更可靠）
def handle_turnstile(sb) -> bool:
    print("🔍 处理 Cloudflare Turnstile 验证...")
    _ensure_connected(sb)

    # 阶段 0: 等 widget 渲染出来（api.js 是 async，容器有尺寸才说明渲染完成）
    box = None
    for i in range(15):
        box = _turnstile_box(sb)
        if box:
            print(f"✅ 定位到 Turnstile widget: "
                  f"({box['x']:.0f},{box['y']:.0f}) {box['w']:.0f}x{box['h']:.0f}（{i+1}s）")
            break
        time.sleep(1)
    if not box:
        print("⚠️ Turnstile widget 未渲染出可见容器")

    # 阶段 1: managed / 隐形模式可能自动放行，先短暂等待，避免多余点击
    print("⏳ 等待 Turnstile 自动通过...")
    for i in range(12):
        if _js(sb, _SOLVED_JS):
            print(f"✅ Turnstile 已自动通过（{i+1}s）")
            return True
        time.sleep(1)

    # 阶段 2: 使用 SeleniumBase UC Mode 内置方法点击 checkbox
    # Turnstile widget 的 checkbox 在 closed shadow root iframe 内，
    # 手动 CDP 点击无法穿透，必须用 UC Mode 的 PyAutoGUI 模拟真实鼠标点击
    print("⚠️ 未自动通过，使用 UC Mode 内置方法点击 Turnstile checkbox...")

    # 策略 A: uc_gui_click_captcha() - 使用 PyAutoGUI 模拟真实鼠标点击
    for attempt in range(3):
        if _js(sb, _SOLVED_JS):
            print("✅ Turnstile 已通过")
            return True

        try:
            print(f"🖱️  尝试 uc_gui_click_captcha() [尝试 {attempt + 1}]")
            sb.uc_gui_click_captcha()
            time.sleep(2)

            # 等待验证结果（最多 10 秒）
            for _ in range(20):
                time.sleep(0.5)
                if _js(sb, _SOLVED_JS):
                    token_len = _js(sb, _TOKEN_LEN_JS)
                    print(f"✅ Turnstile 通过（uc_gui_click_captcha，token 长度 {token_len}）")
                    return True
        except Exception as e:
            print(f"⚠️ uc_gui_click_captcha() 异常: {e}")

        # reset widget 后重试
        print(f"⚠️ 第 {attempt + 1} 次未通过，reset widget 后重试...")
        try:
            _js(sb, "(function(){ try { window.turnstile.reset(); return 'reset'; }"
                    " catch(e) { return 'reset failed: ' + e.message; } })()")
            time.sleep(2)
        except Exception as e:
            print(f"⚠️ turnstile.reset() 调用异常: {e}")

    # 策略 B: solve_captcha() - 另一个内置方法
    for attempt in range(2):
        if _js(sb, _SOLVED_JS):
            print("✅ Turnstile 已通过")
            return True

        try:
            print(f"🖱️  尝试 solve_captcha() [尝试 {attempt + 1}]")
            sb.solve_captcha()
            time.sleep(2)

            # 等待验证结果
            for _ in range(20):
                time.sleep(0.5)
                if _js(sb, _SOLVED_JS):
                    token_len = _js(sb, _TOKEN_LEN_JS)
                    print(f"✅ Turnstile 通过（solve_captcha，token 长度 {token_len}）")
                    return True
        except Exception as e:
            print(f"⚠️ solve_captcha() 异常: {e}")

        # reset widget
        try:
            _js(sb, "(function(){ try { window.turnstile.reset(); return 'reset'; }"
                    " catch(e) { return 'reset failed: ' + e.message; } })()")
            time.sleep(2)
        except Exception:
            pass

    # 策略 C: 回退到手动 CDP 点击（最后手段）
    print("⚠️ 内置方法均失败，回退到手动 CDP 点击...")
    x_offsets = (24, 30, 18, 36)
    for attempt in range(4):
        if _js(sb, _SOLVED_JS):
            print("✅ Turnstile 已通过")
            return True

        box = _turnstile_box(sb) or box
        if not box:
            print(f"⚠️ 取不到 Turnstile 容器坐标，无法点击 [尝试 {attempt + 1}]")
            time.sleep(2)
            continue

        dx = x_offsets[attempt % len(x_offsets)]
        cx = box["x"] + dx
        cy = box["y"] + box["h"] / 2
        print(f"🖱️ CDP 点击 ({cx:.0f},{cy:.0f}) 容器左偏移 {dx}px [尝试 {attempt + 1}]")
        _cdp_click(sb, cx, cy)

        # 等待验证结果
        for _ in range(24):
            time.sleep(0.5)
            if _js(sb, _SOLVED_JS):
                token_len = _js(sb, _TOKEN_LEN_JS)
                print(f"✅ Turnstile 通过（CDP 点击，token 长度 {token_len}）")
                return True

        # reset widget
        try:
            _js(sb, "(function(){ try { window.turnstile.reset(); return 'reset'; }"
                    " catch(e) { return 'reset failed: ' + e.message; } })()")
            time.sleep(2)
        except Exception as e:
            print(f"⚠️ turnstile.reset() 调用异常: {e}")

    _turnstile_diagnostics(sb, "turnstile_fail")
    return False


#  账户登录
def login(sb, email: str, password: str) -> bool:
    print(f"🌐 打开登录页面: {BASE_URL}/auth/login")
    sb.uc_open_with_reconnect(BASE_URL + "/auth/login", reconnect_time=8)
    time.sleep(8)

    # 先等待 Cloudflare 验证通过（最多等 45 秒）
    print("⏳ 等待 Cloudflare 验证通过...")
    cf_passed = False
    for i in range(45):
        page_src = sb.get_page_source() or ""
        if 'input[name="email"]' in page_src.lower() or 'name="email"' in page_src.lower():
            cf_passed = True
            print(f"✅ Cloudflare 验证已通过（{i+1}s）")
            break
        time.sleep(1)
    if not cf_passed:
        print("⚠️ Cloudflare 验证可能未通过，继续尝试...")

    try:
        sb.wait_for_element('input[name="email"]', timeout=15)
    except Exception:
        # 尝试大写选择器作为后备
        try:
            sb.wait_for_element('input[name="Email"]', timeout=5)
        except Exception:
            print("❌ 页面未加载出登录表单")
            cur_url = sb.get_current_url()
            page_title = sb.get_title() or ""
            print(f"  当前 URL: {cur_url}")
            print(f"  当前标题: {page_title}")
            _save_artifacts(sb, "login_load_fail")
            return False

    print("🍪 关闭可能的 Cookie 弹窗...")
    try:
        for btn in sb.find_elements("button"):
            if "Accept" in (btn.text or ""):
                btn.click()
                time.sleep(0.5)
                break
    except Exception:
        pass

    print(f"📧 填写邮箱...")
    js_fill_input(sb, 'input[name="email"]', email)
    time.sleep(0.3)

    print("🔑 填写密码...")
    js_fill_input(sb, 'input[name="password"]', password)
    time.sleep(1)

    # 等待 Turnstile 验证框出现（最多 15 秒）
    print("⏳ 等待 Turnstile 验证框出现...")
    ts_found = False
    for i in range(15):
        if _js(sb, _EXISTS_JS):
            ts_found = True
            print(f"✅ 检测到 Turnstile（{i+1}s）")
            break
        time.sleep(1)

    if ts_found:
        if not handle_turnstile(sb):
            print("❌ 登录界面的 Turnstile 验证失败")
            return False
    else:
        print("ℹ️ 未检测到 Turnstile")

    print("🖱️ 提交登录表单...")
    try:
        sb.click("button#submit")
    except Exception:
        sb.press_keys('input[name="password"]', '\n')

    def _logged_in():
        url = (sb.get_current_url() or "").split('?')[0].lower()
        title = (sb.get_title() or "").lower()
        return url.startswith(f"{BASE_URL.lower()}/dashboard") or "dashboard | katabump" in title

    print("⏳ 等待登录跳转...")
    for _ in range(15):
        time.sleep(1)
        if _logged_in():
            break

    page_title = sb.get_title() or ""
    if _logged_in():
        print(f"✅ 登录成功！(URL: {sb.get_current_url()}, Title: {page_title})")
        return True

    print(f"❌ 登录失败，页面未跳转到账户页。(URL: {sb.get_current_url()}, Title: {page_title})")
    alert_text = _read_alert(sb)
    if alert_text:
        print(f"  页面提示: {alert_text}")
    _save_artifacts(sb, "login_failed")
    return False


# ===== 自动续期流程 =====

def _read_alert(sb):
    """读取页面第一个 Bootstrap alert 的文本，找不到返回空串"""
    try:
        el = sb.find_element("div.alert", timeout=4)
        return (el.text or "").strip()
    except Exception:
        return ""


def _goto_server_detail(sb, email=EMAIL) -> bool:
    """在 Dashboard 首页查找并点击 See 进入服务器详情页"""
    print("\n🖥️  正在进入服务器续期页...")
    time.sleep(5)

    # 检查页面顶部是否已有"还无法续期"全局提示
    alert_text = _read_alert(sb)
    if alert_text and "can't renew" in alert_text.lower():
        print(f"ℹ️  页面顶部提示: {alert_text}")
        send_tg_message("⏳", "未到续期时间", alert_text, email=email)
        return False

    # 多种选择器尝试查找 See 链接
    selectors = [
        'a[href*="/servers/edit?id="]',
        'td a[href*="/servers/edit"]',
        'table a[href*="/servers/edit"]',
        'table td a',
    ]

    see_link = None
    for sel in selectors:
        try:
            see_link = sb.find_element(sel, timeout=8)
            print(f"✅ 通过选择器找到链接: {sel}")
            break
        except Exception:
            continue

    # 选择器全部失败，尝试通过文本内容查找
    if see_link is None:
        print("⚠️ 选择器未命中，尝试文本匹配...")
        try:
            for a in sb.find_elements("a"):
                if (a.text or "").strip().lower() == "see":
                    see_link = a
                    print("✅ 通过文本 'See' 找到链接")
                    break
        except Exception:
            pass

    if see_link is None:
        # 打印调试信息帮助排查
        cur_url = sb.get_current_url()
        title = sb.get_title() or ""
        print(f"❌ 未找到 'See' 链接")
        print(f"当前 URL: {cur_url}")
        print(f"页面标题: {title}")
        try:
            links = sb.find_elements("a")
            print(f"     页面共 {len(links)} 个链接:")
            for a in links[:20]:
                href = a.get_attribute("href") or ""
                txt  = (a.text or "").strip()[:30]
                if href:
                    print(f"       - [{txt}] -> {href}")
        except Exception:
            pass
        _save_artifacts(sb, "servers_page_fail")
        raise RenewError("Dashboard 上找不到进入服务器详情页的 'See' 链接")

    print("🖱️  点击 'See' 进入服务器详情页...")
    see_link.click()
    time.sleep(5)
    print(f"📄 当前页面: {sb.get_current_url()}")
    return True


def _open_renew_modal(sb) -> bool:
    """滚动到 Renew 按钮并点击，打开模态框"""
    print("\n🔄 查找 Renew 按钮...")
    try:
        renew_btn = sb.find_element('button[data-bs-target="#renew-modal"]', timeout=10)
    except Exception:
        try:
            renew_btn = sb.find_element('button.btn.btn-outline-primary', timeout=5)
        except Exception:
            print("  ❌ 未找到 Renew 按钮")
            _save_artifacts(sb, "renew_button_missing")
            raise RenewError("服务器详情页找不到 Renew 按钮")

    sb.execute_script("""
        (function(){
            var btn = document.querySelector('button[data-bs-target="#renew-modal"]')
                     || document.querySelector('button.btn.btn-outline-primary');
            if (btn) btn.scrollIntoView({behavior:'smooth',block:'center'});
        })()
    """)
    time.sleep(0.8)
    renew_btn.click()
    print("🖱️ 已点击 Renew 按钮，等待 ALTCHA 验证框...")
    time.sleep(3)

    try:
        sb.find_element('div.modal.show', timeout=5)
        print("✅ Renew 模态框已弹出")
        return True
    except Exception:
        print("⚠️ 模态框未弹出")
        _save_artifacts(sb, "renew_modal_missing")
        raise RenewError("点击 Renew 后模态框没有弹出")


def _solve_altcha(sb) -> bool:
    """处理 ALTCHA 人机验证"""
    print("\n🔐 处理 ALTCHA 人机验证...")
    time.sleep(2)

    # 先检查是否已自动通过
    if _js(sb, _ALTCHA_SOLVED_JS):
        print("✅ ALTCHA 已自动通过")
        return True

    # 展开模态框内 iframe 并获取坐标
    coords = None
    try:
        coords = _js(sb, _ALTCHA_EXPAND_JS)
    except Exception:
        pass

    if coords:
        print(f"  📍 找到模态框内 iframe 坐标: ({coords['cx']}, {coords['cy']})")

    # 最多尝试 3 轮
    for attempt in range(3):
        if _js(sb, _ALTCHA_SOLVED_JS):
            print(f"✅ ALTCHA 验证通过（第 {attempt + 1} 轮）")
            return True

        # 策略 A: CDP 精确点击 Shadow DOM 内 checkbox（受信任事件，无需聚焦窗口）
        cb = _altcha_checkbox_coords(sb)
        if cb:
            print(f"🖱️  CDP 点击 ALTCHA checkbox ({cb['x']:.1f},{cb['y']:.1f})")
            _cdp_click(sb, cb["x"], cb["y"])
            # 备份: 直接触发 shadow checkbox
            sb.execute_script("""
                (function(){
                    var w = document.querySelector('altcha-widget');
                    if (w && w.shadowRoot){
                        var cb = w.shadowRoot.querySelector('input[type="checkbox"]');
                        if (cb && !cb.checked) cb.click();
                    }
                })()
            """)
        # 策略 B: xdotool 物理点击 iframe 坐标
        elif coords:
            wi = None
            try:
                wi = _js(sb, _WININFO_JS)
            except Exception:
                pass
            if not wi:
                wi = {"sx": 0, "sy": 0, "oh": 800, "ih": 768}
            bar = wi["oh"] - wi["ih"]
            ax  = coords["cx"] + wi["sx"]
            ay  = coords["cy"] + wi["sy"] + bar
            print(f"🖱️  ALTCHA点击复选框  ({ax}, {ay})")
            _xdotool_click(ax, ay)

        # 策略 C: JS 遍历模态框内所有可点击元素
        sb.execute_script("""
            (function(){
                var modal = document.querySelector('div.modal.show');
                if (!modal) return;
                // 点击 iframe
                var iframes = modal.querySelectorAll('iframe');
                for (var i = 0; i < iframes.length; i++) {
                    iframes[i].click();
                    iframes[i].dispatchEvent(new MouseEvent('click', {bubbles:true}));
                }
                // 点击含 checkbox 的 label
                var labels = modal.querySelectorAll('label');
                for (var j = 0; j < labels.length; j++) {
                    var txt = (labels[j].textContent || '').toLowerCase();
                    if (txt.includes('robot') || txt.includes('captcha') || txt.includes('verify'))
                        labels[j].click();
                }
                // 点击 checkbox
                var cbs = modal.querySelectorAll('input[type="checkbox"]');
                for (var k = 0; k < cbs.length; k++) {
                    if (!cbs[k].disabled) {
                        cbs[k].click();
                        cbs[k].dispatchEvent(new MouseEvent('click', {bubbles:true}));
                    }
                }
            })()
        """)

        # 等待验证结果
        for _ in range(6):
            time.sleep(1)
            if _js(sb, _ALTCHA_SOLVED_JS):
                print(f"✅ ALTCHA 验证通过（第 {attempt + 1} 轮）")
                return True

        print(f"  ⚠️ 第 {attempt + 1} 轮未通过，重试...")
        # 重新获取坐标（iframe 可能已重新渲染）
        try:
            new_coords = _js(sb, _ALTCHA_EXPAND_JS)
            if new_coords:
                coords = new_coords
        except Exception:
            pass

    print("  ❌ ALTCHA 3 轮均失败")
    _save_artifacts(sb, "altcha_fail")
    return False


def _submit_renew(sb):
    """点击模态框内的 Renew 提交按钮"""
    print("🖱️  点击模态框中的 Renew 按钮...")
    try:
        submit = sb.find_element('div.modal.show button.btn-primary', timeout=5)
        submit.click()
    except Exception:
        sb.execute_script("""
            (function(){
                var m = document.querySelector('div.modal.show');
                if (!m) return;
                var bs = m.querySelectorAll('button');
                for (var i = 0; i < bs.length; i++)
                    if (/renew/i.test(bs[i].textContent)) bs[i].click();
            })()
        """)
    time.sleep(3)


def _check_renew_result(sb, email=EMAIL) -> str:
    """读取页面 alert 提示，判断续期结果并推送 TG 通知。

    返回 "ok"（续期成功）/ "skip"（未到续期时间）/ "error"（无法确认结果）。
    """
    print("\n📋 检查续期结果...")
    alert_text = _read_alert(sb)
    if not alert_text:
        time.sleep(3)
        alert_text = _read_alert(sb)

    if not alert_text:
        print("❌ 未检测到任何提示框，无法确认续期是否生效")
        _save_artifacts(sb, "renew_no_alert")
        return "error"

    print(f"📩 页面提示: {alert_text}")
    low = alert_text.lower()
    if "can't renew" in low or "unable" in low:
        send_tg_message("⏳", "未到续期时间", alert_text, email=email)
        return "skip"
    if any(kw in low for kw in ("renewed", "success", "extended")):
        send_tg_message("✅", "续期成功", alert_text, email=email)
        return "ok"
    print("❌ 提示内容无法判定为成功")
    _save_artifacts(sb, "renew_unknown_alert")
    send_tg_message("❌", "续期结果异常", alert_text, email=email)
    return "error"


def renew_server(sb, email=EMAIL) -> str:
    """登录成功后调用：自动进入详情页 -> Renew -> ALTCHA -> 提交。

    返回 "ok" / "skip"；出错时抛 RenewError 中断整个流程。
    """
    print("\n" + "#" * 25)
    print("  开始自动续期流程")
    print("#" * 25)

    # 返回 False 表示页面顶部已提示「还没到续期时间」，属正常情况
    if not _goto_server_detail(sb, email=email):
        return "skip"

    _open_renew_modal(sb)

    if not _solve_altcha(sb):
        raise RenewError("续期弹窗的 ALTCHA 人机验证未通过")

    _submit_renew(sb)
    status = _check_renew_result(sb, email=email)
    if status == "error":
        raise RenewError("已提交 Renew，但页面未返回可识别的成功提示")
    return status


#  脚本执行入口 (可选代理)
def process_account(sb, user):
    """处理单个账号的登录与续期。

    返回 (login_ok, status)，status 为 "ok" / "skip" / ""（登录失败时）。
    续期环节出错会抛 RenewError。
    """
    email = user.get("username") or user.get("email") or ""
    password = user.get("password") or ""
    print("#" * 25)
    print(f"   处理账号: {email}")
    print("#" * 25)
    if not email or not password:
        raise RenewError(f"账号配置缺少 username 或 password: {email or '(空)'}")

    if not login(sb, email, password):
        print(f"\n❌ 账号 {email} 登录失败。")
        return False, ""

    return True, renew_server(sb, email=email)


def main():
    print("#" * 25)
    print("   katabump 自动登录续期")
    print("#" * 25)

    users = load_users()
    if not users:
        raise RenewError("未配置任何账号（USERS_JSON / KATABUMP_EMAIL）")
    print(f"📋 共读取到 {len(users)} 个账号")

    sub_url = os.environ.get("SUB_URL") or ""
    is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    proxy_str = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1081"

    # 代理模式决策: SUB_URL 优先 → IS_PROXY → 直连
    use_mihomo = False
    proxy_pool = []
    proxy_index = 0
    max_retries = 1

    if sub_url:
        if setup_mihomo(sub_url):
            use_mihomo = True
            all_nodes = get_mihomo_proxies()
            if all_nodes:
                proxy_pool = test_mihomo_proxies(all_nodes)
                if proxy_pool:
                    import random
                    random.shuffle(proxy_pool)
                    _mihomo_log(f"可用节点: {len(proxy_pool)}")
                    max_retries = min(5, len(proxy_pool))
                else:
                    _mihomo_log("⚠️ 无健康节点，使用直连")
            else:
                _mihomo_log("⚠️ 无节点可用，使用直连")
        else:
            print("⚠️ Mihomo 启动失败，回退直连")
    elif is_proxy:
        print(f"🔗 挂载单代理: {proxy_str}")
        max_retries = 3
    else:
        print("🌐 未使用代理，直连访问")

    stats = {"processed": 0, "success": 0, "skipped": 0}
    total = len(users)

    def run_account(idx, user, email):
        """跑单个账号（含换节点重试），返回 "ok" / "skip"；失败抛 RenewError"""
        nonlocal proxy_index

        for attempt in range(max_retries):
            if attempt > 0:
                print(f"\n  [重试 {attempt + 1}/{max_retries}]")

            # 切换代理节点
            if use_mihomo and proxy_pool:
                node_name = proxy_pool[proxy_index % len(proxy_pool)]
                proxy_index += 1
                switch_mihomo_proxy(node_name)
                time.sleep(1)

            # 构建浏览器参数
            sb_kwargs = {"uc": True, "headless": False}
            if use_mihomo:
                sb_kwargs["proxy"] = f"http://127.0.0.1:{MIHOMO_MIXED_PORT}"
            elif is_proxy:
                sb_kwargs["proxy"] = proxy_str

            # 出口 IP 用 requests 走同一个代理查，不要用浏览器:
            # UC 模式下 sb.open() 会把驱动切进 CDP 模式并断开连接，
            # 之后 execute_script 的语义、execute_cdp_cmd 的可用性全变，
            # 白白给后面的验证码环节埋坑。
            print(f"  📍 出口IP: {_check_exit_ip(sb_kwargs.get('proxy'))}")

            with SB(**sb_kwargs) as sb:
                # 不注入任何 prototype 钩子: 改写 Element.prototype.attachShadow /
                # MouseEvent.prototype.screenX 会让 toString() 不再是 [native code]，
                # 属于 Cloudflare 一眼就能识别的篡改特征，而且对定位 widget 毫无帮助
                # （widget 的 checkbox 在跨域 iframe 内部，不在顶层 shadow root 里）。
                # 窗口略高一些，保证 Turnstile widget 落在视口内（CDP 点击用视口坐标）
                try:
                    sb.set_window_size(1280, 900)
                except Exception:
                    pass

                # 只有「登录失败」才换节点重试；其它异常留档后直接上抛，
                # 由 main 记为该账号失败，继续处理下一个账号。
                try:
                    login_ok, status = process_account(sb, user)
                except RenewError:
                    raise
                except Exception as e:
                    print("  ❌ 执行过程中出现未预期异常:")
                    traceback.print_exc()
                    try:
                        _save_artifacts(sb, f"exception_{idx}")
                    except Exception:
                        pass
                    raise RenewError(f"未预期异常: {e}")

                if login_ok:
                    return status
                if attempt == max_retries - 1:
                    send_tg_message("❌", "登录失败", f"{max_retries} 次尝试均未成功",
                                    email=email)
                    raise RenewError(f"{max_retries} 次尝试后仍然登录失败")

    failures = []
    try:
        # 所有账号都要跑完，单个账号失败只记录，不影响后面的账号；
        # 最终按整体失败情况决定退出码。
        for idx, user in enumerate(users, start=1):
            email = user.get("username") or user.get("email") or ""
            print(f"\n{'=' * 25}")
            print(f"  [{idx}/{total}] {email}")
            print(f"{'=' * 25}")

            stats["processed"] += 1
            try:
                status = run_account(idx, user, email)
            except RenewError as e:
                print(f"  ❌ {email} 处理失败: {e}")
                failures.append(f"{email}: {e}")
                continue

            if status == "skip":
                stats["skipped"] += 1
                print(f"  ⏳ {email} 未到续期时间，跳过")
            else:
                stats["success"] += 1
                print(f"  ✅ {email} 续期完成")
    finally:
        # 无论成功失败都打印汇总并清理 Mihomo
        failed = stats["processed"] - stats["success"] - stats["skipped"]
        pending = total - stats["processed"]
        print(f"\n{'=' * 40}")
        print(f"📊 总计 {total}, 成功 {stats['success']}, 跳过 {stats['skipped']}, "
              f"失败 {failed}, 未处理 {pending}")
        print(f"{'=' * 40}")
        if use_mihomo:
            stop_mihomo()

    # 全部账号跑完后，按最终失败情况决定是否中断（非零退出）
    if failures:
        raise RenewError(
            f"{len(failures)}/{total} 个账号失败:\n  - " + "\n  - ".join(failures))


if __name__ == "__main__":
    try:
        main()
    except RenewError as e:
        print(f"\n❌ 执行中断: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n⚠️ 已被手动中断")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ 未预期异常，执行中断: {e}")
        traceback.print_exc()
        sys.exit(1)
