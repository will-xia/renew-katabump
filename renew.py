#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import requests
from seleniumbase import SB


# ============================================================
# 配置
# ============================================================

BASE_URL = "https://dashboard.katabump.com"

# 从 GitHub Actions Secrets / 环境变量读取
USERS_JSON = os.environ.get("USERS_JSON", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()

IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip()

# workflow 使用：xvfb-run -a python renew.py
# 因此不使用 SeleniumBase 内部 Xvfb，也不运行 Chrome headless。
HEADLESS = False
USE_XVFB = False

REPORT_DIR = Path("output")
SCREENSHOT_DIR = REPORT_DIR / "screenshots"
REPORT_JSON_FILE = REPORT_DIR / "katabump_report.json"
REPORT_TXT_FILE = REPORT_DIR / "katabump_report.txt"

REPORT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# 数据结构
# ============================================================

class RenewStatus(str, Enum):
    RENEW_SUCCESS = "renew_success"
    NOT_TIME = "not_time"
    LOGIN_FAILED = "login_failed"
    RENEW_FAILED = "renew_failed"
    NEEDS_MANUAL_VERIFICATION = "needs_manual_verification"
    SCRIPT_ERROR = "script_error"


@dataclass
class Account:
    username: str
    password: str


@dataclass
class AccountResult:
    username: str
    status: RenewStatus
    message: str
    timestamp: str


# ============================================================
# 基础工具
# ============================================================

def now_bj() -> str:
    """返回 UTC+8 时间。"""
    return time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.gmtime(time.time() + 8 * 3600),
    )


def safe_name(value: str) -> str:
    """转为安全的文件名片段。"""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "_", value)
    return value[:96] or "unknown"


def mask_email(email: str) -> str:
    """邮箱脱敏。"""
    if not email:
        return "****"

    if "@" not in email:
        return f"{email[:2]}****"

    name, domain = email.split("@", 1)

    if len(name) <= 4:
        return f"{name}@{domain}"

    return f"{name[:2]}****{name[-2:]}@{domain}"


