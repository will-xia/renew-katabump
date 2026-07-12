#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import subprocess
import json
import requests
from seleniumbase import SB

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
_EXPAND_JS = """
(function() {
    var ts = document.querySelector('input[name="cf-turnstile-response"]');
    if (!ts) return 'no-turnstile';
    var el = ts;
    for (var i = 0; i < 20; i++) {
        el = el.parentElement;
        if (!el) break;
        var s = window.getComputedStyle(el);
        if (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.overflowY === 'hidden')
            el.style.overflow = 'visible';
        el.style.minWidth = 'max-content';
    }
    document.querySelectorAll('iframe').forEach(function(f){
        if (f.src && f.src.includes('challenges.cloudflare.com')) {
            f.style.width = '300px'; f.style.height = '65px';
            f.style.minWidth = '300px';
            f.style.visibility = 'visible'; f.style.opacity = '1';
        }
    });
    return 'done';
})()
"""

_EXISTS_JS = """
(function(){
    return document.querySelector('input[name="cf-turnstile-response"]') !== null;
})()
"""

_SOLVED_JS = """
(function(){
    var i = document.querySelector('input[name="cf-turnstile-response"]');
    return !!(i && i.value && i.value.length > 20);
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

# 定位 Turnstile iframe 并返回其中心坐标（视口坐标）
_TURNSTILE_COORDS_JS = """
(function() {
    var iframes = document.querySelectorAll('iframe');
    // 优先匹配 challenges.cloudflare.com 的 iframe
    for (var i = 0; i < iframes.length; i++) {
        var f = iframes[i];
        var r = f.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
            var src = f.src || '';
            var cls = f.className || '';
            if (src.indexOf('challenges.cloudflare.com') >= 0 || cls.indexOf('cf-turnstile') >= 0) {
                // 把 widget 滚入视口，确保点击命中
                f.scrollIntoView({behavior: 'auto', block: 'center'});
                var r2 = f.getBoundingClientRect();
                return { cx: Math.round(r2.x + r2.width / 2), cy: Math.round(r2.y + r2.height / 2) };
            }
        }
    }
    // 回退: 任意可见 iframe（取最后一个）
    for (var j = iframes.length - 1; j >= 0; j--) {
        var rr = iframes[j].getBoundingClientRect();
        if (rr.width > 0 && rr.height > 0) {
            iframes[j].scrollIntoView({behavior: 'auto', block: 'center'});
            var rr2 = iframes[j].getBoundingClientRect();
            return { cx: Math.round(rr2.x + rr2.width / 2), cy: Math.round(rr2.y + rr2.height / 2) };
        }
    }
    return null;
})()
"""

# 注入脚本: Hook Shadow DOM，捕获 Turnstile checkbox 在其 iframe 内的位置比例。
# 参考成熟方案: 通过 CDP Input.dispatchMouseEvent 派发受信任鼠标事件，
# 无需可见/聚焦窗口即可在 CI 中命中 Shadow DOM 内的 checkbox。
_TURNSTILE_HOOK_JS = """
(function() {
    // 注意: 不要跳过顶层框架。部分站点把 Turnstile checkbox 渲染在
    // 主文档自身的 Shadow DOM（<div class="cf-turnstile"> 内），而非跨域 iframe，
    // 因此顶层框架也必须 hook attachShadow 才能捕获位置。
    try {
        function getRandomInt(min, max){return Math.floor(Math.random()*(max-min+1))+min;}
        var sx = getRandomInt(800,1200), sy = getRandomInt(400,600);
        Object.defineProperty(MouseEvent.prototype,'screenX',{value:sx});
        Object.defineProperty(MouseEvent.prototype,'screenY',{value:sy});
    } catch(e){}
    try {
        var orig = Element.prototype.attachShadow;
        Element.prototype.attachShadow = function(init){
            var sr = orig.call(this, init);
            if (sr) {
                var report = function(){
                    var cb = sr.querySelector('input[type="checkbox"]');
                    if (cb) {
                        var r = cb.getBoundingClientRect();
                        if (r.width>0 && r.height>0 && window.innerWidth>0 && window.innerHeight>0) {
                            window.__turnstile_data = {
                                xRatio:(r.left + r.width/2)/window.innerWidth,
                                yRatio:(r.top + r.height/2)/window.innerHeight
                            };
                            return true;
                        }
                    }
                    return false;
                };
                if (!report()) {
                    var ob = new MutationObserver(function(){ if(report()) ob.disconnect(); });
                    ob.observe(sr,{childList:true,subtree:true});
                }
            }
            return sr;
        };
    } catch(e){}
})()
"""


def _cdp_click(sb, x, y):
    """通过 CDP 派发受信任鼠标事件（无需聚焦/可见窗口，headless CI 可用）"""
    try:
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


def _scroll_turnstile_into_view(sb):
    """把页面上所有 iframe 滚入视口中心（不依赖 src 关键字）"""
    try:
        sb.execute_script(
            "document.querySelectorAll('iframe').forEach(function(f){try{f.scrollIntoView({block:'center'});}catch(e){}});"
        )
    except Exception:
        pass


def _find_turnstile_target(sb):
    """遍历所有 iframe，返回 (data, box)。

    data 来自 Turnstile 跨域 iframe 内注入的 window.__turnstile_data
    （Shadow DOM checkbox 的位置比例）。不按 src 关键字过滤，避免漏掉
    srcdoc / about:blank / 动态域名 形态的 widget。
    若没有任何 iframe 携带 data，则回退返回 (None, 第一个可见 iframe 的 box)。
    """
    best_box = None
    for iframe in sb.find_elements("iframe"):
        try:
            box = iframe.rect
        except Exception:
            box = {}
        # 跳过 Cloudflare 隐藏的 1x1 token iframe，避免无谓的跨域切换
        if box.get("width", 0) <= 2 and box.get("height", 0) <= 2:
            continue
        try:
            sb.driver.switch_to.frame(iframe)
            try:
                data = sb.execute_script("return window.__turnstile_data || null")
            finally:
                sb.driver.switch_to.default_content()
            if data and "xRatio" in data:
                return data, box
            if best_box is None and box.get("width", 0) > 0:
                best_box = box
        except Exception:
            try:
                sb.driver.switch_to.default_content()
            except Exception:
                pass
    if best_box is not None:
        return None, best_box
    return None, None


def _read_top_turnstile_coords(sb):
    """读取主文档(顶层框架)内 window.__turnstile_data，按视口尺寸算出点击坐标。
    适用于 checkbox 渲染在主文档 Shadow DOM 的站点。"""
    try:
        res = sb.execute_script("""
            (function(){
                var d = window.__turnstile_data || null;
                if (!d || typeof d.xRatio !== 'number') return null;
                return {
                    x: d.xRatio * window.innerWidth,
                    y: d.yRatio * window.innerHeight
                };
            })()
        """)
        if res and "x" in res:
            return res["x"], res["y"]
    except Exception:
        pass
    return None


def _altcha_checkbox_coords(sb):
    """读取 ALTCHA widget Shadow DOM 内 checkbox 的视口坐标"""
    try:
        return sb.execute_script("""
            (function(){
                var w = document.querySelector('altcha-widget');
                if (!w) return null;
                var root = w.shadowRoot;
                var cb = root ? root.querySelector('input[type="checkbox"],[role="checkbox"]') : null;
                if (!cb) return null;
                cb.scrollIntoView({block:'center'});
                var r = cb.getBoundingClientRect();
                if (r.width>0 && r.height>0) return {x: r.x + r.width/2, y: r.y + r.height/2};
                return null;
            })()
        """)
    except Exception:
        return None


#  人机验证处理（CDP 受信任鼠标事件，精确点击 Shadow DOM 内 checkbox）
def handle_turnstile(sb) -> bool:
    print("🔍 处理 Cloudflare Turnstile 验证...")

    # 尝试展开 Turnstile（防止被父容器 overflow:hidden 裁剪）
    for _ in range(3):
        try: sb.execute_script(_EXPAND_JS)
        except Exception: pass
        time.sleep(0.5)

    # 阶段 1: 隐形 Turnstile 通常会自动注入 cf-turnstile-response，无需点击。
    # 先轮询等待 token 出现（最多 ~25 秒），这是 CI / 无显示环境下唯一可靠的通过方式。
    print("⏳ 等待 Turnstile 自动通过（隐形验证，无需点击）...")
    for i in range(25):
        if sb.execute_script(_SOLVED_JS):
            print(f"✅ Turnstile 已自动通过（{i+1}s）")
            return True
        time.sleep(1)

    # 阶段 2: 交互式挑战 —— 通过 CDP 派发受信任鼠标事件，
    # 精确点击 Shadow DOM 内的 checkbox（无需聚焦窗口，CI/headless 可用）。
    print("⚠️ 未自动通过，使用 CDP 真实点击 Turnstile checkbox...")
    for attempt in range(10):
        if sb.execute_script(_SOLVED_JS):
            print(f"✅ Turnstile 通过（第 {attempt} 次尝试）")
            return True

        # 重新展开并滚动到视口内（iframe 可能已重渲染）
        try: sb.execute_script(_EXPAND_JS)
        except Exception: pass
        try: _scroll_turnstile_into_view(sb)
        except Exception: pass

        # 优先: checkbox 在主文档 Shadow DOM（顶层框架）
        top = _read_top_turnstile_coords(sb)
        if top:
            print(f"🖱️ CDP 点击 Turnstile 主文档 checkbox ({top[0]:.1f},{top[1]:.1f}) [尝试 {attempt + 1}]")
            _cdp_click(sb, top[0], top[1])
        else:
            # 回退: checkbox 在跨域 iframe 内
            data, box = _find_turnstile_target(sb)
            if data and box:
                clickX = box["x"] + box["width"] * data["xRatio"]
                clickY = box["y"] + box["height"] * data["yRatio"]
                print(f"🖱️ CDP 点击 Turnstile ({clickX:.1f},{clickY:.1f}) [尝试 {attempt + 1}]")
                _cdp_click(sb, clickX, clickY)
            elif box:
                # 取不到内部比例时，回退为点击 iframe 中心
                print(f"🖱️ CDP 点击 Turnstile iframe 中心 [尝试 {attempt + 1}]")
                _cdp_click(sb, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                # 完全取不到坐标时回退到 SeleniumBase 内置点击
                print("⚠️ 未取得 Turnstile 坐标，回退 uc_gui_click_captcha [尝试 {0}]".format(attempt + 1))
                try:
                    sb.uc_gui_click_captcha()
                except Exception as e:
                    print(f"⚠️ uc_gui_click_captcha 调用异常: {e}")

        # 等待验证结果（最多 8 秒）
        for _ in range(16):
            time.sleep(0.5)
            if sb.execute_script(_SOLVED_JS):
                print(f"✅ Turnstile 通过（第 {attempt + 1} 次尝试）")
                return True

        print(f"⚠️ 第 {attempt + 1} 次未通过，重试...")

    # 诊断信息: 输出 token 长度与页面状态，便于后续排查
    print("  ❌ Turnstile 处理失败，输出诊断信息:")
    try:
        diag = sb.execute_script("""
            (function(){
                var i = document.querySelector('input[name="cf-turnstile-response"]');
                var info = {
                    has_input: !!i,
                    token_len: i ? (i.value || '').length : -1,
                    title: document.title
                };
                // 列出所有 iframe 的 src 与位置
                var iframes = document.querySelectorAll('iframe');
                info.iframe_count = iframes.length;
                info.iframes = [];
                for (var k = 0; k < iframes.length; k++) {
                    var f = iframes[k];
                    var r = f.getBoundingClientRect();
                    var src = f.src || f.getAttribute('src') || '(empty)';
                    info.iframes.push({
                        src: src.slice(0, 90),
                        x: Math.round(r.x), y: Math.round(r.y),
                        w: Math.round(r.width), h: Math.round(r.height)
                    });
                }
                // 每个 iframe 内是否设置了 __turnstile_data
                info.ts_data = [];
                return info;
            })()
        """)
        print(f"     Turnstile input 存在: {diag.get('has_input')}, token 长度: {diag.get('token_len')}, 标题: {diag.get('title')}")
        print(f"     iframe 数量: {diag.get('iframe_count')}")
        for f in (diag.get('iframes') or []):
            print(f"       iframe src={f['src']} rect=({f['x']},{f['y']},{f['w']}x{f['h']})")
        # 跨域读取每个 iframe 内的 __turnstile_data
        try:
            for iframe in sb.find_elements("iframe"):
                try:
                    sb.driver.switch_to.frame(iframe)
                    d = sb.execute_script("return window.__turnstile_data || null")
                finally:
                    sb.driver.switch_to.default_content()
                print(f"       iframe __turnstile_data = {d}")
        except Exception as e:
            print(f"     读取 iframe 内 data 异常: {e}")
    except Exception as e:
        print(f"     诊断获取异常: {e}")
    sb.save_screenshot("turnstile_fail.png")
    return False


#  账户登录
def login(sb, email: str, password: str) -> bool:
    print(f"🌐 打开登录页面: {BASE_URL}/auth/login")
    sb.uc_open_with_reconnect(BASE_URL + "/auth/login", reconnect_time=8)
    time.sleep(6)

    # 先等待 Cloudflare 验证通过（最多等 30 秒）
    print("⏳ 等待 Cloudflare 验证通过...")
    cf_passed = False
    for i in range(30):
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
            sb.save_screenshot("login_load_fail.png")
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

    # 等待 Turnstile 验证框出现（最多 10 秒）
    print("⏳ 等待 Turnstile 验证框出现...")
    ts_found = False
    for i in range(10):
        if sb.execute_script(_EXISTS_JS):
            ts_found = True
            print(f"✅ 检测到 Turnstile（{i+1}s）")
            break
        time.sleep(1)

    if ts_found:
        if not handle_turnstile(sb):
            print("❌ 登录界面的 Turnstile 验证失败")
            sb.save_screenshot("login_turnstile_fail.png")
            return False
    else:
        print("ℹ️ 未检测到 Turnstile")

    print("🖱️ 敲击回车提交表单...")
    sb.press_keys('input[name="password"]', '\n')

    print("⏳ 等待登录跳转...")
    for _ in range(12):
        time.sleep(1)
        cur_url = sb.get_current_url().split('?')[0].lower()
        page_title = sb.get_title() or ""
        if cur_url.startswith(f"{BASE_URL}/dashboard") or "Dashboard | KataBump" in page_title.lower():
            break

    cur_url = sb.get_current_url().split('?')[0].lower()
    page_title = sb.get_title() or ""
    if cur_url.startswith(f"{BASE_URL}/dashboard") or "Dashboard | KataBump" in page_title.lower():
        print(f"✅ 登录成功！(URL: {sb.get_current_url()}, Title: {page_title})")
        return True

    print(f"❌ 登录失败，页面未跳转到账户页。(URL: {sb.get_current_url()}, Title: {page_title})")
    sb.save_screenshot("login_failed.png")
    return False


# ===== 自动续期流程 =====

def _read_alert(sb):
    """读取页面第一个 Bootstrap alert 的文本，找不到返回空串"""
    try:
        el = sb.find_element("div.alert", timeout=4)
        return (el.text or "").strip()
    except Exception:
        return ""


def _goto_server_detail(sb) -> bool:
    """在 Dashboard 首页查找并点击 See 进入服务器详情页"""
    print("\n🖥️  正在进入服务器续期页...")
    time.sleep(5)

    # 检查页面顶部是否已有"还无法续期"全局提示
    alert_text = _read_alert(sb)
    if alert_text and "can't renew" in alert_text.lower():
        print(f"ℹ️  页面顶部提示: {alert_text}")
        send_tg_message("ℹ️", "⚠️ 未到续期时间", alert_text)
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
        sb.save_screenshot("servers_page_fail.png")
        return False

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
            return False

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
        return False


def _solve_altcha(sb) -> bool:
    """处理 ALTCHA 人机验证"""
    print("\n🔐 处理 ALTCHA 人机验证...")
    time.sleep(2)

    # 先检查是否已自动通过
    if sb.execute_script(_ALTCHA_SOLVED_JS):
        print("✅ ALTCHA 已自动通过")
        return True

    # 展开模态框内 iframe 并获取坐标
    coords = None
    try:
        coords = sb.execute_script(_ALTCHA_EXPAND_JS)
    except Exception:
        pass

    if coords:
        print(f"  📍 找到模态框内 iframe 坐标: ({coords['cx']}, {coords['cy']})")

    # 最多尝试 3 轮
    for attempt in range(3):
        if sb.execute_script(_ALTCHA_SOLVED_JS):
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
            try:
                wi = sb.execute_script(_WININFO_JS)
            except Exception:
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
            if sb.execute_script(_ALTCHA_SOLVED_JS):
                print(f"✅ ALTCHA 验证通过（第 {attempt + 1} 轮）")
                return True

        print(f"  ⚠️ 第 {attempt + 1} 轮未通过，重试...")
        # 重新获取坐标（iframe 可能已重新渲染）
        try:
            new_coords = sb.execute_script(_ALTCHA_EXPAND_JS)
            if new_coords:
                coords = new_coords
        except Exception:
            pass

    print("  ❌ ALTCHA 3 轮均失败")
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


def _check_renew_result(sb, email=EMAIL):
    """读取页面 alert 提示，判断续期结果并推送 TG 通知"""
    print("\n📋 检查续期结果...")
    alert_text = _read_alert(sb)
    if not alert_text:
        time.sleep(3)
        alert_text = _read_alert(sb)

    if alert_text:
        print(f"📩 页面提示: {alert_text}")
        low = alert_text.lower()
        if "can't renew" in low or "unable" in low:
            send_tg_message("⏳", "未到续期时间", alert_text, email=email)
        elif any(kw in low for kw in ( "renewed", "success", "extended")):
            send_tg_message("✅", "续期成功", alert_text, email=email)
        else:
            send_tg_message("ℹ️", "续期操作已执行", alert_text, email=email)
    else:
        print("ℹ️ 未检测到明确的提示框，可能续期操作未生效")
        send_tg_message("ℹ️", "续期操作已执行", "未检测到明确提示", email=email)


def renew_server(sb, email=EMAIL):
    """登录成功后调用：自动进入详情页 -> Renew -> ALTCHA -> 提交"""
    print("\n" + "#" * 25)
    print("  开始自动续期流程")
    print("#" * 25)

    if not _goto_server_detail(sb):
        return

    if not _open_renew_modal(sb):
        return

    altcha_ok = _solve_altcha(sb)
    if not altcha_ok:
        print("⚠️ ALTCHA 验证未通过，仍尝试提交 Renew...")

    _submit_renew(sb)
    _check_renew_result(sb, email=email)


#  脚本执行入口 (可选代理)
def process_account(sb, user):
    """处理单个账号的登录与续期"""
    email = user.get("username") or user.get("email") or ""
    password = user.get("password") or ""
    print("#" * 25)
    print(f"   处理账号: {email}")
    print("#" * 25)
    if not email or not password:
        print("⚠️ 账号缺少 username 或 password，跳过")
        return

    if login(sb, email, password):
        renew_server(sb, email=email)
    else:
        print(f"\n❌ 账号 {email} 登录失败，终止后续续期操作。")
        send_tg_message("❌", "登录失败", "未知", email=email)


def main():
    print("#" * 25)
    print("   katabump 自动登录续期")
    print("#" * 25)

    users = load_users()
    if not users:
        print("❌ 未配置任何账号（USERS_JSON / KATABUMP_EMAIL）。")
        return
    print(f"📋 共读取到 {len(users)} 个账号")

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    proxy_str = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1081"
    sb_kwargs = {"uc": True, "headless": False}

    if IS_PROXY:
        print(f"🔗 挂载代理: {proxy_str}")
        sb_kwargs["proxy"] = proxy_str
    else:
        print("🌐 未使用代理，直连访问")

    for idx, user in enumerate(users, start=1):
        print(f"\n{'=' * 25}")
        print(f"  [{idx}/{len(users)}] 开始处理账号")
        print(f"{'=' * 25}")
        # 每个账号使用独立浏览器会话，避免登录态串号
        with SB(**sb_kwargs) as sb:
            # 注入 Turnstile Shadow DOM 钩子（在每个新文档中生效，含跨域 iframe）
            try:
                sb.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _TURNSTILE_HOOK_JS})
                print("✅ 已注入 Turnstile Shadow DOM 钩子")
            except Exception as e:
                print(f"⚠️ 注入 Turnstile 钩子失败: {e}")
            # 固定视口，保证 CDP 点击坐标一致
            try:
                sb.set_window_size(1280, 720)
            except Exception:
                pass
            try:
                sb.open("https://api.ip.sb/ip")
                print(f"📍  当前出口IP: {sb.get_text('body')}")
            except Exception:
                pass
            try:
                process_account(sb, user)
            except Exception as e:
                print(f"⚠️ 账号处理异常: {e}")
                send_tg_message("⚠️", "处理异常", str(e),
                                email=user.get("username") or user.get("email") or "")
        print(f"[{idx}/{len(users)}] 账号处理完毕，关闭浏览器")


if __name__ == "__main__":
    main()
