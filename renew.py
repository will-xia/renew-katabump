#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import time
from enum import Enum
from pathlib import Path

import requests
from seleniumbase import SB

BASE_URL = "https://dashboard.katabump.com"

USERS_JSON = os.environ.get("USERS_JSON", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")

IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip()

REPORT_DIR = Path("output")
SCREENSHOT_DIR = REPORT_DIR / "screenshots"
REPORT_JSON = REPORT_DIR / "katabump_report.json"
REPORT_TXT = REPORT_DIR / "katabump_report.txt"

REPORT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


class RenewStatus(Enum):
    RENEW_SUCCESS = "renew_success"
    NOT_TIME = "not_time"
    LOGIN_FAILED = "login_failed"
    RENEW_FAILED = "renew_failed"
    NEEDS_MANUAL_VERIFICATION = "needs_manual_verification"
    SCRIPT_ERROR = "script_error"


def now_bj() -> str:
    return time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.gmtime(time.time() + 8 * 3600),
    )


def safe_name(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "_", value)
    return value[:80] or "unknown"


def mask_email(email: str) -> str:
    if "@" not in email:
        return (email[:2] + "****") if email else "****"

    name, domain = email.split("@", 1)
    if len(name) <= 4:
        return f"{name}@{domain}"
    return f"{name[:2]}****{name[-2:]}@{domain}"


