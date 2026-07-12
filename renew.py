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

USERS_JSON = os.environ.get("USERS_JSON", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()

IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip()

HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true"
USE_XVFB = os.environ.get("USE_XVFB", "true").lower() == "true"
XVFB_METRICS = os.environ.get("XVFB_METRICS", "1366,768")

REPORT_DIR = Path("output")
SCREENSHOT_DIR = REPORT_DIR / "screenshots"
REPORT_JSON_FILE = REPORT_DIR / "katabump_report.json"
REPORT_TXT_FILE = REPORT_DIR / "katabump_report.txt"

REPORT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# 状态与数据结构
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
    """返回 UTC+8 北京时间。"""
    return time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.gmtime(time.time() + 8 * 3600),
    )


def safe_name(value: str) -> str:
    """将文本转换为可安全作为文件名的一部分。"""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "_", value)
    return value[:96] or "unknown"


def mask_email(email: str) -> str:
    """邮箱脱敏，保留用户名首尾各两位。"""
    if not email:
        return "****"

    if "@" not in email:
        return email[:2] + "****"

    name, domain = email.split("@", 1)

    if len(name) <= 4:
        return f"{name}@{domain}"

    return f"{name[:2]}****{name[-2:]}@{domain}"


def load_accounts() -> list[Account]:
    """
    从 USERS_JSON 读取账号。

    USERS_JSON 示例：
    [
      {"username": "user1@example.com", "password": "password1"},
      {"username": "user2@example.com", "password": "password2"}
    ]

    同时兼容：
    {"email": "user@example.com", "password": "password"}
    """
    if not USERS_JSON:
        raise ValueError("未配置 USERS_JSON。")

    try:
        raw_accounts = json.loads(USERS_JSON)
    except json.JSONDecodeError as exc:
        raise ValueError(f"USERS_JSON 不是合法 JSON：{exc}") from exc

    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ValueError("USERS_JSON 必须是非空 JSON 数组。")

    accounts = []

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
    """保存失败现场截图。"""
    filename = (
        f"{safe_name(username)}_"
        f"{safe_name(label)}_"
        f"{time.strftime('%Y%m%d_%H%M%S')}.png"
    )
    screenshot_path = SCREENSHOT_DIR / filename

    try:
        sb.save_screenshot(str(screenshot_path))
        print(f"📸 已保存截图：{screenshot_path}")
    except Exception as exc:
        print(f"⚠️ 截图保存失败：{exc}")

    return str(screenshot_path)


# ============================================================
# Telegram
# ============================================================

def split_tg_text(text: str, limit: int = 3500) -> list[str]:
    """Telegram 单条消息安全分片。"""
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        position = remaining.rfind("\n", 0, limit)
        if position < limit // 2:
            position = limit

        chunks.append(remaining[:position].rstrip())
        remaining = remaining[position:].lstrip()

    return chunks


