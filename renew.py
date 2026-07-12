#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import json
import subprocess
from pathlib import Path
from enum import Enum

import requests
from seleniumbase import SB

TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
USERS_JSON = os.environ.get("USERS_JSON") or ""

BASE_URL = "https://dashboard.katabump.com"
REPORT_DIR = Path("output")
SCREENSHOT_DIR = REPORT_DIR / "screenshots"
TG_SAFE_LIMIT = 3500

REPORT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


class RenewStatus(Enum):
    LOGIN_FAILED = "login_failed"
    NOT_TIME = "not_time"
    RENEW_SUCCESS = "renew_success"
    RENEW_FAILED = "renew_failed"
    SCRIPT_ERROR = "script_error"


def load_users():
    if not USERS_JSON.strip():
        raise ValueError("未配置 USERS_JSON")

    try:
        users = json.loads(USERS_JSON)
    except json.JSONDecodeError as e:
        raise ValueError(f"USERS_JSON 不是合法 JSON: {e}")

    if not isinstance(users, list) or not users:
        raise ValueError("USERS_JSON 必须是非空数组")

    normalized = []
    for i, user in enumerate(users, start=1):
        if not isinstance(user, dict):
            raise ValueError(f"第 {i} 个账号不是对象")
        username = (user.get("username") or "").strip()
        password = user.get("password") or ""
        if not username or not password:
            raise ValueError(f"第 {i} 个账号缺少 username 或 password")
        normalized.append({
            "username": username,
            "password": password,
        })
    return normalized


def safe_name(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r'[^a-z0-9._-]+', '_', text)
    return text[:80] or "user"


def mask_email(email: str) -> str:
    if '@' in email:
        name, domain = email.split('@', 1)
        if len(name) > 4:
            return f"{name[:2]}****{name[-2:]}@{domain}"
        return f"{name}@{domain}"
    return (email[:2] + '****') if email else "****"


def now_bj():
    local_time = time.gmtime(time.time() + 8 * 3600)
    return time.strftime("%Y-%m-%d %H:%M:%S", local_time)


def save_shot(sb, filename: str):
    path = SCREENSHOT_DIR / filename
    sb.save_screenshot(str(path))
    print(f"📸 已保存截图: {path}")


def _send_tg_text_once(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 推送。")
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text
    }

    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            print("📩 Telegram 通知发送成功！")
        else:
            print(f"⚠️ Telegram 通知发送失败: {r.status_code} {r.text}")
    except Exception as e:
        print(f"⚠️ Telegram 通知发送异常: {e}")


def split_text_for_tg(text: str, limit: int = TG_SAFE_LIMIT):
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        cut = remaining.rfind("\n", 0, limit)
        if cut < int(limit * 0.6):
            cut = remaining.rfind(" ", 0, limit)
        if cut < int(limit * 0.6):
            cut = limit

        chunk = remaining[:cut].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            cut = limit

        chunks.append(chunk)
        remaining = remaining[cut:].lstrip()

    return chunks


def send_tg_text(text: str):
    parts = split_text_for_tg(text, TG_SAFE_LIMIT)
    total = len(parts)

    for idx, part in enumerate(parts, start=1):
        final_text = part if total == 1 else f"[{idx}/{total}]\n{part}"
        _send_tg_text_once(final_text)
        if idx < total:
            time.sleep(1.2)


def send_account_tg_message(user_email: str, status_icon: str, status_text: str, detail: str = ""):
    text = (
        f"🇫🇷 katabump 续期通知\n\n"
        f"{status_icon} {status_text}\n"
        f"👤 续期账户: {mask_email(user_email)}\n"
        f"⏱️ 续期时间: {now_bj()}"
    )
    if detail:
        text += f"\n📝 详情: {detail}"
    send_tg_text(text)