def load_users() -> list[dict]:
    if not USERS_JSON.strip():
        raise ValueError("未配置 USERS_JSON")

    try:
        raw_users = json.loads(USERS_JSON)
    except json.JSONDecodeError as exc:
        raise ValueError(f"USERS_JSON 不是合法 JSON：{exc}") from exc

    if not isinstance(raw_users, list) or not raw_users:
        raise ValueError("USERS_JSON 必须是非空 JSON 数组")

    users = []
    for index, item in enumerate(raw_users, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个账号不是 JSON 对象")

        username = str(item.get("username") or item.get("email") or "").strip()
        password = str(item.get("password") or "")

        if not username or not password:
            raise ValueError(f"第 {index} 个账号缺少 username/email 或 password")

        users.append({
            "username": username,
            "password": password,
        })

    return users


def tg_send(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 未配置 Telegram，跳过通知。")
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=15,
        )
        if response.status_code != 200:
            print(f"⚠️ Telegram 发送失败：{response.status_code} {response.text}")
    except requests.RequestException as exc:
        print(f"⚠️ Telegram 请求异常：{exc}")


def tg_account(user: str, icon: str, title: str, detail: str = ""):
    text = (
        "🇫🇷 KataBump 续期通知\n\n"
        f"{icon} {title}\n"
        f"👤 账户：{mask_email(user)}\n"
        f"⏱️ 时间：{now_bj()}"
    )

    if detail:
        text += f"\n📝 详情：{detail[:1000]}"

    tg_send(text)


def save_screenshot(sb, user: str, label: str) -> str:
    filename = f"{safe_name(user)}_{label}_{int(time.time())}.png"
    path = SCREENSHOT_DIR / filename

    try:
        sb.save_screenshot(str(path))
        print(f"📸 截图已保存：{path}")
    except Exception as exc:
        print(f"⚠️ 保存截图失败：{exc}")

    return str(path)


def is_turnstile_present(sb) -> bool:
    script = """
    return Boolean(
        document.querySelector('input[name="cf-turnstile-response"]') ||
        document.querySelector('.cf-turnstile') ||
        document.querySelector('iframe[src*="challenges.cloudflare.com"]')
    );
    """
    try:
        return bool(sb.execute_script(script))
    except Exception:
        return False


def turnstile_has_token(sb) -> bool:
    script = """
    const input = document.querySelector('input[name="cf-turnstile-response"]');
    return Boolean(input && input.value && input.value.length > 20);
    """
    try:
        return bool(sb.execute_script(script))
    except Exception:
        return False


def wait_for_login_form_or_turnstile(sb, timeout: int = 30) -> str:
    for second in range(1, timeout + 1):
        try:
            has_email = sb.is_element_present('input[name="email"]')
        except Exception:
            has_email = False

        if has_email:
            return "login_form"

        if is_turnstile_present(sb):
            return "turnstile"

        time.sleep(1)

    return "timeout"


def close_cookie_banner(sb):
    selectors = [
        "button#onetrust-accept-btn-handler",
        "button[aria-label*='Accept']",
        "button[aria-label*='accept']",
    ]

    for selector in selectors:
        try:
            if sb.is_element_visible(selector):
                sb.click(selector)
                time.sleep(0.5)
                return
        except Exception:
            pass

    try:
        for button in sb.find_elements("button"):
            text = (button.text or "").strip().lower()
            if text in {"accept", "accept all", "allow all"}:
                button.click()
                time.sleep(0.5)
                return
    except Exception:
        pass


def js_fill_input(sb, selector: str, value: str):
    sb.execute_script(
        """
        const selector = arguments[0];
        const value = arguments[1];
        const el = document.querySelector(selector);
        if (!el) return false;

        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, "value"
        ).set;

        setter.call(el, value);
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        el.dispatchEvent(new Event("blur", { bubbles: true }));
        return true;
        """,
        selector,
        value,
    )


def login(sb, user: dict) -> tuple[RenewStatus, str]:
    username = user["username"]
    password = user["password"]

    print(f"🌐 打开登录页：{BASE_URL}/auth/login")

    try:
        sb.open(f"{BASE_URL}/auth/login")
        time.sleep(5)
    except Exception as exc:
        return RenewStatus.LOGIN_FAILED, f"打开登录页失败：{exc}"

    page_state = wait_for_login_form_or_turnstile(sb, timeout=30)

    if page_state == "turnstile":
        screenshot = save_screenshot(sb, username, "turnstile_login")
        detail = f"检测到 Turnstile，需人工完成验证。截图：{screenshot}"
        print(f"⛔ {detail}")
        return RenewStatus.NEEDS_MANUAL_VERIFICATION, detail

    if page_state != "login_form":
        screenshot = save_screenshot(sb, username, "login_form_timeout")
        return RenewStatus.LOGIN_FAILED, f"登录表单未出现。截图：{screenshot}"

    close_cookie_banner(sb)

    try:
        js_fill_input(sb, 'input[name="email"]', username)
        js_fill_input(sb, 'input[name="password"]', password)
        time.sleep(1)
    except Exception as exc:
        screenshot = save_screenshot(sb, username, "fill_login_failed")
        return RenewStatus.LOGIN_FAILED, f"填写登录表单失败：{exc}；截图：{screenshot}"

    if is_turnstile_present(sb) and not turnstile_has_token(sb):
        screenshot = save_screenshot(sb, username, "turnstile_before_submit")
        detail = f"提交前检测到 Turnstile，需人工验证。截图：{screenshot}"
        print(f"⛔ {detail}")
        return RenewStatus.NEEDS_MANUAL_VERIFICATION, detail

    print("🖱️ 提交登录表单...")

    submitted = False
    selectors = [
        'form button[type="submit"]',
        'button[type="submit"]',
        'button.btn-primary',
    ]

    for selector in selectors:
        try:
            if sb.is_element_visible(selector):
                sb.click(selector)
                submitted = True
                break
        except Exception:
            pass

    if not submitted:
        try:
            sb.press_keys('input[name="password"]', "\n")
            submitted = True
        except Exception as exc:
            screenshot = save_screenshot(sb, username, "submit_login_failed")
            return RenewStatus.LOGIN_FAILED, f"提交登录表单失败：{exc}；截图：{screenshot}"

    for _ in range(15):
        time.sleep(1)
        url = (sb.get_current_url() or "").split("?")[0].lower()
        title = (sb.get_title() or "").lower()

        if "/dashboard" in url or "dashboard" in title:
            print("✅ 登录成功")
            return RenewStatus.RENEW_SUCCESS, "登录成功"

        if is_turnstile_present(sb) and not turnstile_has_token(sb):
            screenshot = save_screenshot(sb, username, "turnstile_after_submit")
            return (
                RenewStatus.NEEDS_MANUAL_VERIFICATION,
                f"登录提交后出现 Turnstile，需人工验证。截图：{screenshot}",
            )

    screenshot = save_screenshot(sb, username, "login_failed")
    return RenewStatus.LOGIN_FAILED, f"登录后未进入 Dashboard。截图：{screenshot}"


def read_alert(sb) -> str:
    selectors = [
        "div.alert",
        "[role='alert']",
        ".alert",
    ]

    for selector in selectors:
        try:
            if sb.is_element_visible(selector):
                return (sb.get_text(selector) or "").strip()
        except Exception:
            pass

    return ""


def goto_server_detail(sb, user: dict) -> tuple[RenewStatus | None, str]:
    username = user["username"]

    print("🖥️ 查找服务器详情页...")
    time.sleep(4)

    alert = read_alert(sb)
    if alert and "can't renew" in alert.lower():
        return RenewStatus.NOT_TIME, alert

    selectors = [
        'a[href*="/servers/edit?id="]',
        'a[href*="/servers/edit"]',
        "table a",
    ]

    for selector in selectors:
        try:
            elements = sb.find_elements(selector)
            for element in elements:
                href = (element.get_attribute("href") or "").lower()
                text = (element.text or "").strip().lower()

                if "/servers/edit" in href or text == "see":
                    element.click()
                    time.sleep(4)
                    return None, "已进入服务器详情页"
        except Exception:
            pass

    screenshot = save_screenshot(sb, username, "server_detail_not_found")
    return RenewStatus.RENEW_FAILED, f"未找到服务器详情入口。截图：{screenshot}"


def open_renew_modal(sb, user: dict) -> tuple[RenewStatus | None, str]:
    username = user["username"]

    print("🔄 查找 Renew 按钮...")

    selectors = [
        'button[data-bs-target="#renew-modal"]',
        "button.btn.btn-outline-primary",
        "button.btn-primary",
    ]

    for selector in selectors:
        try:
            elements = sb.find_elements(selector)
            for element in elements:
                text = (element.text or "").strip().lower()
                if "renew" in text or selector == 'button[data-bs-target="#renew-modal"]':
                    sb.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});",
                        element,
                    )
                    time.sleep(0.6)
                    element.click()
                    time.sleep(2)

                    if sb.is_element_visible("div.modal.show"):
                        return None, "Renew 模态框已打开"
        except Exception:
            pass

    screenshot = save_screenshot(sb, username, "renew_modal_not_found")
    return RenewStatus.RENEW_FAILED, f"未能打开 Renew 模态框。截图：{screenshot}"


