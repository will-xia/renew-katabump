#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import os
import re
import sys
import time
import subprocess
import json
import traceback
from urllib.parse import urlparse, parse_qs
import requests
import yaml
from seleniumbase import SB


class RenewError(Exception):
    """致命错误: 立即中断整个流程，并以非零退出码结束（CI 会标红）。

    「未到续期时间」不属于错误，走 skip 状态，不抛这个异常。
    """


#  失败证据留档（workflow 只上传 output/ 目录，写到别处等于丢弃）
OUTPUT_DIR = os.path.join(os.getcwd(), "output")
ARTIFACT_DIR = os.path.join(OUTPUT_DIR, "screenshots")


def _poll(fn, seconds: float, interval: float = 0.5):
    """反复调用 fn 直到返回真值，超时返回 None

    全文有好几处「等某个条件成立」的循环，统一走这里，省得每处
    各写一遍 sleep 间隔和超时判断。fn 抛异常视为「还没成立」。
    """
    deadline = time.time() + seconds
    while True:
        try:
            result = fn()
            if result:
                return result
        except Exception:
            pass
        if time.time() >= deadline:
            return None
        time.sleep(interval)

# ==========================================
# ========== Mihomo 代理池模块 ==============
# ==========================================
# 版本号只写一处: 下载 URL 和拉订阅用的 UA 都从这里派生，
# 否则升级 mihomo 时漏改一处，UA 就会和实际版本对不上。
MIHOMO_VERSION = "v1.19.29"
MIHOMO_PATH = os.path.join(os.getcwd(), "mihomo")
MIHOMO_CONFIG = os.path.join(os.getcwd(), "config.yaml")
MIHOMO_LOG = os.path.join(os.getcwd(), "mihomo.log")
MIHOMO_CONTROLLER = "http://127.0.0.1:9090"
MIHOMO_MIXED_PORT = 7890
MIHOMO_GROUP = "MyGroup"
# provider 名字同时用在配置里、proxy-groups.use 里和控制 API 的 URL 里，
# 文件名也由它派生，三处必须一致，所以只留一个来源。
MIHOMO_PROVIDER = "sub1"
MIHOMO_SUB_FILE = os.path.join(os.getcwd(), f"{MIHOMO_PROVIDER}.yaml")
MIHOMO_PROVIDER_API = f"{MIHOMO_CONTROLLER}/providers/proxies/{MIHOMO_PROVIDER}"

# 拉订阅用 clash 系 UA: 部分机场只对这类 UA 返回 Clash 格式
_SUB_UA = f"mihomo/{MIHOMO_VERSION}"

_mihomo_proc = None   # 用于退出时清理


# 需要从任何输出里抹掉的凭据。SUB_URL 本身就是凭据，而 mihomo 以
# log-level: debug 运行时会把订阅 URL 打进自己的日志。
# 抹除统一在 _mihomo_log() 里做，不依赖每个调用点自觉 —— 靠自觉的
# 版本已经漏过两处（原来那两个直接 dump mihomo.log 的地方）。
_SECRETS: set = set()


def _register_secret(value: str):
    """登记一个凭据及其常见变形，供 _redact() 抹除"""
    if not value or len(value) < 8:
        return
    value = value.strip()
    _SECRETS.add(value)
    _SECRETS.add(value.rstrip("/"))
    try:
        _SECRETS.add(requests.utils.quote(value, safe=""))
        u = urlparse(value)
        if u.netloc:
            _SECRETS.add(u.netloc)
        # 路径末段和 query 值往往就是 token，报错信息里可能只带这一段
        for seg in u.path.split("/"):
            if len(seg) >= 8:
                _SECRETS.add(seg)
        for vals in parse_qs(u.query).values():
            for v in vals:
                if len(v) >= 8:
                    _SECRETS.add(v)
    except Exception:
        pass


def _redact(text) -> str:
    """把已登记的凭据替换成 ***（长的先替换，避免子串把长串切碎）"""
    if text is None:
        return ""
    out = str(text)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        out = out.replace(secret, "***")
    return out