def build_summary_text(results: list):
    counters = {
        RenewStatus.RENEW_SUCCESS: 0,
        RenewStatus.NOT_TIME: 0,
        RenewStatus.LOGIN_FAILED: 0,
        RenewStatus.RENEW_FAILED: 0,
        RenewStatus.SCRIPT_ERROR: 0,
    }
    for item in results:
        counters[item["status"]] += 1

    lines = [
        "🇫🇷 katabump 多账号续期汇总",
        "",
        f"⏱️ 汇总时间: {now_bj()}",
        f"👥 总账号数: {len(results)}",
        f"✅ 续期成功: {counters[RenewStatus.RENEW_SUCCESS]}",
        f"⏳ 未到时间: {counters[RenewStatus.NOT_TIME]}",
        f"🔒 登录失败: {counters[RenewStatus.LOGIN_FAILED]}",
        f"⚠️ 续期失败: {counters[RenewStatus.RENEW_FAILED]}",
        f"💥 脚本异常: {counters[RenewStatus.SCRIPT_ERROR]}",
        "",
        "📋 明细:"
    ]

    status_icon_map = {
        RenewStatus.RENEW_SUCCESS: "✅",
        RenewStatus.NOT_TIME: "⏳",
        RenewStatus.LOGIN_FAILED: "🔒",
        RenewStatus.RENEW_FAILED: "⚠️",
        RenewStatus.SCRIPT_ERROR: "💥",
    }

    for i, item in enumerate(results, start=1):
        detail = item.get("message") or "无"
        lines.append(
            f"{i}. {status_icon_map[item['status']]} {mask_email(item['username'])} - "
            f"{item['status'].value} - {detail}"
        )

    return "\n".join(lines)


def send_summary_tg_message(results: list):
    send_tg_text(build_summary_text(results))