def modal_needs_verification(sb) -> bool:
    script = """
    const modal = document.querySelector("div.modal.show");
    if (!modal) return false;

    return Boolean(
        modal.querySelector("iframe") ||
        modal.querySelector("input[type='checkbox']") ||
        modal.querySelector("[class*='altcha']") ||
        modal.querySelector("[name*='captcha']") ||
        modal.querySelector("[name*='altcha']")
    );
    """
    try:
        return bool(sb.execute_script(script))
    except Exception:
        return False


def submit_renew(sb, user: dict) -> tuple[RenewStatus, str]:
    username = user["username"]

    if modal_needs_verification(sb):
        screenshot = save_screenshot(sb, username, "renew_verification_required")
        return (
            RenewStatus.NEEDS_MANUAL_VERIFICATION,
            f"Renew 模态框要求验证码/人工验证。截图：{screenshot}",
        )

    print("🖱️ 提交 Renew...")

    try:
        buttons = sb.find_elements("div.modal.show button")
        for button in buttons:
            text = (button.text or "").strip().lower()
            if "renew" in text and "cancel" not in text:
                button.click()
                time.sleep(4)
                break
        else:
            screenshot = save_screenshot(sb, username, "renew_submit_not_found")
            return RenewStatus.RENEW_FAILED, f"未找到提交 Renew 按钮。截图：{screenshot}"
    except Exception as exc:
        screenshot = save_screenshot(sb, username, "renew_submit_exception")
        return RenewStatus.RENEW_FAILED, f"点击 Renew 失败：{exc}；截图：{screenshot}"

    alert = read_alert(sb)
    if not alert:
        time.sleep(3)
        alert = read_alert(sb)

    if not alert:
        screenshot = save_screenshot(sb, username, "renew_result_unknown")
        return RenewStatus.RENEW_FAILED, f"未检测到续期结果提示。截图：{screenshot}"

    lower = alert.lower()

    if "can't renew" in lower or "unable" in lower:
        return RenewStatus.NOT_TIME, alert

    if any(word in lower for word in ("renewed", "success", "extended")):
        return RenewStatus.RENEW_SUCCESS, alert

    return RenewStatus.RENEW_FAILED, alert


def renew_server(sb, user: dict) -> tuple[RenewStatus, str]:
    status, message = goto_server_detail(sb, user)
    if status is not None:
        return status, message

    status, message = open_renew_modal(sb, user)
    if status is not None:
        return status, message

    return submit_renew(sb, user)