def _mihomo_log(msg):
    """打印一条 mihomo 相关日志。

    抹除凭据放在这个漏斗里，而不是散在各个调用点: 所有要进 CI 日志的
    mihomo 消息都要经过这里，在这里保证一次，就不会有哪个新增的诊断
    忘了包 _redact() 而把订阅链接漏进公开日志。_redact 是幂等的。
    """
    msg = _redact(msg)
    print(f"[Mihomo] {msg}")
    try:
        with open(MIHOMO_LOG, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def setup_mihomo(sub_url: str) -> bool:
    """下载 Mihomo、生成配置、启动，并装好一份**能被加载**的节点集

    返回 True 即保证 mihomo 已经成功加载了订阅（坏节点已剔除）。
    剔除放在这里而不是留给调用方，是为了让「setup 成功」这件事本身
    就意味着节点可用 —— 否则谁跳过剔除直接读节点，就会撞回原来的坑。
    """
    global _mihomo_proc

    if not sub_url:
        return False

    # 订阅链接是凭据，在拿到它的同一处登记，之后所有日志自动抹除。
    # 放在这里而不是 main，是为了让「持有」和「登记」是同一个动作。
    _register_secret(sub_url)
    _mihomo_log("检测到 SUB_URL，准备启动代理池...")

    # 1. 下载二进制（若不存在）
    if not os.path.exists(MIHOMO_PATH):
        _mihomo_log(f"下载 mihomo-linux-amd64 {MIHOMO_VERSION}...")
        try:
            url = (f"https://github.com/MetaCubeX/mihomo/releases/download/"
                   f"{MIHOMO_VERSION}/mihomo-linux-amd64-{MIHOMO_VERSION}.gz")
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

    # 2. 先自己把订阅拉下来写成本地文件，再让 mihomo 读这个文件。
    #    不让它直连订阅 URL，是因为它遇到一个解析不了的节点就整份作废，
    #    而本地文件模式下我们可以把坏节点剔掉再喂给它。
    try:
        proxies = fetch_subscription(sub_url)
        _write_sub_file(proxies)
    except Exception as e:
        _mihomo_log(f"订阅下载/解析失败: {e}")
        return False

    # 3. 生成配置（不再包含订阅链接，配置本身也就不含凭据了）
    config_yaml = f"""
mixed-port: {MIHOMO_MIXED_PORT}
allow-lan: false
mode: rule
log-level: debug
external-controller: 127.0.0.1:9090
proxy-providers:
  {MIHOMO_PROVIDER}:
    type: file
    path: ./{os.path.basename(MIHOMO_SUB_FILE)}
    health-check:
      enable: true
      interval: 600
      url: http://www.gstatic.com/generate_204
proxy-groups:
  - name: {MIHOMO_GROUP}
    type: select
    use:
      - {MIHOMO_PROVIDER}
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

    # 4. 语法检查
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

    # 5. 启动 Mihomo
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

    # 6. 等待就绪（最多 15 秒）
    mihomo_ready = False
    for i in range(15):
        time.sleep(1)
        # 检查进程是否还活着
        if _mihomo_proc and _mihomo_proc.poll() is not None:
            _mihomo_log(f"⚠️ Mihomo 进程已退出 (code={_mihomo_proc.returncode})")
            _log_mihomo_tail()
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
        _log_mihomo_tail()
        # 仍然尝试继续（可能 API 延迟绑定）

    # 7. 让 mihomo 真正加载这份订阅，顺手剔掉它解析不了的节点。
    #    必须在进程起来之后做 —— 校验器就是 mihomo 自己。
    before = len(proxies)
    kept, dropped = _prune_bad_proxies(proxies)
    if dropped:
        _mihomo_log(f"共剔除 {len(dropped)} 个无法解析的节点，"
                    f"{before} → {len(kept)} 个可用")
    if not kept:
        _mihomo_log(f"⚠️ 订阅里 {before} 个节点 mihomo 全都解析不了，无可用节点")
    return bool(kept)


# Mihomo 内置的占位/特殊节点，一个都不能当成真实代理。
#
# 其中 COMPATIBLE 是最坑的一个: provider 加载失败时代理组不会变空，
# 而是只剩这一个占位符，它的行为**等同 DIRECT**。
# 早先只过滤 DIRECT/REJECT，于是 COMPATIBLE 一路混过测速（走直连当然通），
# 日志显示「可用节点: 1」，实际流量从 runner 自己的 Azure 数据中心 IP 出去，
# Turnstile 对着数据中心 IP 只会给最硬的那档验证，必然卡死。
# 现在订阅由我们自己下载、坏节点会被剔除，所以走到「只有 COMPATIBLE」
# 基本只剩一种可能: sub1.yaml 里的节点 mihomo 全都不认。
_PLACEHOLDER_NODES = frozenset({
    "DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL",
})


def _filter_proxy_nodes(all_nodes, group: str = MIHOMO_GROUP) -> list:
    """从代理组的 all 列表里挑出真实节点，保持原有顺序"""
    return [n for n in all_nodes if n not in _PLACEHOLDER_NODES and n != group]


def get_mihomo_proxies() -> tuple:
    """读代理组，返回 (真实节点, 被排除的占位节点)

    刻意不在这里打日志: 它会被 _poll 反复调用，每次都打就是十来行重复。
    日志留给 load_mihomo_nodes 在拿到最终结果后打一次。
    """
    r = requests.get(f"{MIHOMO_CONTROLLER}/proxies/{MIHOMO_GROUP}", timeout=10)
    all_nodes = r.json().get("all", [])
    return (_filter_proxy_nodes(all_nodes),
            [n for n in all_nodes if n in _PLACEHOLDER_NODES or n == MIHOMO_GROUP])


def _refresh_provider() -> tuple:
    """刷新订阅 provider，返回 (状态码, 响应体)；异常返回 (-1, 错误串)

    失败时 mihomo 会在响应体里带上真正的解析错误（比如
    "proxy 34 error: encoding/hex: invalid byte"），这是定位问题的唯一线索。
    """
    try:
        r = requests.put(MIHOMO_PROVIDER_API, timeout=15)
        detail = ""
        if r.status_code >= 300:
            detail = f" | mihomo 返回: {r.text[:300].strip()}"
        _mihomo_log(f"刷新 provider: {r.status_code}{detail}")
        return r.status_code, r.text
    except Exception as e:
        _mihomo_log(f"刷新 provider 失败: {e}")
        return -1, str(e)


# mihomo 只报坏节点的下标，不报名字，例如:
#   {"message":"proxy 34 error: encoding/hex: invalid byte: U+0068 'h'"}
_BAD_PROXY_RE = re.compile(r"proxy\s+(\d+)\s+error")


def fetch_subscription(sub_url: str) -> list:
    """下载订阅并解析出 proxies 列表

    自己下载而不是让 mihomo 直连订阅，是为了能在喂给它之前剔掉坏节点 ——
    mihomo 只要遇到一个解析不了的节点，就会把整份 provider 判为失败，
    198 个好节点会因为 1 个坏节点全部作废（实测就是这么挂的）。
    """
    r = requests.get(sub_url, headers={"User-Agent": _SUB_UA}, timeout=30)

    # 元信息在成功和失败时都要报，且只报可安全公开的部分:
    # 链接本身是凭据，响应体里是节点密码，都不能进日志。
    # subscription-userinfo 是关键 —— 流量耗尽/套餐过期会直接体现在这里。
    meta = (f"HTTP {r.status_code}, {len(r.content)} 字节, "
            f"content-type={r.headers.get('content-type', '?')}")
    userinfo = r.headers.get("subscription-userinfo")
    if userinfo:
        meta += f", subscription-userinfo={userinfo}"
    _mihomo_log(f"订阅响应: {meta}")

    if r.status_code >= 400:
        # 错误响应通常是短 JSON/文本提示，不含节点凭据
        raise RuntimeError(f"订阅返回 {r.status_code}: {r.text[:200].strip()!r}")

    # HTML 单独判一下: 直接丢给 yaml.safe_load 会报一句像
    # "mapping values are not allowed here: --tab-size-preference: 4"
    # 这样指着 CSS 的错，完全看不出真正发生了什么。
    ctype = (r.headers.get("content-type") or "").lower()
    head = r.content[:512].lstrip().lower()
    if "html" in ctype or head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        raise RuntimeError(
            f"订阅返回的是 HTML 页面而不是 Clash 配置"
            f"（{len(r.content)} 字节, content-type={ctype}）。"
            "常见原因: 短时间内拉取太频繁被限流、订阅需要人机验证、"
            "或链接已失效被跳到了网页版。")

    data = yaml.safe_load(r.content)
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        raise ValueError("订阅不是 Clash/mihomo YAML 格式（找不到 proxies 列表），"
                         "可能是 base64 的 V2Ray 订阅")
    proxies = data["proxies"]
    _mihomo_log(f"订阅解析成功: {len(proxies)} 个节点")
    return proxies


def _write_sub_file(proxies: list):
    """把节点列表写成 mihomo 的 file provider 输入"""
    with open(MIHOMO_SUB_FILE, "w") as f:
        yaml.safe_dump({"proxies": proxies}, f,
                       allow_unicode=True, sort_keys=False, default_flow_style=False)


def _describe_proxy(p: dict) -> str:
    """描述一个节点，只给名字/类型/字段名，不打印任何字段值（里面是密码）"""
    if not isinstance(p, dict):
        return f"<非字典节点: {type(p).__name__}>"
    return (f"name={_redact(p.get('name'))!r} type={p.get('type')!r} "
            f"字段: {','.join(sorted(str(k) for k in p.keys()))}")


def _prune_bad_proxies(proxies: list, max_drops: int = 25) -> tuple:
    """用 mihomo 自己的解析器逐个剔除解析不了的节点，返回 (剩余节点, 被剔除的)

    每轮: 写文件 → 让 mihomo 重新加载 → 若报 "proxy N error" 就丢掉第 N 个。
    删除会让后面的下标前移，所以每轮都重新读它报的下标，不做预测。
    用 mihomo 的解析器当校验器，好处是判断标准和它实际加载时完全一致，
    不需要在 Python 里重新实现一遍各种协议的字段校验。
    """
    dropped = []
    for _ in range(max_drops):
        _write_sub_file(proxies)
        code, msg = _refresh_provider()
        if 0 <= code < 300:
            return proxies, dropped

        m = _BAD_PROXY_RE.search(msg or "")
        if not m:
            _mihomo_log("⚠️ provider 报错但不是坏节点导致的，停止剔除")
            return proxies, dropped

        idx = int(m.group(1))
        if not (0 <= idx < len(proxies)):
            _mihomo_log(f"⚠️ mihomo 报的下标 {idx} 超出范围（共 {len(proxies)} 个），停止剔除")
            return proxies, dropped

        bad = proxies.pop(idx)
        dropped.append(bad)
        _mihomo_log(f"🗑️ 剔除无法解析的节点 [{idx}] {_describe_proxy(bad)}")

    _mihomo_log(f"⚠️ 已连续剔除 {max_drops} 个仍未加载成功，放弃")
    return proxies, dropped


def _log_provider_info():
    """打印 mihomo 侧的 provider 状态（节点数 / 上次更新时间）"""
    try:
        d = requests.get(MIHOMO_PROVIDER_API, timeout=10).json()
        _mihomo_log(f"provider 状态: vehicleType={d.get('vehicleType')}, "
                    f"updatedAt={d.get('updatedAt')}, "
                    f"节点数={len(d.get('proxies') or [])}")
    except Exception as e:
        _mihomo_log(f"读取 provider 状态失败: {e}")


def _mihomo_log_tail(lines: int = 80) -> str:
    """读 mihomo 日志尾部（凭据由 _mihomo_log / 写出口统一抹除）"""
    try:
        with open(MIHOMO_LOG, errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except Exception as e:
        return f"(读取 {MIHOMO_LOG} 失败: {e})"


def _log_mihomo_tail(lines: int = 40):
    """把 mihomo 日志尾部打进 CI 日志"""
    _mihomo_log(f"日志尾部:\n{_mihomo_log_tail(lines)}")


def _export_mihomo_log(lines: int = 80):
    """把 mihomo 日志尾部抹掉凭据后落进 output/，供 CI artifact 上传"""
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(os.path.join(OUTPUT_DIR, "mihomo.log"), "w") as f:
            f.write(_redact(_mihomo_log_tail(lines)))
        _mihomo_log("mihomo 日志尾部已导出: output/mihomo.log（已抹除凭据）")
    except Exception as e:
        _mihomo_log(f"导出 mihomo 日志失败: {e}")


def _diagnose_subscription():
    """代理组里没有真实节点时的诊断

    不再回头重新探测订阅 URL: 能走到这里，说明 fetch_subscription 已经
    成功下载并解析过（否则 setup_mihomo 就返回 False 了），所以订阅可达性、
    格式、UA 都已经被证实没问题。剩下的唯一可能是 mihomo 不认这批节点，
    答案在它自己的日志里。
    """
    _mihomo_log("=" * 20 + " 节点加载诊断 " + "=" * 20)
    _log_provider_info()
    _log_mihomo_tail()
    _export_mihomo_log()
    _mihomo_log("=" * 50)


def load_mihomo_nodes(timeout: int = 10) -> list:
    """取出代理组里的真实节点，等 mihomo 把加载结果反映到 API 上。

    这里不再重试 provider 刷新: provider 现在是 type: file，重刷只会让
    mihomo 重读我们刚写的同一份字节，结果不可能变；而坏节点已经在
    setup_mihomo 里剔干净了。只需要轮询等 /proxies 跟上加载结果。
    """
    seen = {"nodes": [], "placeholders": [], "error": None}

    def ready() -> bool:
        try:
            seen["nodes"], seen["placeholders"] = get_mihomo_proxies()
        except Exception as e:
            seen["error"] = e
            return False
        return bool(seen["nodes"])

    _poll(ready, timeout, interval=1.0)

    if seen["error"] and not seen["nodes"]:
        _mihomo_log(f"获取节点列表失败: {seen['error']}")
    _mihomo_log(f"获取到 {len(seen['nodes'])} 个候选节点"
                + (f"（已排除占位节点: {', '.join(seen['placeholders'])}）"
                   if seen["placeholders"] else ""))
    return seen["nodes"]


# 整组测速: mihomo v1.19 起，provider 里的节点不再单独暴露成
# /proxies/{name} —— /proxies 只列顶层代理（DIRECT/GLOBAL/组名 等），
# 逐个测速会全部 404（实测 195/195 全挂在这上面）。
#
# 替代接口是 /group/{组名}/delay: 一次请求测完整组，mihomo 内部自己并发，
# 只把**测通的**节点连延迟一起返回（{"节点名": 毫秒}），不通的不出现在
# 结果里。既比发 195 个请求快，也省掉了自己管并发。
# 全部超时时返回 504。
_DELAY_PROBE_MS = 3000
_DELAY_HTTP_TIMEOUT = 120


def test_mihomo_proxies(proxy_names: list) -> list:
    """让 mihomo 一次性测完整个代理组，返回健康节点（保持入参顺序）"""
    if not proxy_names:
        return []

    _mihomo_log(f"开始对 {len(proxy_names)} 个节点测速（整组一次性测）...")
    try:
        r = requests.get(
            f"{MIHOMO_CONTROLLER}/group/"
            f"{requests.utils.quote(MIHOMO_GROUP, safe='')}/delay",
            params={"timeout": str(_DELAY_PROBE_MS),
                    "url": "http://www.gstatic.com/generate_204"},
            timeout=_DELAY_HTTP_TIMEOUT
        )
    except Exception as e:
        _mihomo_log(f"⚠️ 测速请求失败: {type(e).__name__}: {e}")
        return []

    if r.status_code != 200:
        # 504 = 组内全部超时；其它状态码基本都是接口本身对不上
        # （比如再次升级 mihomo 后路由又变了），必须能一眼看出来
        _mihomo_log(f"⚠️ 测速接口返回 HTTP {r.status_code}: {r.text[:150].strip()}")
        return []

    try:
        delays = r.json()
    except Exception:
        _mihomo_log(f"⚠️ 测速结果不是 JSON: {r.text[:150].strip()}")
        return []

    def ok(name):
        d = delays.get(name)
        return isinstance(d, (int, float)) and not isinstance(d, bool) and d > 0

    healthy = [n for n in proxy_names if ok(n)]
    _mihomo_log(f"测速完成，健康节点: {len(healthy)}/{len(proxy_names)}")
    if healthy:
        ds = sorted(delays[n] for n in healthy)
        _mihomo_log(f"延迟 {ds[0]}~{ds[-1]}ms，中位 {ds[len(ds) // 2]}ms")
    return healthy


def _pick_node(pool: list, index: int, attempt: int, known_good) -> tuple:
    """挑这次尝试用哪个节点，返回 (节点名, 新的轮换下标, 是否是复用)

    首次尝试优先复用「上一个账号刚登录成功的那个节点」: 能否过 Turnstile
    几乎完全取决于出口 IP 的口碑，同一份池子里有的节点首击就过、有的连试
    4 次都不行。既然刚证明过某个出口能过，就没道理再去随机摸一个 ——
    实测出现过第一个账号在某节点顺利登录、第二个账号被切到另一批节点上
    连挂 5 次的情况。重试时才回到轮换，避免死磕一个已经不灵的节点。
    """
    if attempt == 0 and known_good:
        return known_good, index, True
    return pool[index % len(pool)], index + 1, False


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
COOKIES_DIR = os.path.join(os.getcwd(), "cookies")


def _cookies_path(email: str) -> str:
    """每个账号一个 cookie 文件

    文件名带上完整邮箱的哈希: 只取 @ 前面那段会让 a@x.com 和 a@y.com 落到
    同一个文件。cookies 现在跨运行复用（CI 缓存），一旦撞名就会拿另一个
    账号的会话去登录，属于会静默串号的那类 bug。
    """
    # 先归一化再派生两部分: 否则 "A@B.com" 和 " a@b.com " 哈希相同、
    # 可读前缀却不同，同一个账号会拿到两个 cookie 文件
    # （其中一个文件名还带着开头的空格）。
    email = (email or "").strip().lower()
    local = re.sub(r"[^a-z0-9_-]", "_", email.split("@")[0]) or "account"
    digest = hashlib.sha256(email.encode()).hexdigest()[:8]
    return os.path.join(COOKIES_DIR, f"{local}-{digest}.json")


def _save_cookies(sb, email: str):
    """Save browser cookies to disk for future cookie-based authentication"""
    try:
        os.makedirs(COOKIES_DIR, exist_ok=True)
        cookies = sb.get_cookies()
        if cookies:
            path = _cookies_path(email)
            with open(path, "w") as f:
                json.dump(cookies, f)
            print(f"  💾 Cookies 已保存到 {path}")
    except Exception as e:
        print(f"  ⚠️ 保存 cookies 失败: {e}")


def _load_cookies(sb, email: str) -> bool:
    """Try loading saved cookies and navigate to dashboard.
      Returns True if cookies valid (already logged in), False otherwise.
    """
    path = _cookies_path(email)
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            cookies = json.load(f)
        if not isinstance(cookies, list):
            return False
        # Use regular navigation so CDP session stays stable for cookie ops
        sb.get(BASE_URL)
        time.sleep(3)
        for cookie in cookies:
            try:
                sb.add_cookie(cookie)
            except Exception:
                pass
        sb.get(BASE_URL)
        time.sleep(2)
        url = (sb.get_current_url() or "").lower()
        title = (sb.get_title() or "").lower()
        if url.startswith(f"{BASE_URL.lower()}/dashboard") or "dashboard | katabump" in title:
            print("  ✅ 使用已保存的 cookies 登录（跳过 Turnstile）")
            return True
    except Exception as e:
        print(f"  ⚠️ cookies 加载失败: {e}")
    try:
        os.remove(path)
    except Exception:
        pass
    return False


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


# 取不到窗口信息时的兜底值（窗口按 set_window_size(1280, 900) 算，
# Linux 版 Chrome 的标签栏+地址栏合计约 88px；实测 CI 里是 141px，
# 所以正常路径一定要读实测值，这里只是最后的保险）。
# 宁可用估算值，也不能退化成零偏移 —— 那等于把视口坐标当屏幕坐标用。
_WIN_FALLBACK = {"oh": 900, "ih": 812}


def _screen_offset(wininfo) -> tuple:
    """由窗口信息算出「视口坐标 → 屏幕坐标」要加的偏移 (dx, dy)。

    xdotool 收的是 X 屏幕坐标，而 getBoundingClientRect() 给的是视口坐标，
    两者相差「窗口左上角位置 + 浏览器工具栏高度」。漏掉这段偏移时，
    点击会稳定落在目标上方 80~140px 处 —— 日志看起来一切正常，实际全打空。
    """
    if not wininfo or not wininfo.get("oh") or not wininfo.get("ih"):
        wininfo = _WIN_FALLBACK
    bar = max(0, int(wininfo["oh"]) - int(wininfo["ih"]))
    return int(wininfo.get("sx") or 0), int(wininfo.get("sy") or 0) + bar


def _to_screen_point(wininfo, vx, vy) -> tuple:
    """视口坐标 (vx, vy) → 屏幕坐标，供 xdotool 使用"""
    dx, dy = _screen_offset(wininfo)
    return int(vx + dx), int(vy + dy)


def _xdotool_click(sb, vx, vy):
    """在**视口坐标** (vx, vy) 处做一次 OS 级点击。

    刻意收视口坐标而不是屏幕坐标: 全文所有坐标来源
    （_TS_BOX_JS / _ALTCHA_EXPAND_JS / _altcha_checkbox_coords）给的都是
    视口坐标，没有一处真正产出屏幕坐标。换算放在这个唯一的 OS 边界里，
    调用方就不可能忘记 —— 之前换算靠调用方自觉，Turnstile 那条路径漏了，
    六次点击全部打在 widget 上方，日志还打得像模像样。
    """
    wininfo = None
    try:
        wininfo = _js(sb, _WININFO_JS)
    except Exception:
        pass
    x, y = _to_screen_point(wininfo, vx, vy)
    print(f"   视口({vx:.0f},{vy:.0f}) → 屏幕({x},{y})")

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
#
# 这里读到的是**视口坐标**，直接交给 _xdotool_click 即可 ——
# 换算成屏幕坐标由它内部统一负责。
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


# checkbox 中心实测在容器左边缘 +21px 处，多备几个候选值应对
# 不同 data-size 下的细微差异。旧版本用的 (10, 14, 8, 18) 全都偏在
# checkbox 左侧，叠加当时缺失的坐标换算，六次点击一次都没命中。
_TS_CHECKBOX_X_OFFSETS = (21, 25, 18, 30)


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


#  人机验证处理
def _wait_turnstile_box(sb, seconds: int = 15):
    """等 widget 渲染出可见容器（api.js 是 async，容器有尺寸才说明渲染完成）"""
    box = _poll(lambda: _turnstile_box(sb), seconds, interval=1.0)
    if box:
        print(f"✅ 定位到 Turnstile widget: "
              f"({box['x']:.0f},{box['y']:.0f}) {box['w']:.0f}x{box['h']:.0f}")
    else:
        print("⚠️ Turnstile widget 未渲染出可见容器")
    return box


def _wait_turnstile_solved(sb, seconds: float, label: str) -> bool:
    """轮询 hidden input，拿到 token 即算通过"""
    if not _poll(lambda: _js(sb, _SOLVED_JS), seconds):
        return False
    print(f"✅ Turnstile 已通过（{label}，token 长度 {_js(sb, _TOKEN_LEN_JS)}）")
    return True


def _reset_turnstile(sb):
    try:
        _js(sb, "(function(){ try { window.turnstile.reset(); return 'reset'; }"
                " catch(e) { return 'reset failed: ' + e.message; } })()")
        time.sleep(2)
    except Exception as e:
        print(f"⚠️ turnstile.reset() 调用异常: {e}")


def handle_turnstile(sb) -> bool:
    """处理登录页的 Cloudflare Turnstile。

    只走 xdotool 的 OS 级点击。SeleniumBase 那两个内置方法
    （uc_gui_click_cf / uc_gui_click_captcha）在这个页面上是纯空转:
    它们靠**普通 DOM 里的 iframe 选择器**定位 challenge，而这里
    document.querySelectorAll('iframe') 返回 0 个（widget 的 iframe 在
    shadow root 内），选择器命不中就静默 return，连异常都不抛。
    之前日志里那 12 次「尝试」实际一次都没点下去，纯粹浪费两分钟。
    """
    print("🔍 处理 Cloudflare Turnstile 验证...")
    _ensure_connected(sb)

    for round_num in range(2):
        if round_num > 0:
            print(f"🔄 第 {round_num + 1} 轮：重新加载页面后重试...")
            try:
                sb.uc_open_with_reconnect(sb.get_current_url(), reconnect_time=10)
                time.sleep(8)
            except Exception as e:
                print(f"⚠️ 页面重载异常: {e}")

        box = _wait_turnstile_box(sb)

        # managed / 隐形模式可能自动放行，先等一会儿，避免多余点击
        print("⏳ 等待 Turnstile 自动通过...")
        if _wait_turnstile_solved(sb, 12, f"自动通过，第 {round_num + 1} 轮"):
            return True
        if not box:
            continue

        last_attempt = len(_TS_CHECKBOX_X_OFFSETS) - 1
        for attempt, dx in enumerate(_TS_CHECKBOX_X_OFFSETS):
            print(f"🖱️ xdotool 点击 Turnstile checkbox，"
                  f"容器左偏移 {dx}px [尝试 {attempt + 1}]")
            _xdotool_click(sb, box["x"] + dx, box["y"] + box["h"] / 2)
            # 模拟人类操作：点击后短暂停顿，让浏览器处理事件
            time.sleep(0.5 + (time.time() % 1) * 0.3)

            if _wait_turnstile_solved(sb, 15, "xdotool 点击"):
                return True

            # 最后一次就不用再 reset 了 —— 紧接着要么重载页面，要么收摊
            if attempt < last_attempt:
                print(f"⚠️ 第 {attempt + 1} 次未通过，reset widget 后重试...")
                _reset_turnstile(sb)
                box = _turnstile_box(sb) or box

    _turnstile_diagnostics(sb, "turnstile_fail")
    return False


#  账户登录
def login(sb, email: str, password: str) -> bool:
    # 尝试使用已保存的 cookies 跳过登录（跳过 Turnstile）
    print("🔍 检查已保存的 cookies...")
    if _load_cookies(sb, email):
        print(f"✅ 使用 cookies 跳过登录（跳过 Turnstile）")
        return True

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
        _save_cookies(sb, email)
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
            print("🖱️  ALTCHA点击复选框")
            _xdotool_click(sb, coords["cx"], coords["cy"])

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
            # 配了 SUB_URL 就是明确要走代理。此时**不能**回退直连:
            # runner 自己的 IP 属于数据中心网段，Turnstile 只会给最硬的
            # 那档验证，跑满 90 分钟也过不去，还白留一条失败记录。
            all_nodes = load_mihomo_nodes()
            if not all_nodes:
                _diagnose_subscription()
                stop_mihomo()
                raise RenewError(
                    "代理组里没有任何真实节点（只剩 COMPATIBLE 占位符）。"
                    "订阅本身已下载解析成功，所以问题在 mihomo 不认这批节点，"
                    "看上面的 provider 状态和 mihomo 日志。"
                    "拒绝用 runner 自身的数据中心 IP 直连硬闯 Turnstile。")

            proxy_pool = test_mihomo_proxies(all_nodes)
            if not proxy_pool:
                stop_mihomo()
                raise RenewError(
                    f"{len(all_nodes)} 个节点全部测速不通，无可用出口。")

            import random
            random.shuffle(proxy_pool)
            _mihomo_log(f"可用节点: {len(proxy_pool)}")
            max_retries = min(5, len(proxy_pool))
        else:
            raise RenewError("Mihomo 初始化失败（订阅下载/解析失败或进程未起来），"
                             "且已配置 SUB_URL，拒绝回退直连。")
    elif is_proxy:
        print(f"🔗 挂载单代理: {proxy_str}")
        max_retries = 3
    else:
        print("🌐 未使用代理，直连访问")

    stats = {"processed": 0, "success": 0, "skipped": 0}
    total = len(users)
    # 最近一次登录成功所用的节点，跨账号复用（见 run_account 里的说明）
    known_good_node = None

    def run_account(idx, user, email):
        """跑单个账号（含换节点重试），返回 "ok" / "skip"；失败抛 RenewError"""
        nonlocal proxy_index, known_good_node

        for attempt in range(max_retries):
            if attempt > 0:
                print(f"\n  [重试 {attempt + 1}/{max_retries}]")

            # 切换代理节点（挑选逻辑见 _pick_node）
            node_name = None
            if use_mihomo and proxy_pool:
                node_name, proxy_index, reused = _pick_node(
                    proxy_pool, proxy_index, attempt, known_good_node)
                if reused:
                    print("  ♻️ 复用上个账号登录成功的节点")
                switch_mihomo_proxy(node_name)
                time.sleep(1)

            # 构建浏览器参数
            sb_kwargs = {"uc": True, "headless": False, "xvfb": True}
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
                    known_good_node = node_name
                    return status

                # 这个节点也过不去了，别再让它拖累后面的账号
                if node_name and node_name == known_good_node:
                    print("  ♻️ 复用的节点这次没过，取消复用")
                    known_good_node = None

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