def write_report_files(results: list):
    counters = {
        RenewStatus.RENEW_SUCCESS: 0,
        RenewStatus.NOT_TIME: 0,
        RenewStatus.LOGIN_FAILED: 0,
        RenewStatus.RENEW_FAILED: 0,
        RenewStatus.SCRIPT_ERROR: 0,
    }
    for item in results:
        counters[item["status"]] += 1

    json_report = {
        "generated_at": now_bj(),
        "total": len(results),
        "summary": {
            "renew_success": counters[RenewStatus.RENEW_SUCCESS],
            "not_time": counters[RenewStatus.NOT_TIME],
            "login_failed": counters[RenewStatus.LOGIN_FAILED],
            "renew_failed": counters[RenewStatus.RENEW_FAILED],
            "script_error": counters[RenewStatus.SCRIPT_ERROR],
        },
        "results": [
            {
                "username": item["username"],
                "masked_username": mask_email(item["username"]),
                "status": item["status"].value,
                "message": item.get("message", ""),
            }
            for item in results
        ]
    }

    txt_report = build_summary_text(results)

    json_path = REPORT_DIR / "katabump_report.json"
    txt_path = REPORT_DIR / "katabump_report.txt"

    json_path.write_text(json.dumps(json_report, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text(txt_report, encoding="utf-8")

    print(f"📄 已生成报告文件: {json_path}")
    print(f"📄 已生成报告文件: {txt_path}")
    return [str(json_path), str(txt_path)]


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
            f.style.width = '300px';
            f.style.height = '65px';
            f.style.minWidth = '300px';
            f.style.visibility = 'visible';
            f.style.opacity = '1';
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

_ALTCHA_SOLVED_JS = """
(function(){
    var modal = document.querySelector('div.modal.show') || document;
    var inputs = modal.querySelectorAll('input[type="hidden"]');
    for (var i = 0; i < inputs.length; i++) {
        var n = (inputs[i].name || '').toLowerCase();
        if ((n.includes('altcha') || n.includes('captcha')) &&
            inputs[i].value && inputs[i].value.length > 20) return true;
    }
    var cbs = modal.querySelectorAll('input[type="checkbox"]');
    for (var j = 0; j < cbs.length; j++) {
        if (cbs[j].disabled) return true;
    }
    var w = modal.querySelector('[data-state="verified"],.altcha--verified,.altcha-verified');
    if (w) return true;
    return false;
})()
"""


def js_fill_input(sb, selector: str, text: str):
    safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
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
            r = subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--class", cls],
                capture_output=True,
                text=True,
                timeout=3
            )
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(
                    ["xdotool", "windowactivate", "--sync", wids[0]],
                    timeout=3,
                    stderr=subprocess.DEVNULL
                )
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(
            ["xdotool", "getactivewindow", "windowactivate"],
            timeout=3,
            stderr=subprocess.DEVNULL
        )
    except Exception:
        pass


def _xdotool_click(x: int, y: int):
    _activate_window()
    try:
        subprocess.run(
            ["xdotool", "mousemove", "--sync", str(x), str(y)],
            timeout=3,
            stderr=subprocess.DEVNULL
        )
        time.sleep(0.15)
        subprocess.run(
            ["xdotool", "click", "1"],
            timeout=2,
            stderr=subprocess.DEVNULL
        )
    except Exception:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")


def open_login_page(sb):
    print(f"🌐 打开登录页面: {BASE_URL}/auth/login")
    mode = "uc"
    try:
        sb.activate_cdp_mode(BASE_URL + "/auth/login")
        mode = "cdp"
        print("✅ 已切换到 CDP Mode")
    except Exception as e:
        print(f"⚠️ CDP Mode 打开失败，回退 UC Mode: {e}")
        sb.uc_open_with_reconnect(BASE_URL + "/auth/login", reconnect_time=8)
    time.sleep(6)
    return mode


def handle_turnstile(sb, mode="uc") -> bool:
    print(f"🔍 处理 Cloudflare Turnstile 验证... (mode={mode})")
    time.sleep(2)

    if sb.execute_script(_SOLVED_JS):
        print("✅ 已静默通过")
        return True

    for _ in range(3):
        try:
            sb.execute_script(_EXPAND_JS)
        except Exception:
            pass
        time.sleep(0.5)

    cdp_selectors = [
        "#turnstile-widget div",
        "iframe[src*='challenges.cloudflare.com']",
        "div.cf-turnstile",
        "[name='cf-turnstile-response']",
    ]

    for attempt in range(6):
        if sb.execute_script(_SOLVED_JS):
            print(f"✅ Turnstile 通过（第 {attempt + 1} 次尝试前检测）")
            return True

        if mode == "cdp":
            print(f"🧭 第 {attempt + 1} 次尝试 CDP 点击...")
            for sel in cdp_selectors:
                try:
                    sb.cdp.gui_click_element(sel)
                    print(f"✅ CDP 点击选择器成功: {sel}")
                    break
                except Exception as e:
                    print(f"⚠️ CDP 点击失败 {sel}: {e}")

            for _ in range(8):
                time.sleep(0.5)
                if sb.execute_script(_SOLVED_JS):
                    print(f"✅ Turnstile 通过（CDP，第 {attempt + 1} 次）")
                    return True

        print(f"🖱️ 第 {attempt + 1} 次调用 uc_gui_click_captcha...")
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"⚠️ uc_gui_click_captcha 调用异常: {e}")

        for _ in range(8):
            time.sleep(0.5)
            if sb.execute_script(_SOLVED_JS):
                print(f"✅ Turnstile 通过（click，第 {attempt + 1} 次）")
                return True

        print(f"⌨️ 第 {attempt + 1} 次调用 uc_gui_handle_captcha...")
        try:
            sb.uc_gui_handle_captcha()
        except Exception as e:
            print(f"⚠️ uc_gui_handle_captcha 调用异常: {e}")

        for _ in range(8):
            time.sleep(0.5)
            if sb.execute_script(_SOLVED_JS):
                print(f"✅ Turnstile 通过（handle，第 {attempt + 1} 次）")
                return True

        print(f"⚠️ 第 {attempt + 1} 次未通过，重试...")

    print("❌ Turnstile 6 次均失败")
    return False