def icon_for(status: RenewStatus) -> str:
    return {
        RenewStatus.RENEW_SUCCESS: "✅",
        RenewStatus.NOT_TIME: "⏳",
        RenewStatus.LOGIN_FAILED: "🔒",
        RenewStatus.RENEW_FAILED: "⚠️",
        RenewStatus.NEEDS_MANUAL_VERIFICATION: "🧑‍💻",
        RenewStatus.SCRIPT_ERROR: "💥",
    }[status]


def title_for(status: RenewStatus) -> str:
    return {
        RenewStatus.RENEW_SUCCESS: "续期成功",
        RenewStatus.NOT_TIME: "未到续期时间",
        RenewStatus.LOGIN_FAILED: "登录失败",
        RenewStatus.RENEW_FAILED: "续期失败",
        RenewStatus.NEEDS_MANUAL_VERIFICATION: "需要人工完成验证",
        RenewStatus.SCRIPT_ERROR: "脚本执行异常",
    }[status]


def run_one_user(user: dict, sb_kwargs: dict) -> dict:
    username = user["username"]

    print("\n" + "=" * 60)
    print(f"👤 处理账户：{mask_email(username)}")
    print("=" * 60)

    try:
        with SB(**sb_kwargs) as sb:
            try:
                sb.open("https://api.ip.sb/ip")
                print(f"📍 出口 IP：{sb.get_text('body')}")
            except Exception:
                pass

            status, message = login(sb, user)

            if status == RenewStatus.RENEW_SUCCESS:
                status, message = renew_server(sb, user)

    except Exception as exc:
        status = RenewStatus.SCRIPT_ERROR
        message = f"{type(exc).__name__}: {exc}"

    result = {
        "username": username,
        "masked_username": mask_email(username),
        "status": status,
        "message": message,
        "time": now_bj(),
    }

    tg_account(username, icon_for(status), title_for(status), message)
    print(f"{icon_for(status)} {status.value}: {message}")
    return result


def build_summary(results: list[dict]) -> str:
    count = {status: 0 for status in RenewStatus}

    for item in results:
        count[item["status"]] += 1

    lines = [
        "🇫🇷 KataBump 多账号续期汇总",
        "",
        f"⏱️ 时间：{now_bj()}",
        f"👥 总账号：{len(results)}",
        f"✅ 成功：{count[RenewStatus.RENEW_SUCCESS]}",
        f"⏳ 未到时间：{count[RenewStatus.NOT_TIME]}",
        f"🧑‍💻 需人工验证：{count[RenewStatus.NEEDS_MANUAL_VERIFICATION]}",
        f"🔒 登录失败：{count[RenewStatus.LOGIN_FAILED]}",
        f"⚠️ 续期失败：{count[RenewStatus.RENEW_FAILED]}",
        f"💥 脚本异常：{count[RenewStatus.SCRIPT_ERROR]}",
        "",
        "📋 账号明细：",
    ]

    for index, item in enumerate(results, start=1):
        lines.append(
            f"{index}. {icon_for(item['status'])} "
            f"{item['masked_username']} | "
            f"{item['status'].value} | "
            f"{item['message']}"
        )

    return "\n".join(lines)


def write_reports(results: list[dict]):
    summary = build_summary(results)

    json_data = {
        "generated_at": now_bj(),
        "results": [
            {
                **item,
                "status": item["status"].value,
            }
            for item in results
        ],
    }

    REPORT_JSON.write_text(
        json.dumps(json_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    REPORT_TXT.write_text(summary, encoding="utf-8")

    print(f"📄 JSON 报告：{REPORT_JSON}")
    print(f"📄 文本报告：{REPORT_TXT}")


def main():
    print("#" * 60)
    print("KataBump 多账号续期脚本（人工验证安全模式）")
    print("#" * 60)

    users = load_users()
    print(f"📦 已读取 {len(users)} 个账号")

    sb_kwargs = {
        "uc": True,
        "headless": False,
        "xvfb": True,
        "xvfb_metrics": "1366,768",
    }

    if IS_PROXY:
        proxy = PROXY_SERVER or "http://127.0.0.1:1081"
        sb_kwargs["proxy"] = proxy
        print(f"🔗 使用代理：{proxy}")
    else:
        print("🌐 未配置代理，使用直连")

    results = []

    for index, user in enumerate(users, start=1):
        print(f"\n🚀 开始第 {index}/{len(users)} 个账号")
        results.append(run_one_user(user, sb_kwargs))

    write_reports(results)
    summary = build_summary(results)

    print("\n" + summary)
    tg_send(summary)


if __name__ == "__main__":
    main()