def load_accounts() -> list[Account]:
    """
    USERS_JSON 格式：

    [
      {"username":"user1@example.com","password":"password1"},
      {"username":"user2@example.com","password":"password2"}
    ]

    同时兼容 email 字段：

    [
      {"email":"user@example.com","password":"password"}
    ]
    """
    if not USERS_JSON:
        raise ValueError("未配置 USERS_JSON。")

    try:
        raw_accounts = json.loads(USERS_JSON)
    except json.JSONDecodeError as exc:
        raise ValueError(f"USERS_JSON 不是合法 JSON：{exc}") from exc

    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ValueError("USERS_JSON 必须是非空 JSON 数组。")

    accounts: list[Account] = []

    for index, raw in enumerate(raw_accounts, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 个账号不是 JSON 对象。")

        username = str(raw.get("username") or raw.get("email") or "").strip()
        password = str(raw.get("password") or "")

        if not username:
            raise ValueError(f"第 {index} 个账号缺少 username 或 email。")

        if not password:
            raise ValueError(f"第 {index} 个账号缺少 password。")

        accounts.append(Account(username=username, password=password))

    return accounts


def save_screenshot(sb, username: str, label: str) -> str:
    """保存截图。"""
    filename = (
        f"{safe_name(username)}_"
        f"{safe_name(label)}_"
        f"{time.strftime('%Y%m%d_%H%M%S')}.png"
    )
    path = SCREENSHOT_DIR / filename

    try:
        sb.save_screenshot(str(path))
        print(f"📸 已保存截图：{path}")
    except Exception as exc:
        print(f"⚠️ 截图保存失败：{exc}")

    return str(path)


# ============================================================
# Telegram
# ============================================================

def split_tg_text(text: str, limit: int = 3500) -> list[str]:
    """将 Telegram 长消息切分。"""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remain = text

    while remain:
        if len(remain) <= limit:
            chunks.append(remain)
            break

        split_pos = remain.rfind("\n", 0, limit)

        if split_pos < limit // 2:
            split_pos = limit

        chunks.append(remain[:split_pos].rstrip())
        remain = remain[split_pos:].lstrip()

    return chunks


def send_tg_text(text: str):
    """推送 Telegram 消息。"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 通知。")
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    for index, chunk in enumerate(split_tg_text(text), start=1):
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": TG_CHAT_ID,
                    "text": chunk,
                },
                timeout=15,
            )

            if response.status_code == 200:
                print(f"📩 Telegram 已发送（第 {index} 段）。")
            else:
                print(
                    f"⚠️ Telegram 发送失败："
                    f"{response.status_code} {response.text}"
                )

        except requests.RequestException as exc:
            print(f"⚠️ Telegram 请求异常：{exc}")


def status_icon(status: RenewStatus) -> str:
    return {
        RenewStatus.RENEW_SUCCESS: "✅",
        RenewStatus.NOT_TIME: "⏳",
        RenewStatus.LOGIN_FAILED: "🔒",
        RenewStatus.RENEW_FAILED: "⚠️",
        RenewStatus.NEEDS_MANUAL_VERIFICATION: "🧑‍💻",
        RenewStatus.SCRIPT_ERROR: "💥",
    }[status]


def status_text(status: RenewStatus) -> str:
    return {
        RenewStatus.RENEW_SUCCESS: "续期成功",
        RenewStatus.NOT_TIME: "未到续期时间",
        RenewStatus.LOGIN_FAILED: "登录失败",
        RenewStatus.RENEW_FAILED: "续期失败",
        RenewStatus.NEEDS_MANUAL_VERIFICATION: "需要人工完成验证",
        RenewStatus.SCRIPT_ERROR: "脚本执行异常",
    }[status]


def send_account_notification(result: AccountResult):
    """发送单账号结果通知。"""
    message = (
        "🇫🇷 KataBump 续期通知\n\n"
        f"{status_icon(result.status)} {status_text(result.status)}\n"
        f"👤 账号：{mask_email(result.username)}\n"
        f"⏱️ 时间：{result.timestamp}\n"
        f"📝 详情：{result.message[:1500]}"
    )
    send_tg_text(message)


# ============================================================
# JavaScript 工具
# ============================================================

TURNSTILE_EXISTS_JS = """
(function() {
    return Boolean(
        document.querySelector('input[name="cf-turnstile-response"]') ||
        document.querySelector(".cf-turnstile") ||
        document.querySelector(
            'iframe[src*="challenges.cloudflare.com"]'
        )
    );
})();
"""

TURNSTILE_SOLVED_JS = """
(function() {
    var input = document.querySelector(
        'input[name="cf-turnstile-response"]'
    );

    return Boolean(
        input &&
        input.value &&
        input.value.length > 20
    );
})();
"""

MODAL_VERIFICATION_EXISTS_JS = """
(function() {
    var modal = document.querySelector("div.modal.show");

    if (!modal) {
        return false;
    }

    return Boolean(
        modal.querySelector("iframe") ||
        modal.querySelector('input[type="checkbox"]') ||
        modal.querySelector('[class*="altcha"]') ||
        modal.querySelector('[class*="captcha"]') ||
        modal.querySelector('[name*="altcha"]') ||
        modal.querySelector('[name*="captcha"]')
    );
})();
"""


def is_turnstile_present(sb) -> bool:
    try:
        return bool(sb.execute_script(TURNSTILE_EXISTS_JS))
    except Exception:
        return False


def has_turnstile_token(sb) -> bool:
    try:
        return bool(sb.execute_script(TURNSTILE_SOLVED_JS))
    except Exception:
        return False


def modal_requires_verification(sb) -> bool:
    try:
        return bool(sb.execute_script(MODAL_VERIFICATION_EXISTS_JS))
    except Exception:
        return False


def js_fill_input(sb, selector: str, value: str) -> bool:
    """
    通过 JS 填写输入框。

    关键点：
    - 不使用 arguments[0] / arguments[1]；
    - 不向 execute_script() 传 Python 参数；
    - selector、value 使用 json.dumps() 安全嵌入 JS；
    - 所有 return 均在 IIFE 内部。
    """
    selector_js = json.dumps(selector, ensure_ascii=False)
    value_js = json.dumps(value, ensure_ascii=False)

    script = f"""
    (function() {{
        var selector = {selector_js};
        var value = {value_js};
        var element = document.querySelector(selector);

        if (!element) {{
            return {{
                ok: false,
                reason: "element_not_found"
            }};
        }}

        try {{
            element.scrollIntoView({{
                behavior: "instant",
                block: "center",
                inline: "nearest"
            }});

            element.focus();

            var descriptor = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype,
                "value"
            );

            if (descriptor && descriptor.set) {{
                descriptor.set.call(element, value);
            }} else {{
                element.value = value;
            }}

            element.dispatchEvent(new Event("input", {{
                bubbles: true
            }}));

            element.dispatchEvent(new Event("change", {{
                bubbles: true
            }}));

            element.dispatchEvent(new Event("blur", {{
                bubbles: true
            }}));

            return {{
                ok: true,
                actual_value: element.value
            }};
        }} catch (error) {{
            return {{
                ok: false,
                reason: String(error)
            }};
        }}
    }})();
    """

    try:
        result = sb.execute_script(script)

        # 常规 Selenium / SeleniumBase 模式可能返回 dict。
        if isinstance(result, dict):
            if result.get("ok"):
                print(f"✅ 已填写：{selector}")
                return True

            print(f"⚠️ 填写输入框失败 {selector}：{result}")
            return False

        # CDP evaluate 有时不能正常返回对象；
        # 再次读取输入框 value，以真实页面状态为准。
        verify_script = f"""
        (function() {{
            var element = document.querySelector({selector_js});

            return Boolean(
                element &&
                element.value === {value_js}
            );
        }})();
        """

        verified = bool(sb.execute_script(verify_script))

        if verified:
            print(f"✅ 已填写：{selector}")
            return True

        print(
            f"⚠️ 填写输入框失败 {selector}："
            f"脚本返回 {result}，且页面值校验失败。"
        )
        return False

    except Exception as exc:
        print(f"⚠️ 写入输入框失败 {selector}：{exc}")
        return False


def scroll_to_element(sb, element):
    """
    使用 SeleniumBase / WebDriver 的元素滚动，
    不使用 arguments[0] 传入 JS。
    """
    try:
        sb.scroll_to(element)
        time.sleep(0.3)
        return
    except Exception:
        pass

    try:
        element.location_once_scrolled_into_view
        time.sleep(0.3)
    except Exception:
        pass


def close_cookie_banner(sb):
    """尽力关闭 Cookie 弹窗。"""
    selectors = [
        "button#onetrust-accept-btn-handler",
        "button[aria-label*='Accept']",
        "button[aria-label*='accept']",
        "button.cookie-accept",
    ]

    for selector in selectors:
        try:
            if sb.is_element_visible(selector):
                sb.click(selector)
                print(f"🍪 已关闭 Cookie 弹窗：{selector}")
                time.sleep(0.5)
                return
        except Exception:
            pass

    try:
        for button in sb.find_elements("button"):
            text = (button.text or "").strip().lower()

            if text in {"accept", "accept all", "allow all"}:
                button.click()
                print("🍪 已通过按钮文本关闭 Cookie 弹窗。")
                time.sleep(0.5)
                return
    except Exception:
        pass


def read_alert(sb) -> str:
    """读取页面 alert 文本。"""
    selectors = [
        "div.alert",
        "[role='alert']",
        ".alert",
    ]

    for selector in selectors:
        try:
            if sb.is_element_visible(selector):
                text = (sb.get_text(selector) or "").strip()

                if text:
                    return text
        except Exception:
            pass

    return ""


# ============================================================
# 登录流程
# ============================================================

def wait_login_form_or_turnstile(sb, timeout: int = 30) -> str:
    """
    等待登录表单或验证码。

    返回：
    - login_form
    - turnstile
    - timeout
    """
    for second in range(1, timeout + 1):
        try:
            if sb.is_element_present('input[name="email"]'):
                print(f"✅ 登录表单已出现（{second}s）")
                return "login_form"
        except Exception:
            pass

        if is_turnstile_present(sb):
            print(f"🧑‍💻 检测到 Turnstile（{second}s）")
            return "turnstile"

        time.sleep(1)

    return "timeout"


def login(sb, account: Account) -> tuple[RenewStatus, str]:
    """登录账号。"""
    print(f"🌐 打开登录页面：{BASE_URL}/auth/login")

    try:
        sb.open(f"{BASE_URL}/auth/login")
        time.sleep(5)
    except Exception as exc:
        return RenewStatus.LOGIN_FAILED, f"打开登录页面失败：{exc}"

    page_state = wait_login_form_or_turnstile(sb, timeout=30)

    if page_state == "turnstile":
        screenshot = save_screenshot(
            sb,
            account.username,
            "login_turnstile",
        )
        return (
            RenewStatus.NEEDS_MANUAL_VERIFICATION,
            f"登录页面出现 Turnstile。截图：{screenshot}",
        )

    if page_state == "timeout":
        screenshot = save_screenshot(
            sb,
            account.username,
            "login_form_timeout",
        )
        return (
            RenewStatus.LOGIN_FAILED,
            f"等待登录表单超时。截图：{screenshot}",
        )

    close_cookie_banner(sb)

    print(f"📧 填写邮箱：{mask_email(account.username)}")
    if not js_fill_input(sb, 'input[name="email"]', account.username):
        screenshot = save_screenshot(
            sb,
            account.username,
            "email_input_failed",
        )
        return (
            RenewStatus.LOGIN_FAILED,
            f"无法填写邮箱输入框。截图：{screenshot}",
        )

    print("🔑 填写密码")
    if not js_fill_input(sb, 'input[name="password"]', account.password):
        screenshot = save_screenshot(
            sb,
            account.username,
            "password_input_failed",
        )
        return (
            RenewStatus.LOGIN_FAILED,
            f"无法填写密码输入框。截图：{screenshot}",
        )

    time.sleep(1)

    if is_turnstile_present(sb) and not has_turnstile_token(sb):
        screenshot = save_screenshot(
            sb,
            account.username,
            "turnstile_before_submit",
        )
        return (
            RenewStatus.NEEDS_MANUAL_VERIFICATION,
            f"提交前检测到 Turnstile。截图：{screenshot}",
        )

    print("🖱️ 提交登录表单")

    submitted = False

    for selector in [
        'form button[type="submit"]',
        'button[type="submit"]',
        'button.btn-primary',
    ]:
        try:
            if sb.is_element_visible(selector):
                sb.click(selector)
                print(f"✅ 已点击登录提交按钮：{selector}")
                submitted = True
                break
        except Exception:
            pass

    if not submitted:
        try:
            sb.press_keys('input[name="password"]', "\n")
            print("✅ 已通过回车提交登录表单。")
            submitted = True
        except Exception as exc:
            screenshot = save_screenshot(
                sb,
                account.username,
                "login_submit_failed",
            )
            return (
                RenewStatus.LOGIN_FAILED,
                f"提交登录失败：{exc}；截图：{screenshot}",
            )

    for second in range(1, 16):
        time.sleep(1)

        try:
            current_url = (sb.get_current_url() or "").split("?")[0].lower()
            page_title = (sb.get_title() or "").lower()
        except Exception:
            current_url = ""
            page_title = ""

        if "/dashboard" in current_url or "dashboard" in page_title:
            print(f"✅ 登录成功（{second}s）")
            return RenewStatus.RENEW_SUCCESS, "登录成功"

        if is_turnstile_present(sb) and not has_turnstile_token(sb):
            screenshot = save_screenshot(
                sb,
                account.username,
                "turnstile_after_submit",
            )
            return (
                RenewStatus.NEEDS_MANUAL_VERIFICATION,
                f"登录提交后出现 Turnstile。截图：{screenshot}",
            )

    screenshot = save_screenshot(
        sb,
        account.username,
        "login_failed",
    )

    return (
        RenewStatus.LOGIN_FAILED,
        f"登录后未进入 Dashboard。截图：{screenshot}",
    )


# ============================================================
# 服务器续期流程
# ============================================================

def goto_server_detail(
    sb,
    account: Account,
) -> tuple[Optional[RenewStatus], str]:
    """Dashboard -> 服务器详情页。"""
    print("\n🖥️ 正在进入服务器续期页...")
    time.sleep(4)

    alert_text = read_alert(sb)

    if alert_text and "can't renew" in alert_text.lower():
        print(f"⏳ 页面提示：{alert_text}")
        return RenewStatus.NOT_TIME, alert_text

    selectors = [
        'a[href*="/servers/edit?id="]',
        'td a[href*="/servers/edit"]',
        'table a[href*="/servers/edit"]',
        "table td a",
    ]

    for selector in selectors:
        try:
            elements = sb.find_elements(selector)
        except Exception:
            continue

        for element in elements:
            try:
                href = (element.get_attribute("href") or "").lower()
                text = (element.text or "").strip().lower()

                if "/servers/edit" not in href and text != "see":
                    continue

                print(f"✅ 找到服务器详情入口：{selector}")

                scroll_to_element(sb, element)
                element.click()

                time.sleep(4)
                print(f"📄 当前页面：{sb.get_current_url()}")

                return None, "已进入服务器详情页"

            except Exception:
                continue

    screenshot = save_screenshot(
        sb,
        account.username,
        "server_detail_not_found",
    )

    return (
        RenewStatus.RENEW_FAILED,
        f"未找到服务器详情页入口。截图：{screenshot}",
    )


def open_renew_modal(
    sb,
    account: Account,
) -> tuple[Optional[RenewStatus], str]:
    """服务器详情页 -> 打开 Renew 模态框。"""
    print("\n🔄 查找 Renew 按钮...")

    selectors = [
        'button[data-bs-target="#renew-modal"]',
        "button.btn.btn-outline-primary",
        "button.btn-primary",
    ]

    for selector in selectors:
        try:
            buttons = sb.find_elements(selector)
        except Exception:
            continue

        for button in buttons:
            try:
                text = (button.text or "").strip().lower()

                if (
                    selector != 'button[data-bs-target="#renew-modal"]'
                    and "renew" not in text
                ):
                    continue

                scroll_to_element(sb, button)
                button.click()

                print("🖱️ 已点击 Renew，等待模态框...")
                time.sleep(2)

                if sb.is_element_visible("div.modal.show"):
                    print("✅ Renew 模态框已弹出。")
                    return None, "Renew 模态框已打开"

            except Exception:
                continue

    screenshot = save_screenshot(
        sb,
        account.username,
        "renew_modal_not_found",
    )

    return (
        RenewStatus.RENEW_FAILED,
        f"未找到或无法打开 Renew 模态框。截图：{screenshot}",
    )


def submit_renew(sb, account: Account) -> tuple[RenewStatus, str]:
    """
    提交续期。

    模态框内检测到验证控件时停止，避免自动化处理人机验证。
    """
    if modal_requires_verification(sb):
        screenshot = save_screenshot(
            sb,
            account.username,
            "renew_verification_required",
        )

        return (
            RenewStatus.NEEDS_MANUAL_VERIFICATION,
            f"Renew 模态框需要人工验证。截图：{screenshot}",
        )

    print("🖱️ 提交 Renew...")

    clicked = False

    try:
        buttons = sb.find_elements("div.modal.show button")

        for button in buttons:
            text = (button.text or "").strip().lower()

            if "renew" not in text or "cancel" in text:
                continue

            scroll_to_element(sb, button)
            button.click()

            clicked = True
            print("✅ 已点击 Renew 提交按钮。")
            break

    except Exception as exc:
        print(f"⚠️ 查找 Renew 提交按钮异常：{exc}")

    if not clicked:
        screenshot = save_screenshot(
            sb,
            account.username,
            "renew_submit_button_not_found",
        )

        return (
            RenewStatus.RENEW_FAILED,
            f"未找到 Renew 提交按钮。截图：{screenshot}",
        )

    time.sleep(4)

    alert_text = read_alert(sb)

    if not alert_text:
        time.sleep(3)
        alert_text = read_alert(sb)

    if not alert_text:
        screenshot = save_screenshot(
            sb,
            account.username,
            "renew_result_unknown",
        )

        return (
            RenewStatus.RENEW_FAILED,
            f"未检测到明确续期结果。截图：{screenshot}",
        )

    print(f"📩 页面提示：{alert_text}")
    lowered = alert_text.lower()

    if "can't renew" in lowered or "unable" in lowered:
        return RenewStatus.NOT_TIME, alert_text

    if any(word in lowered for word in ("renewed", "success", "extended")):
        return RenewStatus.RENEW_SUCCESS, alert_text

    return RenewStatus.RENEW_FAILED, alert_text


def renew_server(sb, account: Account) -> tuple[RenewStatus, str]:
    """完整续期流程。"""
    print("\n" + "#" * 50)
    print(f"开始续期：{mask_email(account.username)}")
    print("#" * 50)

    status, message = goto_server_detail(sb, account)

    if status is not None:
        return status, message

    status, message = open_renew_modal(sb, account)

    if status is not None:
        return status, message

    return submit_renew(sb, account)


# ============================================================
# 浏览器与账号调度
# ============================================================

def build_sb_kwargs() -> dict:
    """构建浏览器参数。"""
    kwargs = {
        "uc": True,
        "headless": HEADLESS,
    }

    # 当前始终 false；外部 xvfb-run 管理虚拟显示。
    if USE_XVFB and not HEADLESS:
        kwargs["xvfb"] = True

    if IS_PROXY:
        proxy = PROXY_SERVER or "http://127.0.0.1:1081"
        kwargs["proxy"] = proxy
        print(f"🔗 使用代理：{proxy}")
    else:
        print("🌐 未使用代理，直连访问。")

    return kwargs


def print_exit_ip(sb):
    """打印当前出口 IP。"""
    try:
        sb.open("https://api.ip.sb/ip")
        time.sleep(1)

        ip = (sb.get_text("body") or "").strip()
        print(f"📍 当前出口 IP：{ip}")

    except Exception as exc:
        print(f"ℹ️ 无法获取出口 IP：{exc}")


def run_account(account: Account, sb_kwargs: dict) -> AccountResult:
    """独立执行一个账号。"""
    print("\n" + "=" * 60)
    print(f"👤 开始处理：{mask_email(account.username)}")
    print("=" * 60)

    try:
        with SB(**sb_kwargs) as sb:
            print_exit_ip(sb)

            status, message = login(sb, account)

            if status == RenewStatus.RENEW_SUCCESS:
                status, message = renew_server(sb, account)

    except Exception as exc:
        status = RenewStatus.SCRIPT_ERROR
        message = f"{type(exc).__name__}: {exc}"
        print(f"💥 账号执行异常：{message}")

    result = AccountResult(
        username=account.username,
        status=status,
        message=message,
        timestamp=now_bj(),
    )

    print(
        f"{status_icon(result.status)} "
        f"{mask_email(result.username)} => "
        f"{result.status.value} | {result.message}"
    )

    send_account_notification(result)

    return result


# ============================================================
# 报告
# ============================================================

def build_summary(results: list[AccountResult]) -> str:
    """生成汇总文本。"""
    counts = {status: 0 for status in RenewStatus}

    for result in results:
        counts[result.status] += 1

    lines = [
        "🇫🇷 KataBump 多账号续期汇总",
        "",
        f"⏱️ 汇总时间：{now_bj()}",
        f"👥 总账号数：{len(results)}",
        f"✅ 续期成功：{counts[RenewStatus.RENEW_SUCCESS]}",
        f"⏳ 未到续期时间：{counts[RenewStatus.NOT_TIME]}",
        f"🧑‍💻 需要人工验证："
        f"{counts[RenewStatus.NEEDS_MANUAL_VERIFICATION]}",
        f"🔒 登录失败：{counts[RenewStatus.LOGIN_FAILED]}",
        f"⚠️ 续期失败：{counts[RenewStatus.RENEW_FAILED]}",
        f"💥 脚本异常：{counts[RenewStatus.SCRIPT_ERROR]}",
        "",
        "📋 账号明细：",
    ]

    for index, result in enumerate(results, start=1):
        lines.append(
            f"{index}. "
            f"{status_icon(result.status)} "
            f"{mask_email(result.username)} | "
            f"{result.status.value} | "
            f"{result.message}"
        )

    return "\n".join(lines)


def write_reports(results: list[AccountResult]):
    """写入 JSON 和 TXT 报告。"""
    summary = build_summary(results)

    report_data = {
        "generated_at": now_bj(),
        "results": [
            {
                "username": result.username,
                "masked_username": mask_email(result.username),
                "status": result.status.value,
                "message": result.message,
                "timestamp": result.timestamp,
            }
            for result in results
        ],
    }

    REPORT_JSON_FILE.write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    REPORT_TXT_FILE.write_text(
        summary,
        encoding="utf-8",
    )

    print(f"📄 已生成 JSON 报告：{REPORT_JSON_FILE}")
    print(f"📄 已生成文本报告：{REPORT_TXT_FILE}")


# ============================================================
# 入口
# ============================================================

def main():
    print("#" * 60)
    print("KataBump 多账号自动续期")
    print("运行模式：外部 xvfb-run，内部 Xvfb 已禁用")
    print("#" * 60)

    accounts = load_accounts()

    print(f"📦 已读取 {len(accounts)} 个账号。")

    sb_kwargs = build_sb_kwargs()
    results: list[AccountResult] = []

    for index, account in enumerate(accounts, start=1):
        print(f"\n🚀 处理第 {index}/{len(accounts)} 个账号")
        result = run_account(account, sb_kwargs)
        results.append(result)

    write_reports(results)

    summary = build_summary(results)

    print("\n" + summary)
    send_tg_text(summary)


if __name__ == "__main__":
    main()