def login(sb, user) -> bool:
    username = user["username"]
    password = user["password"]
    file_tag = safe_name(username)

    mode = open_login_page(sb)

    print("⏳ 等待 Cloudflare 验证通过...")
    cf_passed = False
    for i in range(30):
        page_src = sb.get_page_source() or ""
        if 'input[name="email"]' in page_src.lower() or 'name="email"' in page_src.lower():
            cf_passed = True
            print(f"✅ Cloudflare 验证已通过或页面已显示表单（{i + 1}s）")
            break
        time.sleep(1)

    if not cf_passed:
        print("⚠️ Cloudflare 验证可能未通过，继续尝试...")

    try:
        sb.wait_for_element('input[name="email"]', timeout=15)
    except Exception:
        try:
            sb.wait_for_element('input[name="Email"]', timeout=5)
        except Exception:
            print("❌ 页面未加载出登录表单")
            print(f"当前 URL: {sb.get_current_url()}")
            print(f"当前标题: {sb.get_title() or ''}")
            save_shot(sb, f"login_load_fail_{file_tag}.png")
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

    print(f"📧 填写邮箱: {mask_email(username)}")
    try:
        if mode == "cdp":
            sb.cdp.gui_click_element('input[name="email"]')
    except Exception:
        pass
    js_fill_input(sb, 'input[name="email"]', username)
    time.sleep(0.3)

    print("🔑 填写密码...")
    try:
        if mode == "cdp":
            sb.cdp.gui_click_element('input[name="password"]')
    except Exception:
        pass
    js_fill_input(sb, 'input[name="password"]', password)
    time.sleep(1)

    print("⏳ 等待 Turnstile 验证框出现...")
    ts_found = False
    for i in range(10):
        if sb.execute_script(_EXISTS_JS):
            ts_found = True
            print(f"✅ 检测到 Turnstile（{i + 1}s）")
            break
        time.sleep(1)

    if ts_found:
        if not handle_turnstile(sb, mode=mode):
            print("❌ 登录界面的 Turnstile 验证失败")
            save_shot(sb, f"login_turnstile_fail_{file_tag}.png")
            return False
    else:
        print("ℹ️ 未检测到 Turnstile")

    print("🖱️ 提交登录表单...")
    submitted = False

    if mode == "cdp":
        submit_selectors = [
            'button[type="submit"]',
            'form button[type="submit"]',
            'button.btn-primary',
        ]
        for sel in submit_selectors:
            try:
                sb.cdp.gui_click_element(sel)
                print(f"✅ 通过 CDP 点击提交按钮: {sel}")
                submitted = True
                break
            except Exception as e:
                print(f"⚠️ CDP 提交点击失败 {sel}: {e}")

    if not submitted:
        try:
            sb.press_keys('input[name="password"]', '\n')
            submitted = True
        except Exception as e:
            print(f"⚠️ 回车提交失败: {e}")

    print("⏳ 等待登录跳转...")
    for _ in range(15):
        time.sleep(1)
        cur_url = sb.get_current_url().split('?')[0].lower()
        page_title = sb.get_title() or ""
        if cur_url.startswith(f"{BASE_URL}/dashboard") or "dashboard" in page_title.lower():
            break

    cur_url = sb.get_current_url().split('?')[0].lower()
    page_title = sb.get_title() or ""

    if cur_url.startswith(f"{BASE_URL}/dashboard") or "dashboard" in page_title.lower():
        print(f"✅ 登录成功！(URL: {sb.get_current_url()}, Title: {page_title})")
        return True

    print(f"❌ 登录失败，页面未跳转到账户页。(URL: {sb.get_current_url()}, Title: {page_title})")
    save_shot(sb, f"login_failed_{file_tag}.png")
    return False


def _read_alert(sb):
    try:
        el = sb.find_element("div.alert", timeout=4)
        return (el.text or "").strip()
    except Exception:
        return ""


def _goto_server_detail(sb, user):
    print("\n🖥️ 正在进入服务器续期页...")
    time.sleep(5)

    alert_text = _read_alert(sb)
    if alert_text and "can't renew" in alert_text.lower():
        print(f"ℹ️ 页面顶部提示: {alert_text}")
        return {
            "status": RenewStatus.NOT_TIME,
            "message": alert_text,
        }

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
        print("❌ 未找到 'See' 链接")
        print(f"当前 URL: {sb.get_current_url()}")
        print(f"页面标题: {sb.get_title() or ''}")
        save_shot(sb, f"servers_page_fail_{safe_name(user['username'])}.png")
        return {
            "status": RenewStatus.RENEW_FAILED,
            "message": "未找到服务器详情入口 See 链接",
        }

    print("🖱️ 点击 'See' 进入服务器详情页...")
    see_link.click()
    time.sleep(5)
    print(f"📄 当前页面: {sb.get_current_url()}")
    return None


