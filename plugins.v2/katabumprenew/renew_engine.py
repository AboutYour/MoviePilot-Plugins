"""
Katabump 续期核心引擎（Python + Playwright）。

从 Node 版 action_renew.js 移植：
- 登录：等 Cloudflare Turnstile 令牌就绪再提交（必要时主动 turnstile.render()）
- 续期：See → Renew → 解 ALTCHA（穿透 shadow DOM + 真实点击）→ 确认 → 截图
- 顺序处理多账号，每个账号独立会话（登录前先 logout）

引擎不依赖 MoviePilot，可单独测试；日志通过传入的 logger 输出。
"""
import asyncio
import glob
import os
import random
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

ENGINE_VERSION = "1.3.7"

LOGIN_URL_DEFAULT = "https://dashboard.katabump.com/auth/login"
LOGOUT_URL = "https://dashboard.katabump.com/auth/logout"
VIEWPORT = {"width": 1280, "height": 720}

# Cloudflare 挑战相关域名：连这些都解析/连不通时，等满 Turnstile 超时没有意义
CF_PROBE_HOSTS = (
    "challenges.cloudflare.com",
    "cdn-cgi.cloudflare.com",
)
# 需要预解析/强制映射的 CF 主机（Turnstile 还会用随机子域 *.challenges.cloudflare.com）
CF_MAP_HOSTS = (
    "challenges.cloudflare.com",
    "cdn-cgi.cloudflare.com",
    "static.cloudflareinsights.com",
)
PUBLIC_DNS_SERVERS = (
    "223.5.5.5",      # 阿里
    "119.29.29.29",   # DNSPod
    "8.8.8.8",
    "1.1.1.1",
)
# Chromium DoH 模板（系统 DNS 坏时让浏览器自己解析）
DOH_TEMPLATES = (
    "https://dns.alidns.com/dns-query",
    "https://doh.pub/dns-query",
    "https://chrome.cloudflare-dns.com/dns-query",
    "https://dns.google/dns-query",
)

CF_URL_RE = re.compile(
    r"cloudflare|turnstile|challenges\.cloudflare\.com|cdn-cgi",
    re.I,
)
# 明确属于“网络/DNS 不可达”的 Chromium 失败原因（不是 IP 信誉拒发 token）
CF_HARD_FAIL_RE = re.compile(
    r"ERR_NAME_NOT_RESOLVED|ERR_CONNECTION_REFUSED|ERR_CONNECTION_RESET|"
    r"ERR_CONNECTION_CLOSED|ERR_CONNECTION_TIMED_OUT|ERR_TIMED_OUT|"
    r"ERR_ADDRESS_UNREACHABLE|ERR_NETWORK_CHANGED|ERR_INTERNET_DISCONNECTED|"
    r"ERR_PROXY_CONNECTION_FAILED|ERR_TUNNEL_CONNECTION_FAILED|"
    r"ERR_SOCKS_CONNECTION_FAILED|ERR_NAME_RESOLUTION_FAILED",
    re.I,
)

PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


def _log(logger, msg: str):
    if logger:
        logger.info(f"[Katabump] {msg}")
    else:
        print(f"[Katabump] {msg}")


# ============================================================
# 公共 DNS 预解析（绕过容器坏掉的 /etc/resolv.conf）
# ============================================================
def _dns_query_a(hostname: str, dns_server: str, timeout: float = 3.0) -> List[str]:
    """向指定公共 DNS 发起 A 查询，不依赖系统 resolver。"""
    name = (hostname or "").strip().rstrip(".")
    if not name:
        return []
    tid = random.randint(0, 65535)
    header = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname = b""
    for part in name.split("."):
        p = part.encode("idna")
        if len(p) > 63:
            return []
        qname += bytes([len(p)]) + p
    qname += b"\x00" + struct.pack("!HH", 1, 1)  # A IN
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(header + qname, (dns_server, 53))
        data, _ = sock.recvfrom(4096)
    except Exception:
        return []
    finally:
        try:
            sock.close()
        except Exception:
            pass
    if len(data) < 12:
        return []
    ancount = struct.unpack("!H", data[6:8])[0]
    i = 12
    # skip question
    try:
        while i < len(data) and data[i] != 0:
            i += 1 + data[i]
        i += 5  # 0 + type + class
    except Exception:
        return []
    ips: List[str] = []
    for _ in range(ancount):
        if i >= len(data):
            break
        try:
            if data[i] & 0xC0 == 0xC0:
                i += 2
            else:
                while i < len(data) and data[i] != 0:
                    i += 1 + data[i]
                i += 1
            if i + 10 > len(data):
                break
            rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[i:i + 10])
            i += 10
            rdata = data[i:i + rdlen]
            i += rdlen
            if rtype == 1 and rdlen == 4:
                ips.append(socket.inet_ntoa(rdata))
        except Exception:
            break
    return ips


def resolve_a_system(hostname: str) -> Optional[str]:
    """系统 resolver 解析，返回第一个 IPv4。"""
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        for fam, _t, _p, _c, sockaddr in infos:
            ip = sockaddr[0]
            if fam == socket.AF_INET and ip:
                return ip
    except Exception:
        pass
    return None


def resolve_a_public(hostname: str, logger=None) -> Optional[str]:
    """用多个公共 DNS 解析主机名，返回第一个 A 记录。"""
    for server in PUBLIC_DNS_SERVERS:
        ips = _dns_query_a(hostname, server)
        if ips:
            _log(logger, f"公共 DNS {server} 解析 {hostname} -> {ips[0]}"
                         f"{(' (+' + str(len(ips) - 1) + ')') if len(ips) > 1 else ''}")
            return ips[0]
    _log(logger, f"公共 DNS 均未能解析 {hostname}")
    return None


def resolve_a_any(hostname: str, logger=None) -> Optional[str]:
    """优先系统 DNS，失败再公共 DNS。"""
    ip = resolve_a_system(hostname)
    if ip:
        _log(logger, f"系统 DNS 解析 {hostname} -> {ip}")
        return ip
    return resolve_a_public(hostname, logger)


def system_dns_ok(hostname: str) -> bool:
    return resolve_a_system(hostname) is not None