def send_tg_text(text: str):
    """发送 Telegram；未配置时仅记录日志。"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 通知。")
        return

    api_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    for index, chunk in enumerate(split_tg_text(text), start=1):
        try:
            response = requests.post(
                api_url,
                json={
                    "chat_id": TG_CHAT_ID,
                    "text": chunk,
                },
                timeout=15,
            )

            if response.status_code == 200:
                print(f"📩 Telegram 消息已发送（第 {index} 段）。")
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
    """发送单账号结果。"""
    text = (
        "🇫🇷 KataBump 续期通知\n\n"
        f"{status_icon(result.status)} {status_text(result.status)}\n"
        f"👤 账号：{mask_email(result.username)}\n"
        f"⏱️ 时间：{result.timestamp}\n"
        f"📝 详情：{result.message[:1500]}"
    )
    send_tg_text(text)


# ============================================================
# JavaScript 页面工具
# ============================================================

TURNSTILE_EXISTS_JS = """
(function() {
    return Boolean(
        document.querySelector('input[name="cf-turnstile-response"]') ||
        document.querySelector('.cf-turnstile') ||
        document.querySelector('iframe[src*="challenges.cloudflare.com"]')
    );
})();
"""

TURNSTILE_SOLVED_JS = """
(function() {
    var input = document.querySelector('input[name="cf-turnstile-response"]');
    return Boolean(input && input.value && input.value.length > 20);
})();
"""

MODAL_VERIFICATION_EXISTS_JS = """
(function() {
    var modal = document.querySelector('div.modal.show');

    if (!modal) {
        return false;
    }

    return Boolean(
        modal.querySelector('iframe') ||
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
    用原生 input.value setter 填写输入框。

    重要：
    - 返回值必须在 IIFE 函数内。
    - 不能将 return 写在 execute_script 的顶层。
    - 否则 CDP 环境会出现 SyntaxError: Illegal return statement。
    """
    script = """
    (function(selector, value) {
        var element = document.querySelector(selector);

        if (!element) {
            return {
                ok: false,
                reason: "element_not_found"
            };
        }

        try {
            element.scrollIntoView({
                behavior: "instant",
                block: "center",
                inline: "nearest"
            });

            element.focus();

            var descriptor = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype,
                "value"
            );

            if (descriptor && descriptor.set) {
                descriptor.set.call(element, value);
            } else {
                element.value = value;
            }

            element.dispatchEvent(new Event("input", {
                bubbles: true
            }));

            element.dispatchEvent(new Event("change", {
                bubbles: true
            }));

            element.dispatchEvent(new Event("blur", {
                bubbles: true
            }));

            return {
                ok: true,
                actual_value: element.value
            };
        } catch (error) {
            return {
                ok: false,
                reason: String(error)
            };
        }
    })(arguments[0], arguments[1]);
    """

    try:
        result = sb.execute_script(script, selector, value)

        if isinstance(result, dict):
            if result.get("ok"):
                print(f"✅ 已填写：{selector}")
                return True

            print(f"⚠️ 填写输入框失败 {selector}：{result}")
            return False

        if result is True:
            print(f"✅ 已填写：{selector}")
            return True

        print(f"⚠️ 填写输入框失败 {selector}：返回值 {result}")
        return False

    except Exception as exc:
        print(f"⚠️ 写入输入框失败 {selector}：{exc}")
        return False


def close_cookie_banner(sb):
    """尝试关闭 Cookie 弹窗；找不到也不报错。"""
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
            button_text = (button.text or "").strip().lower()

            if button_text in {"accept", "accept all", "allow all"}:
                button.click()
                print("🍪 已通过文本关闭 Cookie 弹窗。")
                time.sleep(0.5)
                return
    except Exception:
        pass


def read_alert(sb) -> str:
    """读取 Bootstrap 或 ARIA alert 文本。"""
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
# 登录
# ============================================================

def wait_login_form_or_turnstile(sb, timeout: int = 30) -> str:
    """
    等待登录页面状态。

    返回值：
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
    """打开页面、填写凭据、提交登录并确认 Dashboard。"""
    username = account.username

    print(f"🌐 打开登录页面：{BASE_URL}/auth/login")

    try:
        sb.open(f"{BASE_URL}/auth/login")
        time.sleep(5)
    except Exception as exc:
        return RenewStatus.LOGIN_FAILED, f"打开登录页面失败：{exc}"

    state = wait_login_form_or_turnstile(sb, timeout=30)

    if state == "turnstile":
        screenshot = save_screenshot(sb, username, "login_turnstile")
        return (
            RenewStatus.NEEDS_MANUAL_VERIFICATION,
            f"登录页出现 Turnstile，需要人工验证。截图：{screenshot}",
        )

    if state == "timeout":
        screenshot = save_screenshot(sb, username, "login_form_timeout")
        return (
            RenewStatus.LOGIN_FAILED,
            f"登录表单未出现。截图：{screenshot}",
        )

    close_cookie_banner(sb)

    print(f"📧 填写邮箱：{mask_email(username)}")
    if not js_fill_input(sb, 'input[name="email"]', account.username):
        screenshot = save_screenshot(sb, username, "email_input_failed")
        return (
            RenewStatus.LOGIN_FAILED,
            f"无法填写邮箱输入框。截图：{screenshot}",
        )

    print("🔑 填写密码")
    if not js_fill_input(sb, 'input[name="password"]', account.password):
        screenshot = save_screenshot(sb, username, "password_input_failed")
        return (
            RenewStatus.LOGIN_FAILED,
            f"无法填写密码输入框。截图：{screenshot}",
        )

    time.sleep(1)

    if is_turnstile_present(sb) and not has_turnstile_token(sb):
        screenshot = save_screenshot(sb, username, "turnstile_before_submit")
        return (
            RenewStatus.NEEDS_MANUAL_VERIFICATION,
            f"提交登录前检测到 Turnstile，需要人工验证。截图：{screenshot}",
        )

    print("🖱️ 提交登录表单")

    submitted = False
    submit_selectors = [
        'form button[type="submit"]',
        'button[type="submit"]',
        'button.btn-primary',
    ]

    for selector in submit_selectors:
        try:
            if sb.is_element_visible(selector):
                sb.click(selector)
                submitted = True
                print(f"✅ 已点击登录按钮：{selector}")
                break
        except Exception:
            pass

    if not submitted:
        try:
            sb.press_keys('input[name="password"]', "\n")
            submitted = True
            print("✅ 已通过回车提交登录表单。")
        except Exception as exc:
            screenshot = save_screenshot(sb, username, "login_submit_failed")
            return (
                RenewStatus.LOGIN_FAILED,
                f"提交登录表单失败：{exc}；截图：{screenshot}",
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
            screenshot = save_screenshot(sb, username, "turnstile_after_submit")
            return (
                RenewStatus.NEEDS_MANUAL_VERIFICATION,
                f"登录提交后出现 Turnstile，需要人工验证。截图：{screenshot}",
            )

    screenshot = save_screenshot(sb, username, "login_failed")
    return (
        RenewStatus.LOGIN_FAILED,
        f"登录后未进入 Dashboard。截图：{screenshot}",
    )


# ============================================================
# 续期
# ============================================================

def goto_server_detail(sb, account: Account) -> tuple[Optional[RenewStatus], str]:
    """在 Dashboard 查找 See，进入服务器详情页。"""
    username = account.username

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
                link_text = (element.text or "").strip().lower()

                if "/servers/edit" in href or link_text == "see":
                    print(f"✅ 找到服务器详情入口：{selector}")

                    sb.execute_script(
                        """
                        (function(element) {
                            element.scrollIntoView({
                                behavior: "instant",
                                block: "center"
                            });
                        })(arguments[0]);
                        """,
                        element,
                    )

                    time.sleep(0.5)
                    element.click()
                    time.sleep(4)

                    print(f"📄 当前页面：{sb.get_current_url()}")
                    return None, "已进入服务器详情页"

            except Exception:
                continue

    screenshot = save_screenshot(sb, username, "server_detail_not_found")
    return (
        RenewStatus.RENEW_FAILED,
        f"未找到服务器详情入口 See。截图：{screenshot}",
    )


def open_renew_modal(sb, account: Account) -> tuple[Optional[RenewStatus], str]:
    """定位 Renew 按钮，点击并确认模态框显示。"""
    username = account.username

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
                button_text = (button.text or "").strip().lower()

                if (
                    selector == 'button[data-bs-target="#renew-modal"]'
                    or "renew" in button_text
                ):
                    sb.execute_script(
                        """
                        (function(element) {
                            element.scrollIntoView({
                                behavior: "instant",
                                block: "center"
                            });
                        })(arguments[0]);
                        """,
                        button,
                    )

                    time.sleep(0.8)
                    button.click()

                    print("🖱️ 已点击 Renew，等待模态框...")
                    time.sleep(2)

                    if sb.is_element_visible("div.modal.show"):
                        print("✅ Renew 模态框已弹出。")
                        return None, "Renew 模态框已打开"

            except Exception:
                continue

    screenshot = save_screenshot(sb, username, "renew_modal_not_found")
    return (
        RenewStatus.RENEW_FAILED,
        f"未找到或无法打开 Renew 模态框。截图：{screenshot}",
    )


def submit_renew(sb, account: Account) -> tuple[RenewStatus, str]:
    """
    检查模态框是否有验证控件。

    有验证时：停止该账号流程、保存截图、返回人工验证状态。
    无验证时：点击 Renew，并分析页面 alert。
    """
    username = account.username

    if modal_requires_verification(sb):
        screenshot = save_screenshot(sb, username, "renew_verification_required")
        return (
            RenewStatus.NEEDS_MANUAL_VERIFICATION,
            f"Renew 模态框需要人工验证。截图：{screenshot}",
        )

    print("🖱️ 提交 Renew...")

    clicked = False

    try:
        buttons = sb.find_elements("div.modal.show button")

        for button in buttons:
            button_text = (button.text or "").strip().lower()

            if "renew" in button_text and "cancel" not in button_text:
                sb.execute_script(
                    """
                    (function(element) {
                        element.scrollIntoView({
                            behavior: "instant",
                            block: "center"
                        });
                    })(arguments[0]);
                    """,
                    button,
                )

                time.sleep(0.5)
                button.click()

                clicked = True
                print("✅ 已点击 Renew 提交按钮。")
                break

    except Exception as exc:
        print(f"⚠️ 查找 Renew 提交按钮异常：{exc}")

    if not clicked:
        screenshot = save_screenshot(sb, username, "renew_submit_button_not_found")
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
        screenshot = save_screenshot(sb, username, "renew_result_unknown")
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
    """登录成功后的完整业务续期流程。"""
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
# 调度与报告
# ============================================================

def build_sb_kwargs() -> dict:
    """生成 SeleniumBase SB 参数。"""
    kwargs = {
        "uc": True,
        "headless": HEADLESS,
    }

    if USE_XVFB and not HEADLESS:
        kwargs["xvfb"] = True
        kwargs["xvfb_metrics"] = XVFB_METRICS

    if IS_PROXY:
        proxy = PROXY_SERVER or "http://127.0.0.1:1081"
        kwargs["proxy"] = proxy
        print(f"🔗 使用代理：{proxy}")
    else:
        print("🌐 未使用代理，直连访问。")

    return kwargs


def print_exit_ip(sb):
    """输出当前出口 IP，失败不影响续期。"""
    try:
        sb.open("https://api.ip.sb/ip")
        time.sleep(1)
        ip = (sb.get_text("body") or "").strip()
        print(f"📍 当前出口 IP：{ip}")
    except Exception as exc:
        print(f"ℹ️ 无法获取出口 IP：{exc}")


def run_account(account: Account, sb_kwargs: dict) -> AccountResult:
    """独立运行一个账号。"""
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
        print(f"💥 账号运行异常：{message}")

    result = AccountResult(
        username=account.username,
        status=status,
        message=message,
        timestamp=now_bj(),
    )

    print(
        f"{status_icon(status)} {mask_email(account.username)} "
        f"=> {status.value} | {message}"
    )

    send_account_notification(result)
    return result


def build_summary(results: list[AccountResult]) -> str:
    """生成控制台、TXT、Telegram 汇总。"""
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
        f"🧑‍💻 需要人工验证：{counts[RenewStatus.NEEDS_MANUAL_VERIFICATION]}",
        f"🔒 登录失败：{counts[RenewStatus.LOGIN_FAILED]}",
        f"⚠️ 续期失败：{counts[RenewStatus.RENEW_FAILED]}",
        f"💥 脚本异常：{counts[RenewStatus.SCRIPT_ERROR]}",
        "",
        "📋 账号明细：",
    ]

    for index, result in enumerate(results, start=1):
        lines.append(
            f"{index}. {status_icon(result.status)} "
            f"{mask_email(result.username)} | "
            f"{result.status.value} | "
            f"{result.message}"
        )

    return "\n".join(lines)


def write_reports(results: list[AccountResult]):
    """写入 JSON 和 TXT 结果报告。"""
    summary = build_summary(results)

    data = {
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
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    REPORT_TXT_FILE.write_text(
        summary,
        encoding="utf-8",
    )

    print(f"📄 已生成 JSON 报告：{REPORT_JSON_FILE}")
    print(f"📄 已生成文本报告：{REPORT_TXT_FILE}")


def main():
    print("#" * 60)
    print("KataBump 多账号自动续期")
    print("验证安全模式：发现 Turnstile / 续期验证时标记人工处理")
    print("#" * 60)

    accounts = load_accounts()
    print(f"📦 已读取 {len(accounts)} 个账号。")

    sb_kwargs = build_sb_kwargs()
    results: list[AccountResult] = []

    for index, account in enumerate(accounts, start=1):
        print(f"\n🚀 处理第 {index}/{len(accounts)} 个账号")
        results.append(run_account(account, sb_kwargs))

    write_reports(results)

    summary = build_summary(results)
    print("\n" + summary)

    send_tg_text(summary)


if __name__ == "__main__":
    main()