def _open_renew_modal(sb):
    print("\n🔄 查找 Renew 按钮...")
    try:
        renew_btn = sb.find_element('button[data-bs-target="#renew-modal"]', timeout=10)
    except Exception:
        try:
            renew_btn = sb.find_element('button.btn.btn-outline-primary', timeout=5)
        except Exception:
            print("❌ 未找到 Renew 按钮")
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
    print("\n🔐 处理 ALTCHA 人机验证...")
    time.sleep(2)

    if sb.execute_script(_ALTCHA_SOLVED_JS):
        print("✅ ALTCHA 已自动通过")
        return True

    coords = None
    try:
        coords = sb.execute_script(_ALTCHA_EXPAND_JS)
    except Exception:
        pass

    if coords:
        print(f"📍 找到模态框内 iframe 坐标: ({coords['cx']}, {coords['cy']})")

    for attempt in range(3):
        if sb.execute_script(_ALTCHA_SOLVED_JS):
            print(f"✅ ALTCHA 验证通过（第 {attempt + 1} 轮）")
            return True

        if coords:
            try:
                wi = sb.execute_script(_WININFO_JS)
            except Exception:
                wi = {"sx": 0, "sy": 0, "oh": 800, "ih": 768}
            bar = wi["oh"] - wi["ih"]
            ax = coords["cx"] + wi["sx"]
            ay = coords["cy"] + wi["sy"] + bar
            print(f"🖱️ ALTCHA点击复选框 ({ax}, {ay})")
            _xdotool_click(ax, ay)

        try:
            iframes = sb.find_elements('div.modal.show iframe')
            for iframe in iframes:
                try:
                    iframe.click()
                    print("🖱️ SeleniumBase 点击模态框 iframe")
                except Exception:
                    pass
        except Exception:
            pass

        sb.execute_script("""
            (function(){
                var modal = document.querySelector('div.modal.show');
                if (!modal) return;
                var iframes = modal.querySelectorAll('iframe');
                for (var i = 0; i < iframes.length; i++) {
                    iframes[i].click();
                    iframes[i].dispatchEvent(new MouseEvent('click', {bubbles:true}));
                }
                var labels = modal.querySelectorAll('label');
                for (var j = 0; j < labels.length; j++) {
                    var txt = (labels[j].textContent || '').toLowerCase();
                    if (txt.includes('robot') || txt.includes('captcha') || txt.includes('verify'))
                        labels[j].click();
                }
                var cbs = modal.querySelectorAll('input[type="checkbox"]');
                for (var k = 0; k < cbs.length; k++) {
                    if (!cbs[k].disabled) {
                        cbs[k].click();
                        cbs[k].dispatchEvent(new MouseEvent('click', {bubbles:true}));
                    }
                }
            })()
        """)

        for _ in range(6):
            time.sleep(1)
            if sb.execute_script(_ALTCHA_SOLVED_JS):
                print(f"✅ ALTCHA 验证通过（第 {attempt + 1} 轮）")
                return True

        print(f"⚠️ 第 {attempt + 1} 轮未通过，重试...")
        try:
            new_coords = sb.execute_script(_ALTCHA_EXPAND_JS)
            if new_coords:
                coords = new_coords
        except Exception:
            pass

    print("❌ ALTCHA 3 轮均失败")
    return False


def _submit_renew(sb):
    print("🖱️ 点击模态框中的 Renew 按钮...")
    try:
        submit = sb.find_element('div.modal.show button.btn-primary', timeout=5)
        submit.click()
    except Exception:
        sb.execute_script("""
            (function(){
                var m = document.querySelector('div.modal.show');
                if (!m) return;
                var bs = m.querySelectorAll('button');
                for (var i = 0; i < bs.length; i++) {
                    if (/renew/i.test(bs[i].textContent)) {
                        bs[i].click();
                    }
                }
            })()
        """)
    time.sleep(3)


def _check_renew_result(sb):
    print("\n📋 检查续期结果...")
    alert_text = _read_alert(sb)
    if not alert_text:
        time.sleep(3)
        alert_text = _read_alert(sb)

    if not alert_text:
        print("ℹ️ 未检测到明确的提示框，可能续期操作未生效")
        return {
            "status": RenewStatus.RENEW_FAILED,
            "message": "未检测到明确提示框",
        }

    print(f"📩 页面提示: {alert_text}")
    low = alert_text.lower()

    if "can't renew" in low or "unable" in low:
        return {
            "status": RenewStatus.NOT_TIME,
            "message": alert_text,
        }

    if any(kw in low for kw in ("renewed", "success", "extended")):
        return {
            "status": RenewStatus.RENEW_SUCCESS,
            "message": alert_text,
        }

    return {
        "status": RenewStatus.RENEW_FAILED,
        "message": alert_text,
    }