def probe_cf_challenge_subdomain_dns(logger=None) -> bool:
    """
    Turnstile 会请求随机子域，如 brunhild.challenges.cloudflare.com。
    主域 challenges.cloudflare.com 能解析 ≠ 这些子域能解析。
    返回 True 表示系统 DNS 能解析「挑战类」子域（抽查若干常见/探测名）。
    """
    samples = [
        "brunhild.challenges.cloudflare.com",
        "challenges.cloudflare.com",
    ]
    # 再加一个探测名：若存在通配记录应能解；无通配则与 brunhild 一起视为需映射
    probe = f"kbm{random.randint(10000, 99999)}.challenges.cloudflare.com"
    samples.append(probe)

    parent_ok = system_dns_ok("challenges.cloudflare.com")
    results = []
    any_sub_ok = False
    for h in samples:
        ok = system_dns_ok(h)
        results.append(f"{h.split('.')[0]}={'ok' if ok else 'fail'}")
        if ok and h != "challenges.cloudflare.com":
            any_sub_ok = True
    _log(logger, f"CF 子域 DNS 探测: parent={'ok' if parent_ok else 'fail'}; " + "; ".join(results))
    # 主域通但所有抽查子域都 fail → 典型坏 DNS（用户日志场景）
    if parent_ok and not any_sub_ok:
        return False
    # 主域都 fail
    if not parent_ok:
        return False
    return True


def build_cf_host_resolver_rules(logger=None, wildcard: bool = False) -> str:
    """
    生成 Chromium --host-resolver-rules。

    v1.3.7 调整：飞牛 NAS / 家庭宽带场景下，宿主机 Chrome 能正常登录，
    说明出口没有问题；强行把 *.challenges.cloudflare.com 映射到主域 IP
    可能让 Cloudflare 动态挑战资源出现 net::ERR_ABORTED / 空 iframe。
    因此默认只映射稳定主机，随机挑战子域优先交给 Chromium DoH 解析。
    只有环境变量 KATABUMP_CF_WILDCARD_MAP=1 时才启用通配映射。
    """
    rules: List[str] = []
    main_ip = resolve_a_any("challenges.cloudflare.com", logger)
    if main_ip:
        rules.append(f"MAP challenges.cloudflare.com {main_ip}")
        if wildcard:
            rules.append(f"MAP *.challenges.cloudflare.com {main_ip}")
            _log(logger, f"将 *.challenges.cloudflare.com 全部映射到 {main_ip}（强制通配模式）")
        else:
            _log(logger, "不再强制映射 *.challenges.cloudflare.com，随机挑战子域交给浏览器 DoH 解析")
    for host in CF_MAP_HOSTS:
        if host == "challenges.cloudflare.com":
            continue
        ip = resolve_a_any(host, logger)
        if ip:
            rules.append(f"MAP {host} {ip}")
    if rules:
        rules.append("EXCLUDE localhost")
        rules.append("EXCLUDE 127.0.0.1")
    return ", ".join(rules)


def chromium_doh_args() -> List[str]:
    """启用浏览器 DNS-over-HTTPS，系统 resolv 失败时仍可解析。"""
    templates = " ".join(DOH_TEMPLATES)
    # automatic 比 secure 更接近普通 Chrome：DoH 可用时使用，不可用时回退系统 DNS。
    # secure 在部分 NAS/容器网络中会导致挑战子资源被 Chromium 直接 abort。
    return [
        "--enable-features=DnsOverHttps",
        "--dns-over-https-mode=automatic",
        f"--dns-over-https-templates={templates}",
        "--disable-features=AsyncDns",
    ]


def nas_chromium_runtime_args() -> List[str]:
    """飞牛 NAS / Docker 内 Playwright Chromium 的 Turnstile 兼容参数。

    宿主机 Chrome 能登录但容器内 Chromium 出现 /turnstile/.../crashed_retry +
    net::ERR_ABORTED 时，常见不是账号或家庭出口问题，而是 headless Chromium 的
    WebGL/SwiftShader/沙箱/共享内存运行环境导致 Turnstile 小组件崩溃。
    """
    return [
        "--use-angle=swiftshader",
        "--use-gl=swiftshader",
        "--ignore-gpu-blocklist",
        "--enable-unsafe-swiftshader",
        "--disable-software-rasterizer",
        "--disable-features=site-per-process,IsolateOrigins,VizDisplayCompositor",
        "--disable-site-isolation-trials",
        "--font-render-hinting=none",
    ]


# ============================================================
# 代理解析 / Cloudflare 连通性探测
# ============================================================
def _moviepilot_proxy_raw() -> str:
    """读取 MoviePilot 的 PROXY_HOST（环境变量或 settings）。"""
    for k in ("PROXY_HOST", "proxy_host"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    try:
        from app.core.config import settings  # type: ignore
        v = getattr(settings, "PROXY_HOST", None) or getattr(settings, "proxy_host", None)
        if v:
            return str(v).strip()
    except Exception:
        pass
    return ""


def _env_proxy_raw() -> str:
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return ""


def _any_system_proxy_raw() -> Tuple[str, str]:
    """返回 (raw, source_label)。优先 PROXY_HOST，其次 HTTP(S)_PROXY。"""
    mp = _moviepilot_proxy_raw()
    if mp:
        return mp, "PROXY_HOST"
    env = _env_proxy_raw()
    if env:
        return env, "HTTP(S)_PROXY"
    return "", ""


def parse_proxy_server(raw: str) -> Optional[Dict[str, str]]:
    """
    把用户输入/环境变量里的代理串解析成 Playwright 可用的 dict。

    - socks5:// 自动改成 socks5h://（DNS 走代理，解决容器内 DNS 解析不了 CF 子域的问题）
    - 支持 user:pass@host:port 拆到 username/password（Playwright 不认写在 server 里的账号密码）
    - 无 scheme 时默认 http://
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    # 裸 host:port
    if "://" not in raw:
        raw = "http://" + raw

    # DNS 经代理解析：容器 DNS 常解不了 *.challenges.cloudflare.com
    if raw.lower().startswith("socks5://"):
        raw = "socks5h://" + raw[len("socks5://"):]

    try:
        u = urlparse(raw)
    except Exception:
        return {"server": raw}

    if not u.hostname:
        return {"server": raw}

    scheme = (u.scheme or "http").lower()
    # Playwright 认识 socks5 / socks5h / http / https
    if scheme == "socks5":
        scheme = "socks5h"

    server = f"{scheme}://{u.hostname}"
    if u.port:
        server += f":{u.port}"
    else:
        # 缺省端口
        if scheme in ("http", "https"):
            server += ":80" if scheme == "http" else ":443"
        elif scheme in ("socks5", "socks5h"):
            server += ":1080"

    out: Dict[str, str] = {"server": server}
    if u.username:
        out["username"] = unquote(u.username)
    if u.password:
        out["password"] = unquote(u.password)
    return out


def _tcp_reachable(host: str, port: int = 443, timeout: float = 5.0) -> Tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "ok"
    except socket.gaierror as e:
        return False, f"DNS 失败: {e}"
    except OSError as e:
        return False, f"连接失败: {e}"
    except Exception as e:
        return False, f"异常: {e}"


def probe_cloudflare(logger=None, timeout: float = 5.0) -> Tuple[bool, bool]:
    """
    直连探测 challenges.cloudflare.com。
    返回 (tcp_ok, dns_failed)：
      - tcp_ok: 至少有一个主机可连
      - dns_failed: 系统 DNS 对所有探测主机都解析失败
    """
    ok_any = False
    dns_fail_all = True
    details = []
    for host in CF_PROBE_HOSTS:
        ok, reason = _tcp_reachable(host, 443, timeout)
        details.append(f"{host}={'ok' if ok else reason}")
        if ok:
            ok_any = True
            dns_fail_all = False
        elif "DNS" not in reason:
            dns_fail_all = False
    _log(logger, "Cloudflare 直连探测: " + "; ".join(details))
    return ok_any, dns_fail_all and not ok_any


def resolve_browser_proxy(
    proxy_server: str,
    proxy_mode: str,
    logger=None,
) -> Tuple[Optional[Dict[str, str]], str, bool]:
    """
    决定 Playwright 使用的代理。

    proxy_mode:
      - auto   : 先测 CF 直连；不通再回退配置代理 / PROXY_HOST / 环境代理
      - direct : 强制直连
      - system : 用 PROXY_HOST 或环境变量代理
      - custom : 只用配置里的 proxy_server
    返回 (playwright_proxy_dict 或 None, 策略说明, need_dns_fix)
      need_dns_fix=True 时启动 Chromium DoH + 公共 DNS host-resolver-rules
    """
    mode = (proxy_mode or "auto").strip().lower()
    if mode not in ("auto", "direct", "system", "custom"):
        mode = "auto"

    cfg_raw = (proxy_server or "").strip()
    sys_raw, sys_src = _any_system_proxy_raw()
    cfg = parse_proxy_server(cfg_raw) if cfg_raw else None
    sys_proxy = parse_proxy_server(sys_raw) if sys_raw else None

    if mode == "custom":
        if not cfg:
            _log(logger, "proxy_mode=custom 但未填写代理，将直连 + DNS 修复")
            return None, "custom→直连(未配置)", True
        _log(logger, f"使用配置代理: {cfg.get('server')}")
        return cfg, "custom", False

    if mode == "system":
        if not sys_proxy:
            _log(logger, "proxy_mode=system 但无 PROXY_HOST/HTTP(S)_PROXY，将直连 + DNS 修复")
            return None, "system→直连(无系统代理)", True
        _log(logger, f"使用系统代理({sys_src}): {sys_proxy.get('server')}")
        return sys_proxy, f"system({sys_src})", False

    if mode == "direct":
        _log(logger, "强制直连（忽略配置/系统代理），启用 DNS 修复以防容器 resolv 异常")
        return None, "direct", True

    # ---- auto ----
    if cfg:
        _log(logger, f"auto：使用配置代理 {cfg.get('server')}")
        return cfg, "auto→custom", False

    tcp_ok, dns_failed = probe_cloudflare(logger)
    # 主域通 ≠ 随机子域通（Turnstile 用 brunhild.challenges.cloudflare.com）
    sub_dns_ok = probe_cf_challenge_subdomain_dns(logger)

    if tcp_ok:
        if sys_proxy:
            _log(logger, "auto：Cloudflare 主域直连可达，忽略系统代理，走本机出口")
        else:
            _log(logger, "auto：Cloudflare 主域直连可达，使用直连")
        # 子域 DNS 坏时必须开 MAP，否则浏览器会 ERR_NAME_NOT_RESOLVED
        if not sub_dns_ok:
            _log(logger, "auto：主域可达但挑战子域 DNS 异常 → 启用 *.challenges.cloudflare.com 映射")
            return None, "auto→direct+cf_subdomain_map", True
        return None, "auto→direct", False

    if sys_proxy:
        _log(logger, f"auto：Cloudflare 直连不可达，回退系统代理({sys_src}) {sys_proxy.get('server')}")
        # HTTP 代理通常由代理侧解析 DNS；仍建议 socks5h。子域映射在直连路径更关键。
        return sys_proxy, f"auto→system({sys_src})", False

    # 无代理：尽量用公共 DNS + DoH 修容器 DNS
    if dns_failed:
        _log(logger, "auto：系统 DNS 无法解析 Cloudflare，将启用公共 DNS 映射 + 浏览器 DoH")
    else:
        _log(logger, "auto：Cloudflare 直连 TCP 不通且无代理；仍尝试 DNS 修复后直连")
    return None, "auto→direct+dns_fix", True


# ============================================================
# chromium 自动安装：MoviePilot 容器有 playwright 库和系统依赖，
# 但不一定有 chromium 二进制。首次运行自动补装一次。
# ============================================================
def ensure_chromium(logger=None) -> Optional[str]:
    """确保有可用的 chromium，返回可执行路径（None 表示用 playwright 默认）。"""
    candidates = []
    ms_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    search_roots = [ms_root] if ms_root else []
    search_roots += [
        os.path.expanduser("~/.cache/ms-playwright"),
        "/ms-playwright",
        "/root/.cache/ms-playwright",
        "/moviepilot/.cache/ms-playwright",
    ]
    for root in search_roots:
        if not root:
            continue
        candidates += glob.glob(os.path.join(root, "chromium-*", "chrome-linux", "chrome"))
        candidates += glob.glob(os.path.join(root, "chromium-*", "chrome-linux", "headless_shell"))
    for c in candidates:
        if os.path.exists(c):
            _log(logger, f"发现已安装的 chromium: {c}")
            return c

    _log(logger, "未发现 chromium，尝试自动安装（首次约 150MB，请稍候）...")
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True, capture_output=True, timeout=600,
        )
        _log(logger, "chromium 安装完成")
    except Exception as e:
        _log(logger, f"chromium 自动安装失败: {e}（将尝试用系统 Chrome）")
        return None

    for root in search_roots:
        if not root:
            continue
        for c in glob.glob(os.path.join(root, "chromium-*", "chrome-linux", "chrome")):
            if os.path.exists(c):
                return c
    return None


# ============================================================
# Turnstile（登录）
# ============================================================
async def _get_turnstile_state(page) -> dict:
    try:
        return await page.evaluate(
            """() => {
                const els = Array.from(document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'));
                const vals = els.map(e => String(e.value || '').trim());
                const token = vals.find(v => v.length > 20) || '';
                const containers = Array.from(document.querySelectorAll('.cf-turnstile, [data-sitekey]'));
                const allIframes = Array.from(document.querySelectorAll('iframe'));
                const blankIframeCount = allIframes.filter(f => !String(f.getAttribute('src') || f.src || '').trim()).length;
                const iframeBrief = allIframes.slice(0, 6).map((f, idx) => ({
                    idx,
                    src: String(f.getAttribute('src') || f.src || '').slice(0, 180),
                    title: String(f.getAttribute('title') || '').slice(0, 80),
                    name: String(f.getAttribute('name') || '').slice(0, 80),
                    id: String(f.getAttribute('id') || '').slice(0, 80),
                    parentClass: String(f.parentElement ? f.parentElement.className || '' : '').slice(0, 120),
                    parentDataSitekey: String(f.parentElement ? f.parentElement.getAttribute('data-sitekey') || '' : '').slice(0, 60)
                }));
                let iframes = allIframes.filter(f => /turnstile|challenges\\.cloudflare|cf-chl|cloudflare/i.test([
                    f.src || '', f.getAttribute('src') || '', f.title || '', f.name || '', f.id || '',
                    f.parentElement ? (f.parentElement.className || '') : '',
                    f.parentElement ? (f.parentElement.getAttribute('data-sitekey') || '') : ''
                ].join(' ')));
                // 关键修复：部分 Turnstile iframe 初始为 about:blank 或无 src，无法通过 src/title 识别。
                // 只要页面存在 cf-turnstile/data-sitekey 容器，容器附近出现的 iframe 都按挑战 iframe 处理，避免误刷新打断加载。
                if (iframes.length === 0 && containers.length > 0 && allIframes.length > 0) {
                    iframes = allIframes;
                }
                return {
                    required: els.length > 0 || containers.length > 0 || iframes.length > 0,
                    token, inputCount: els.length, iframeCount: iframes.length,
                    allIframeCount: allIframes.length,
                    blankIframeCount,
                    containerCount: containers.length,
                    hasApi: typeof window.turnstile !== 'undefined',
                    readyState: document.readyState,
                    iframeBrief
                };
            }"""
        )
    except Exception:
        return {"required": False, "token": "", "inputCount": 0, "iframeCount": 0, "allIframeCount": 0, "containerCount": 0, "hasApi": False, "iframeBrief": [], "blankIframeCount": 0}


async def _rescue_blank_turnstile(page, logger) -> bool:
    """温和处理 Turnstile 空 iframe。

    v1.3.5 会删除空 iframe 并重渲染；从日志看这可能打断 Cloudflare 自己的 crashed_retry。
    v1.3.7 默认不删除 iframe，只触发表单交互并等待 CF 自恢复。
    只有设置 KATABUMP_TURNSTILE_FORCE_RERENDER=1 时才启用旧的强制重渲染。
    """
    force = (os.environ.get("KATABUMP_TURNSTILE_FORCE_RERENDER") or "").strip().lower() in ("1", "true", "yes", "on")
    if not force:
        try:
            await page.evaluate("""() => {
                for (const el of document.querySelectorAll('input, textarea')) {
                    try { el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); } catch(e) {}
                }
                try { window.dispatchEvent(new Event('focus')); } catch(e) {}
            }""")
            _log(logger, "检测到 Turnstile 空 iframe，已采用温和等待策略（不删除 iframe，不打断 CF crashed_retry）")
        except Exception:
            pass
        return True
    try:
        changed = await page.evaluate(
            """async () => {
                const sleep = ms => new Promise(r => setTimeout(r, ms));
                const containers = Array.from(document.querySelectorAll('.cf-turnstile, [data-sitekey]'));
                if (!containers.length) return false;
                let removed = 0;
                for (const c of containers) {
                    for (const f of Array.from(c.querySelectorAll('iframe'))) {
                        const src = String(f.getAttribute('src') || f.src || '').trim();
                        if (!src || src === 'about:blank') {
                            f.remove();
                            removed++;
                        }
                    }
                    c.removeAttribute('data-turnstile-widget-id');
                    delete c.dataset.__r;
                }
                for (let i = 0; i < 80 && typeof window.turnstile === 'undefined'; i++) await sleep(250);
                if (typeof window.turnstile === 'undefined' || typeof window.turnstile.render !== 'function') return removed > 0;
                for (const c of containers) {
                    const sitekey = c.getAttribute('data-sitekey');
                    if (!sitekey) continue;
                    try {
                        window.turnstile.render(c, {
                            sitekey,
                            callback: (t) => document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').forEach(i => i.value = t || ''),
                            'error-callback': () => {},
                            'expired-callback': () => {}
                        });
                    } catch (e) {}
                }
                await sleep(2500);
                return true;
            }"""
        )
        if changed:
            _log(logger, "检测到 Turnstile 空 iframe，已清理并尝试重新渲染（NAS/Chromium 兼容修复）")
        return bool(changed)
    except Exception as e:
        _log(logger, f"Turnstile 空 iframe 修复失败: {e}")
        return False


async def _render_turnstile(page, logger):
    try:
        await page.evaluate(
            """async () => {
                const sleep = ms => new Promise(r => setTimeout(r, ms));
                const containers = Array.from(document.querySelectorAll('.cf-turnstile, [data-sitekey]'));
                if (!containers.length) return;
                for (let i = 0; i < 50 && typeof window.turnstile === 'undefined'; i++) await sleep(200);
                if (typeof window.turnstile === 'undefined' || typeof window.turnstile.render !== 'function') return;
                for (const c of containers) {
                    const sitekey = c.getAttribute('data-sitekey');
                    if (!sitekey || c.querySelector('iframe') || c.dataset.__r === '1') continue;
                    c.dataset.__r = '1';
                    window.turnstile.render(c, {
                        sitekey,
                        callback: (t) => document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').forEach(i => i.value = t || '')
                    });
                }
                await sleep(1500);
            }"""
        )
    except Exception as e:
        _log(logger, f"Turnstile 主动渲染失败: {e}")


class CfNetworkTracker:
    """跟踪页面内 Cloudflare 资源加载失败，用于尽早判定“网络不可达”。"""

    def __init__(self):
        self.hard_fails = 0
        self.last_fail_url = ""
        self.last_fail_reason = ""
        self.fail_samples: List[str] = []
        self.turnstile_crash_retries = 0

    def on_request_failed(self, req, logger=None):
        try:
            url = req.url or ""
            if not CF_URL_RE.search(url):
                return
            reason = ""
            try:
                reason = req.failure or ""
            except Exception:
                reason = ""
            reason = str(reason or "")
            self.last_fail_url = url
            self.last_fail_reason = reason
            sample = f"{reason} @ {url[:120]}"
            if sample not in self.fail_samples and len(self.fail_samples) < 8:
                self.fail_samples.append(sample)
            if "turnstile" in url and "crashed_retry" in url:
                self.turnstile_crash_retries += 1
                if self.turnstile_crash_retries <= 3:
                    _log(logger, f"⚠️ Turnstile 小组件 crashed_retry({self.turnstile_crash_retries})：更像容器 Chromium/WebGL/SwiftShader 运行环境问题，不是账号或家庭出口问题")
            if CF_HARD_FAIL_RE.search(reason) or reason in ("net::ERR_ABORTED", "net::ERR_FAILED"):
                # ERR_ABORTED  alone 不一定致命（页面刷新也会 abort），配合 hard DNS 类错误才计分
                if CF_HARD_FAIL_RE.search(reason):
                    self.hard_fails += 1
                    _log(logger, f"⚠️ Cloudflare 网络失败({self.hard_fails}): {url[:160]}（{reason}）")
                else:
                    # 软失败只记日志一次
                    if self.hard_fails == 0 and len(self.fail_samples) <= 2:
                        _log(logger, f"⚠️ Cloudflare 请求中止: {url[:160]}（{reason}）")
            elif reason:
                if self.hard_fails == 0 and len(self.fail_samples) <= 2:
                    _log(logger, f"⚠️ Cloudflare 请求失败: {url[:160]}（{reason}）")
        except Exception:
            pass

    def should_early_abort(self, saw_iframe: bool, waited_s: float) -> bool:
        """
        iframe 一直没出来 + 已出现明确的 DNS/连接失败 → 不必空等到满超时。
        至少等 12s 给页面一次 reload 机会。
        """
        if saw_iframe:
            return False
        if waited_s < 12:
            return False
        return self.hard_fails >= 1


async def _prefill_login_fields(page, user: Dict[str, str], logger=None) -> bool:
    """先填账号密码再等 Turnstile。部分站点只有表单发生交互后才真正渲染/签发 Turnstile。"""
    try:
        email = page.locator('#email, input[name="email"], input[type="email"]').first
        pwd = page.locator('#password, input[name="password"], input[type="password"]').first
        await email.wait_for(state="visible", timeout=15000)
        await email.fill("")
        await email.fill(user["username"])
        await pwd.fill("")
        await pwd.fill(user["password"])
        try:
            await email.dispatch_event("input")
            await email.dispatch_event("change")
            await pwd.dispatch_event("input")
            await pwd.dispatch_event("change")
            await pwd.blur()
        except Exception:
            pass
        _log(logger, "已预填账号密码并触发表单事件，随后等待 Turnstile token")
        return True
    except Exception as e:
        _log(logger, f"预填账号密码失败，将继续按原流程等待: {e}")
        return False


async def wait_turnstile_token(page, logger, timeout_s: int = 120,
                               cf_tracker: Optional[CfNetworkTracker] = None) -> bool:
    _log(logger, "检查 Cloudflare Turnstile token...")
    st = await _get_turnstile_state(page)
    try:
        if st.get("allIframeCount", 0) > 0:
            _log(logger, f"Turnstile iframe 调试: {st.get('iframeBrief', [])}")
    except Exception:
        pass
    if st.get("token"):
        _log(logger, f"已有 Turnstile token（长度 {len(st['token'])}）")
        return True
    if not st.get("required"):
        _log(logger, "本次登录页未触发 Turnstile")
        return True

    if st.get("blankIframeCount", 0) > 0 and st.get("containerCount", 0) > 0 and st.get("hasApi"):
        await _rescue_blank_turnstile(page, logger)
        await page.wait_for_timeout(1500)
        st = await _get_turnstile_state(page)
    if st.get("iframeCount", 0) == 0 and st.get("containerCount", 0) > 0 and st.get("hasApi"):
        await _render_turnstile(page, logger)
        await page.wait_for_timeout(2000)
        st = await _get_turnstile_state(page)
        if st.get("token"):
            _log(logger, "主动渲染后已获得 token")
            return True

    start = time.time()
    last_log = 0
    reloaded = False
    blank_rescue_attempted = False
    saw_iframe = st.get("iframeCount", 0) > 0 or st.get("allIframeCount", 0) > 0
    while time.time() - start < timeout_s:
        st = await _get_turnstile_state(page)
        if st.get("iframeCount", 0) > 0 or st.get("allIframeCount", 0) > 0:
            saw_iframe = True
        if st.get("token"):
            _log(logger, f"已获得 Turnstile token（长度 {len(st['token'])}）")
            return True

        waited = time.time() - start
        if cf_tracker and cf_tracker.should_early_abort(saw_iframe, waited):
            _log(logger, f"❌ 提前结束等待（已等 {int(waited)}s）：检测到 Cloudflare 挑战域名网络/DNS 失败 "
                         f"（{cf_tracker.last_fail_reason}），iframe 未渲染。"
                         f"请检查容器能否解析 challenges.cloudflare.com，或在插件配置代理（socks5 会自动走远程 DNS）")
            return False

        if (not blank_rescue_attempted) and st.get("blankIframeCount", 0) > 0 and st.get("containerCount", 0) > 0 and st.get("hasApi") and waited > 35:
            blank_rescue_attempted = True
            await _rescue_blank_turnstile(page, logger)
            await page.wait_for_timeout(2500)
            st = await _get_turnstile_state(page)
            if st.get("token"):
                _log(logger, f"空 iframe 修复后已获得 Turnstile token（长度 {len(st['token'])}）")
                return True

        if not reloaded and st.get("required") and st.get("iframeCount", 0) == 0 and st.get("allIframeCount", 0) == 0 and waited > 18:
            reloaded = True
            _log(logger, "iframe 未渲染，刷新登录页重试一次...")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                _log(logger, f"刷新登录页失败: {e}")
            await page.wait_for_timeout(3000)
            await _render_turnstile(page, logger)
            await page.wait_for_timeout(2000)
            continue
        if time.time() - last_log > 15:
            _log(logger, f"token 仍为空，已等待 {int(waited)}s/{timeout_s}s"
                          f"（container={st.get('containerCount', 0)} iframe={st.get('iframeCount', 0)} "
                          f"all_iframe={st.get('allIframeCount', 0)} ready={st.get('readyState', '')} "
                          f"api={st.get('hasApi')} blank_iframe={st.get('blankIframeCount', 0)} cf_net_fail={cf_tracker.hard_fails if cf_tracker else 0}）")
            last_log = time.time()
        await page.wait_for_timeout(1000)

    if not saw_iframe:
        _log(logger, "❌ Turnstile token 超时仍为空 —— iframe 全程未渲染出来，大概率是当前网络访问不了 "
                      "challenges.cloudflare.com（DNS/出站被限制），不是单纯的 IP 信誉问题；"
                      "请检查容器/宿主机能否访问该域名，或在插件配置里填一个能正常访问 Cloudflare 的代理服务器")
    else:
        _log(logger, "❌ Turnstile token 超时仍为空 —— iframe 已出现但没有拿到 token。"
                      "在飞牛 NAS/家庭网络场景下，更常见原因是 MoviePilot 容器内 Chromium 与宿主机 Chrome 的 WebGL/SwiftShader/沙箱/共享内存/DNS 差异，"
                      "不应直接判定为机房 IP；若宿主机 Chrome 可登录，请优先检查容器 DNS、IPv6、网络模式与 Chromium 依赖")
    return False


# ============================================================
# ALTCHA（续期弹框）
# ============================================================
async def _get_altcha_status(page) -> dict:
    try:
        return await page.evaluate(
            """() => {
                const norm = v => v == null ? '' : String(v).trim();
                const w = document.querySelector('altcha-widget');
                const inputs = Array.from(document.querySelectorAll('input[name="altcha"], textarea[name="altcha"], input[name*="altcha" i], textarea[name*="altcha" i]'));
                const filled = inputs.find(i => norm(i.value).length > 0);
                const sr = w ? w.shadowRoot : null;
                const cb = sr ? sr.querySelector('input[type="checkbox"], [role="checkbox"]') : null;
                const state = norm(w ? (w.state || w.getAttribute('state')) : '');
                const valLen = Math.max(norm(w ? w.value : '').length, norm(w ? w.getAttribute('value') : '').length);
                const hiddenLen = norm(filled ? filled.value : '').length;
                const checked = cb && typeof cb.checked === 'boolean' ? cb.checked : null;
                const aria = norm(cb ? cb.getAttribute('aria-checked') : '');
                const busy = norm(w ? w.getAttribute('aria-busy') : '');
                const solved = state === 'verified' || valLen > 0 || hiddenLen > 0;
                const verifying = !solved && (['verifying','processing','working'].includes(state) || checked === true || aria === 'true' || busy === 'true');
                return { exists: !!w || inputs.length > 0, solved, verifying, state: state || 'unknown', hasShadow: !!sr };
            }"""
        )
    except Exception:
        return {"exists": False, "solved": False, "verifying": False, "state": "error", "hasShadow": False}


async def _cdp_click(page, x: float, y: float, logger):
    client = await page.context.new_cdp_session(page)
    try:
        await client.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        await asyncio.sleep(0.05 + 0.1 * (time.time() % 1))
        await client.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        return True
    except Exception as e:
        _log(logger, f"CDP 点击失败: {e}")
        return False
    finally:
        try:
            await client.detach()
        except Exception:
            pass


async def _click_altcha(page, logger) -> bool:
    try:
        widget = page.locator("altcha-widget").first
        if await widget.count() == 0:
            return False
        await page.wait_for_timeout(500)
        try:
            await widget.scroll_into_view_if_needed()
        except Exception:
            pass
        box = await page.evaluate(
            """() => {
                const w = document.querySelector('altcha-widget');
                if (!w) return null;
                const pick = root => root ? root.querySelector('input[type="checkbox"], [role="checkbox"], label, button') : null;
                let t = w.shadowRoot ? pick(w.shadowRoot) : null;
                if (!t) t = pick(w);
                const el = t || w;
                const r = el.getBoundingClientRect();
                return { x: r.left, y: r.top, w: r.width, h: r.height, exact: !!t };
            }"""
        )
        if not box or box["w"] <= 0 or box["h"] <= 0:
            return False
        if box["exact"]:
            cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
        else:
            cx = box["x"] + min(25, max(12, box["w"] * 0.15))
            cy = box["y"] + box["h"] / 2
        await _cdp_click(page, cx, cy, logger)
        await page.evaluate(
            """() => {
                const w = document.querySelector('altcha-widget');
                if (w && w.shadowRoot) {
                    const cb = w.shadowRoot.querySelector('input[type="checkbox"]');
                    if (cb && !cb.checked) cb.click();
                }
            }"""
        )
        return True
    except Exception as e:
        _log(logger, f"点击 ALTCHA 出错: {e}")
        return False


async def solve_altcha(page, logger, max_attempts: int = 15, wait_after_click_ms: int = 8000) -> bool:
    _log(logger, "检测 ALTCHA 验证码...")
    saw = False
    started = time.time()
    budget = max(wait_after_click_ms * max_attempts, wait_after_click_ms) / 1000.0
    clicks = 0
    while time.time() - started < budget:
        st = await _get_altcha_status(page)
        if st["exists"]:
            saw = True
        if st["solved"]:
            _log(logger, "✅ ALTCHA 已验证")
            return True
        if not st["exists"] or st["verifying"]:
            await page.wait_for_timeout(1000)
            continue
        if clicks >= max_attempts:
            await page.wait_for_timeout(1000)
            continue
        if not await _click_altcha(page, logger):
            await page.wait_for_timeout(1000)
            continue
        clicks += 1
        _log(logger, f"已点击 ALTCHA，等待 PoW 计算 ({clicks}/{max_attempts})...")
        click_start = time.time()
        observed = False
        while (time.time() - click_start) * 1000 < wait_after_click_ms:
            await page.wait_for_timeout(1000)
            fs = await _get_altcha_status(page)
            if fs["exists"]:
                saw = True
            if fs["solved"]:
                _log(logger, "✅ ALTCHA 验证通过")
                return True
            if fs["verifying"]:
                observed = True
                continue
            if not observed and (time.time() - click_start) >= 2.5:
                break
    if not saw:
        _log(logger, "弹框中无 ALTCHA 组件，视为无需验证")
        return True
    _log(logger, "❌ ALTCHA 未能在预算内通过")
    return False


# ============================================================
# 单账号：登录 + 续期
# ============================================================
async def _safe_shot(page, path: str):
    try:
        await page.screenshot(path=path, full_page=True)
        return path
    except Exception:
        return None


def _turnstile_fail_detail(cf_tracker: Optional[CfNetworkTracker]) -> str:
    if cf_tracker and cf_tracker.hard_fails > 0:
        return ("Cloudflare 挑战服务网络/DNS 不可达"
                f"（{cf_tracker.last_fail_reason or 'unknown'}）；"
                "请修复容器 DNS/出站，或在插件配置可访问 Cloudflare 的代理")
    return ("Cloudflare Turnstile 未通过"
            "（iframe 已出现但 token 为空；家庭 NAS 场景优先检查容器 DNS/IPv6/Chromium 出站，必要时再配置代理）")


async def process_account(context, user: Dict[str, str], login_url: str, shot_dir: Path,
                          logger, turnstile_wait: int, renew_attempts: int) -> Dict:
    username = user["username"]
    safe = re.sub(r"[^a-z0-9]", "_", username, flags=re.I)
    result = {"name": username, "success": False, "detail": "", "screenshot": "",
              "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    page = await context.new_page()
    page.set_default_timeout(60000)
    try:
        await page.set_viewport_size(VIEWPORT)
    except Exception:
        pass

    cf_tracker = CfNetworkTracker()

    def _on_request_failed(req):
        cf_tracker.on_request_failed(req, logger)

    page.on("requestfailed", _on_request_failed)

    try:
        # 1) 先登出，隔离上个账号会话
        if "dashboard" in page.url:
            await page.goto(LOGOUT_URL)
            await page.wait_for_timeout(2000)
        await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        if "dashboard" in page.url and "login" not in page.url:
            await page.goto(LOGOUT_URL)
            await page.wait_for_timeout(2000)
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
        await page.wait_for_timeout(2000)

        # 2) 登录：先填表再等待 Cloudflare Turnstile。
        # 有些 Turnstile 配置会在用户输入/表单交互后才真正渲染 iframe 或签发 token。
        await _prefill_login_fields(page, user, logger)
        btn = page.locator('#submit, button[type="submit"], button:has-text("Login")').first
        ok = await wait_turnstile_token(page, logger, turnstile_wait, cf_tracker)
        if not ok:
            result["detail"] = _turnstile_fail_detail(cf_tracker)
            result["screenshot"] = await _safe_shot(page, str(shot_dir / f"{safe}_turnstile_fail.png")) or ""
            return result

        _log(logger, "确认账号密码字段...")
        email = page.locator('#email, input[name="email"], input[type="email"]').first
        pwd = page.locator('#password, input[name="password"], input[type="password"]').first

        # 等 token 期间页面可能刷新/重新渲染，提交前短复检并补填。
        await _prefill_login_fields(page, user, logger)
        ok = await wait_turnstile_token(page, logger, min(20, turnstile_wait), cf_tracker)
        if not ok:
            result["detail"] = _turnstile_fail_detail(cf_tracker)
            result["screenshot"] = await _safe_shot(page, str(shot_dir / f"{safe}_turnstile_fail.png")) or ""
            return result

        # 令牌等待期间可能刷新过页面，补填
        await email.fill("")
        await email.fill(username)
        await pwd.fill("")
        await pwd.fill(user["password"])
        await btn.wait_for(state="visible", timeout=10000)
        await page.wait_for_timeout(800)

        _log(logger, "点击登录...")
        try:
            async with page.expect_navigation(timeout=30000):
                await btn.click(timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)

        if "/auth/login" in page.url:
            result["detail"] = "登录后仍在登录页（账号密码错误或验证未提交）"
            result["screenshot"] = await _safe_shot(page, str(shot_dir / f"{safe}_login_fail.png")) or ""
            return result

        # 3) 找 See 进入服务器详情
        _log(logger, '寻找 "See" 链接...')
        try:
            see = page.get_by_role("link", name="See").first
            await see.wait_for(timeout=15000)
            await page.wait_for_timeout(1000)
            await see.click()
        except Exception:
            result["detail"] = "未找到 See 链接（登录可能未成功）"
            result["screenshot"] = await _safe_shot(page, str(shot_dir / f"{safe}_no_see.png")) or ""
            return result

        # 4) Renew 循环
        for attempt in range(1, renew_attempts + 1):
            if "login" in page.url:
                result["detail"] = "被重定向到登录页"
                break
            _log(logger, f"[尝试 {attempt}/{renew_attempts}] 寻找 Renew 按钮...")
            renew_btn = page.get_by_role("button", name="Renew", exact=True).first
            try:
                await renew_btn.wait_for(state="visible", timeout=5000)
            except Exception:
                pass
            if not await renew_btn.is_visible():
                result["detail"] = "未找到 Renew 按钮"
                break
            await renew_btn.click()
            _log(logger, "Renew 已点击，等待弹框...")
            modal = page.locator('.modal-content, [role="dialog"]').filter(has_text="Renew").first
            try:
                await modal.wait_for(state="visible", timeout=5000)
            except Exception:
                _log(logger, "弹框未出现，重试")
                continue
            confirm = modal.get_by_role("button", name="Renew", exact=True)
            if not await confirm.is_visible():
                await page.reload()
                await page.wait_for_timeout(3000)
                continue

            await _safe_shot(page, str(shot_dir / f"{safe}_altcha_{attempt}.png"))
            altcha_ok = await solve_altcha(page, logger, 15, 8000)
            if not altcha_ok:
                result["detail"] = "ALTCHA 未通过"
                await page.reload()
                await page.wait_for_timeout(3000)
                continue

            _log(logger, "点击弹框内 Renew 确认...")
            await confirm.click()

            captcha_err = False
            not_yet = False
            date_str = ""
            t0 = time.time()
            while time.time() - t0 < 3:
                try:
                    if await page.get_by_text("Please complete the captcha to continue").is_visible():
                        captcha_err = True
                        break
                    not_time_loc = page.get_by_text("You can't renew your server yet")
                    if await not_time_loc.is_visible():
                        txt = await not_time_loc.inner_text()
                        m = re.search(r"as of\s+(.*?)\s+\(", txt or "")
                        date_str = m.group(1) if m else "未知"
                        not_yet = True
                        break
                except Exception:
                    pass
                await page.wait_for_timeout(200)

            if not_yet:
                result["success"] = True
                result["detail"] = f"暂无需续期，下次可续期：{date_str}"
                try:
                    close = modal.get_by_label("Close")
                    if await close.is_visible():
                        await close.click()
                        await page.wait_for_timeout(500)
                except Exception:
                    pass
                result["screenshot"] = await _safe_shot(page, str(shot_dir / f"{safe}_skip.png")) or ""
                break

            if captcha_err:
                result["detail"] = "验证码未通过"
                await page.reload()
                await page.wait_for_timeout(3000)
                continue

            await page.wait_for_timeout(2000)
            if not await modal.is_visible():
                result["success"] = True
                result["detail"] = "续期成功"
                result["screenshot"] = await _safe_shot(page, str(shot_dir / f"{safe}_success.png")) or ""
                break
            else:
                await page.reload()
                await page.wait_for_timeout(3000)
                continue

        if not result["success"] and not result["detail"]:
            result["detail"] = f"续期失败（已重试 {renew_attempts} 次）"
        if not result["screenshot"]:
            result["screenshot"] = await _safe_shot(page, str(shot_dir / f"{safe}.png")) or ""

    except Exception as e:
        result["detail"] = f"异常：{e}"
        result["screenshot"] = await _safe_shot(page, str(shot_dir / f"{safe}_exception.png")) or ""
    finally:
        try:
            await page.close()
        except Exception:
            pass
    return result


# 反自动化检测脚本：Playwright 启动的 chromium 默认带 navigator.webdriver=true 等自动化痕迹，
# Cloudflare Turnstile 一旦识别到就拒绝发令牌。
STEALTH_SCRIPT = r"""
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
    if (!window.chrome) { window.chrome = { runtime: {} }; }
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
      window.navigator.permissions.query = (params) => (
        params && params.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : origQuery(params)
      );
    }
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris OpenGL Engine';
      return getParam.call(this, p);
    };
  } catch (e) {}
})();
"""

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


async def run_all(accounts: List[Dict[str, str]], login_url: str, shot_dir: Path,
                  logger=None, chrome_path: str = "", turnstile_wait: int = 120,
                  renew_attempts: int = 3, headless: bool = False,
                  proxy_server: str = "", proxy_mode: str = "auto",
                  user_agent: str = "") -> List[Dict]:
    from playwright.async_api import async_playwright

    _log(logger, f"======== Katabump 引擎 v{ENGINE_VERSION} 启动 ========")

    # 决定代理策略；浏览器侧只用 launch proxy，避免环境变量与 launch 双重代理
    proxy_dict, strategy, need_dns_fix = resolve_browser_proxy(proxy_server, proxy_mode, logger)
    _log(logger, f"代理策略: {strategy} | DNS修复: {'开' if need_dns_fix else '关'}")

    # 清掉环境代理，避免 Playwright 子进程再读一层与 launch 冲突
    saved_proxy_env = {}
    for k in PROXY_ENV_KEYS:
        if k in os.environ:
            saved_proxy_env[k] = os.environ.pop(k)
    if saved_proxy_env:
        _log(logger, f"已临时清除环境代理变量（改由 Playwright proxy 注入）: {list(saved_proxy_env.keys())}")

    exe = chrome_path.strip() or ensure_chromium(logger)
    ua = user_agent.strip() or DEFAULT_UA
    results = []
    # 注意：不要加 --disable-background-networking，会干扰 DoH / CF 挑战资源加载
    launch_args = [
        "--no-sandbox", "--disable-setuid-sandbox",
        # 不使用 --disable-gpu：Turnstile 在部分 headless Chromium 中需要可用的 WebGL/SwiftShader，
        # 否则容易出现 /turnstile/.../crashed_retry + net::ERR_ABORTED。
        "--disable-dev-shm-usage", f"--window-size={VIEWPORT['width']},{VIEWPORT['height']}",
        "--mute-audio", "--no-first-run", "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--lang=zh-CN",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-ipv6",
        "--enable-webgl",
        "--enable-accelerated-2d-canvas",
    ]
    launch_args.extend(nas_chromium_runtime_args())

    # 直连时：主域通但子域不通很常见，默认只要 need_dns_fix 或无代理就注入 CF 映射
    # （有代理且为 socks5h/http 时 DNS 多在代理侧完成，可不映射）
    apply_dns_fix = need_dns_fix or (proxy_dict is None)
    if apply_dns_fix:
        try:
            wildcard_map = (os.environ.get("KATABUMP_CF_WILDCARD_MAP") or "").strip().lower() in ("1", "true", "yes", "on")
            rules = build_cf_host_resolver_rules(logger, wildcard=wildcard_map)
            if rules:
                launch_args.append(f"--host-resolver-rules={rules}")
                if wildcard_map:
                    _log(logger, "已注入 host-resolver-rules（含 *.challenges.cloudflare.com 强制映射）")
                else:
                    _log(logger, "已注入 host-resolver-rules（仅稳定 CF 主机；随机挑战子域走 DoH）")
            else:
                _log(logger, "CF host-resolver-rules 未生成（系统+公共 DNS 均失败，将仅依赖 DoH）")
        except Exception as e:
            _log(logger, f"生成 host-resolver-rules 失败: {e}")
        launch_args.extend(chromium_doh_args())
        _log(logger, "已启用 Chrome-like DNS-over-HTTPS automatic（NAS 兼容模式）")
        _log(logger, "NAS 兼容模式 v1.3.7：禁用 IPv6，随机 CF 挑战子域不做通配 IP 固定，并启用 SwiftShader/WebGL 兼容参数")
    else:
        _log(logger, "跳过 DNS 修复（使用代理，DNS 交给代理侧）")

    try:
        async with async_playwright() as p:
            launch_kwargs = {
                "headless": headless,
                "args": launch_args,
                "ignore_default_args": ["--enable-automation"],
            }
            if proxy_dict:
                launch_kwargs["proxy"] = proxy_dict
                _log(logger, f"浏览器代理: {proxy_dict.get('server')}"
                             f"{' (带认证)' if proxy_dict.get('username') else ''}")

            browser = None
            if exe:
                try:
                    browser = await p.chromium.launch(executable_path=exe, **launch_kwargs)
                    _log(logger, f"使用浏览器: {exe}")
                except Exception as e:
                    _log(logger, f"指定浏览器启动失败({e})，尝试其它方式")
            if browser is None:
                try:
                    browser = await p.chromium.launch(channel="chrome", **launch_kwargs)
                    _log(logger, "使用系统 Chrome 渠道")
                except Exception:
                    browser = await p.chromium.launch(**launch_kwargs)
                    _log(logger, "使用 Playwright 内置 chromium")

            context = await browser.new_context(
                viewport=VIEWPORT,
                user_agent=ua,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                color_scheme="light",
                has_touch=False,
                java_script_enabled=True,
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            await context.add_init_script(STEALTH_SCRIPT)
            try:
                for i, user in enumerate(accounts):
                    _log(logger, f"=== 处理账号 {i+1}/{len(accounts)}: {user['username']} ===")
                    res = await process_account(context, user, login_url or LOGIN_URL_DEFAULT,
                                                shot_dir, logger, turnstile_wait, renew_attempts)
                    results.append(res)
            finally:
                try:
                    await context.close()
                    await browser.close()
                except Exception:
                    pass
    finally:
        for k, v in saved_proxy_env.items():
            os.environ[k] = v
    return results