def renew_server(sb, user):
    print("\n" + "#" * 25)
    print(f"开始自动续期流程: {mask_email(user['username'])}")
    print("#" * 25)

    precheck = _goto_server_detail(sb, user)
    if precheck is not None:
        return precheck

    if not _open_renew_modal(sb):
        return {
            "status": RenewStatus.RENEW_FAILED,
            "message": "未能打开 Renew 模态框",
        }

    altcha_ok = _solve_altcha(sb)
    if not altcha_ok:
        print("⚠️ ALTCHA 验证未通过，仍尝试提交 Renew...")

    _submit_renew(sb)
    result = _check_renew_result(sb)

    if not altcha_ok and result["status"] == RenewStatus.RENEW_FAILED:
        result["message"] = f"ALTCHA 可能未通过；{result['message']}"

    return result


def run_for_user(user, sb_kwargs):
    username = user["username"]

    print("\n" + "=" * 60)
    print(f"👤 开始处理账号: {mask_email(username)}")
    print("=" * 60)

    with SB(**sb_kwargs) as sb:
        try:
            sb.open("https://api.ip.sb/ip")
            print(f"📍 当前出口IP: {sb.get_text('body')}")
        except Exception:
            pass

        if not login(sb, user):
            result = {
                "username": username,
                "status": RenewStatus.LOGIN_FAILED,
                "message": "登录失败",
            }
            send_account_tg_message(username, "🔒", "登录失败", result["message"])
            return result

        renew_result = renew_server(sb, user)
        result = {
            "username": username,
            "status": renew_result["status"],
            "message": renew_result.get("message", ""),
        }

        icon_map = {
            RenewStatus.RENEW_SUCCESS: "✅",
            RenewStatus.NOT_TIME: "⏳",
            RenewStatus.LOGIN_FAILED: "🔒",
            RenewStatus.RENEW_FAILED: "⚠️",
            RenewStatus.SCRIPT_ERROR: "💥",
        }
        text_map = {
            RenewStatus.RENEW_SUCCESS: "续期成功",
            RenewStatus.NOT_TIME: "未到续期时间",
            RenewStatus.LOGIN_FAILED: "登录失败",
            RenewStatus.RENEW_FAILED: "续期失败",
            RenewStatus.SCRIPT_ERROR: "脚本异常",
        }

        send_account_tg_message(
            username,
            icon_map[result["status"]],
            text_map[result["status"]],
            result["message"]
        )
        return result


def main():
    print("#" * 25)
    print("katabump 多账号自动登录续期（CDP优先）")
    print("#" * 25)

    users = load_users()
    print(f"📦 共读取到 {len(users)} 个账号")

    is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    proxy_str = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1081"

    sb_kwargs = {
        "uc": True,
        "headless": False,
        "xvfb": True,
        "xvfb_metrics": "1366,768",
    }

    if is_proxy:
        print(f"🔗 挂载代理: {proxy_str}")
        sb_kwargs["proxy"] = proxy_str
    else:
        print("🌐 未使用代理，直连访问")

    results = []

    for idx, user in enumerate(users, start=1):
        print(f"\n🚀 开始第 {idx}/{len(users)} 个账号")
        try:
            result = run_for_user(user, sb_kwargs)
        except Exception as e:
            result = {
                "username": user["username"],
                "status": RenewStatus.SCRIPT_ERROR,
                "message": str(e),
            }
            print(f"❌ 账号执行异常: {mask_email(user['username'])} -> {e}")
            send_account_tg_message(user["username"], "💥", "脚本执行异常", str(e))

        results.append(result)

    print("\n" + "#" * 25)
    print("全部账号处理完成")
    for item in results:
        print(f"- {mask_email(item['username'])}: {item['status'].value} | {item['message']}")
    print("#" * 25)

    report_files = write_report_files(results)
    send_summary_tg_message(results)
    print(f"📦 报告文件已生成: {report_files}")


if __name__ == "__main__":
    main()
