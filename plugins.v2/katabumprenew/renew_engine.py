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
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional


LOGIN_URL_DEFAULT = "https://dashboard.katabump.com/auth/login"
LOGOUT_URL = "https://dashboard.katabump.com/auth/logout"
VIEWPORT = {"width": 1280, "height": 720}


def _log(logger, msg: str):
    if logger:
        logger.info(f"[Katabump] {msg}")
    else:
        print(f"[Katabump] {msg}")


# ============================================================
# chromium 自动安装：MoviePilot 容器有 playwright 库和系统依赖，
# 但不一定有 chromium 二进制。首次运行自动补装一次。
# ============================================================
def ensure_chromium(logger=None) -> Optional[str]:
    """确保有可用的 chromium，返回可执行路径（None 表示用 playwright 默认）。"""
    # 1) 已装的 playwright chromium
    candidates = []
    ms_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    search_roots = [ms_root] if ms_root else []
    search_roots += [
        os.path.expanduser("~/.cache/ms-playwright"),
        "/ms-playwright",
        "/root/.cache/ms-playwright",
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

    # 2) 没有则用 playwright 安装
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
                const containers = document.querySelectorAll('.cf-turnstile, [data-sitekey]');
                const iframes = Array.from(document.querySelectorAll('iframe')).filter(f => /turnstile|challenges\\.cloudflare/i.test(f.src||''));
                return {
                    required: els.length > 0 || containers.length > 0 || iframes.length > 0,
                    token, inputCount: els.length, iframeCount: iframes.length,
                    containerCount: containers.length,
                    hasApi: typeof window.turnstile !== 'undefined'
                };
            }"""
        )
    except Exception:
        return {"required": False, "token": "", "inputCount": 0, "iframeCount": 0, "containerCount": 0, "hasApi": False}


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


async def wait_turnstile_token(page, logger, timeout_s: int = 120) -> bool:
    _log(logger, "检查 Cloudflare Turnstile token...")
    st = await _get_turnstile_state(page)
    if st.get("token"):
        _log(logger, f"已有 Turnstile token（长度 {len(st['token'])}）")
        return True
    if not st.get("required"):
        _log(logger, "本次登录页未触发 Turnstile")
        return True

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
    saw_iframe = st.get("iframeCount", 0) > 0
    while time.time() - start < timeout_s:
        st = await _get_turnstile_state(page)
        if st.get("iframeCount", 0) > 0:
            saw_iframe = True
        if st.get("token"):
            _log(logger, f"已获得 Turnstile token（长度 {len(st['token'])}）")
            return True
        if not reloaded and st.get("required") and st.get("iframeCount", 0) == 0 and time.time() - start > 10:
            reloaded = True
            _log(logger, "iframe 未渲染，刷新登录页重试一次...")
            await page.reload(wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            await _render_turnstile(page, logger)
            await page.wait_for_timeout(2000)
            continue
        if time.time() - last_log > 15:
            waited = int(time.time() - start)
            _log(logger, f"token 仍为空，已等待 {waited}s/{timeout_s}s"
                          f"（container={st.get('containerCount', 0)} iframe={st.get('iframeCount', 0)} "
                          f"api={st.get('hasApi')}，需住宅 IP 完成官方验证）")
            last_log = time.time()
        await page.wait_for_timeout(1000)

    if not saw_iframe:
        _log(logger, "❌ Turnstile token 超时仍为空 —— iframe 全程未渲染出来，大概率是当前网络访问不了 "
                      "challenges.cloudflare.com（DNS/出站被限制），不是单纯的 IP 信誉问题；"
                      "请检查容器/宿主机能否访问该域名，或在插件配置里填一个能正常访问 Cloudflare 的代理服务器")
    else:
        _log(logger, "❌ Turnstile token 超时仍为空 —— iframe 已渲染但验证服务器拒发令牌，"
                      "通常是出口 IP 被 Cloudflare 判定为机房/云 IP；请改用住宅网络运行，"
                      "或在插件配置的代理服务器里填一个住宅代理")
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
        # shadow DOM 内兜底再点一次
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

    # 直接监听网络失败：如果连 Cloudflare 挑战/资源域名都请求失败，
    # 说明是网络可达性问题（DNS/出站被限制），而不是 IP 信誉判定，
    # 这类日志比单纯"token 仍为空"更能定位根因。
    def _on_request_failed(req):
        try:
            url = req.url
            if re.search(r"cloudflare|turnstile|challenges\.cloudflare\.com", url, re.I):
                _log(logger, f"⚠️ 网络请求失败: {url}（原因: {req.failure}）—— 若持续出现，说明当前网络访问不了 Cloudflare 挑战服务，需检查出站网络或配置代理")
        except Exception:
            pass
    page.on("requestfailed", _on_request_failed)

    try:
        # 1) 先登出，隔离上个账号会话
        if "dashboard" in page.url:
            await page.goto(LOGOUT_URL)
            await page.wait_for_timeout(2000)
        await page.goto(login_url)
        await page.wait_for_timeout(2000)
        if "dashboard" in page.url and "login" not in page.url:
            await page.goto(LOGOUT_URL)
            await page.wait_for_timeout(2000)
            await page.goto(login_url)
            await page.wait_for_timeout(2000)
        await page.wait_for_timeout(2000)

        # 2) 登录：等待 Cloudflare Turnstile 首次令牌就绪（结果必须判断，否则超时后
        #    仍会往下走，且下面还会再等一次满额超时，白白卡住 2 倍时长）
        ok = await wait_turnstile_token(page, logger, turnstile_wait)
        if not ok:
            result["detail"] = "Cloudflare Turnstile 未通过（当前出口 IP 无法通过官方验证，请改用住宅网络运行，或在插件配置中填写代理服务器）"
            result["screenshot"] = await _safe_shot(page, str(shot_dir / f"{safe}_turnstile_fail.png")) or ""
            return result

        _log(logger, "填写账号密码...")
        email = page.locator('#email, input[name="email"], input[type="email"]').first
        pwd = page.locator('#password, input[name="password"], input[type="password"]').first
        btn = page.locator('#submit, button[type="submit"], button:has-text("Login")').first
        await email.wait_for(state="visible", timeout=15000)
        await email.fill("")
        await email.fill(username)
        await pwd.fill("")
        await pwd.fill(user["password"])

        # 填表可能触发页面刷新/重新校验，做一次短复检（≤20s），不再重复整段超时等待
        ok = await wait_turnstile_token(page, logger, min(20, turnstile_wait))
        if not ok:
            result["detail"] = "Cloudflare Turnstile 未通过（当前出口 IP 无法通过官方验证，请改用住宅网络运行，或在插件配置中填写代理服务器）"
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

            # 检查“还没到续期时间” / 验证码错误
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
# Cloudflare Turnstile 一旦识别到就拒绝发令牌（这就是桌面版 WebView2 能过、裸 Playwright 过不了的根本原因）。
# 下面这段在每个文档创建前注入，抹掉最常见的自动化指纹，尽量贴近真实浏览器。
STEALTH_SCRIPT = r"""
(() => {
  try {
    // 1) navigator.webdriver -> undefined
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    // 2) 伪造 plugins / mimeTypes 非空
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    // 3) languages
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
    // 4) window.chrome 存在（无头/自动化下常缺失）
    if (!window.chrome) { window.chrome = { runtime: {} }; }
    // 5) permissions.query 对 notifications 返回 default（自动化下常报 denied）
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
      window.navigator.permissions.query = (params) => (
        params && params.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : origQuery(params)
      );
    }
    // 6) WebGL vendor/renderer 伪装成常见显卡
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
                  proxy_server: str = "", user_agent: str = "") -> List[Dict]:
    from playwright.async_api import async_playwright

    # 关键：除非用户明确配置了 proxy_server，否则清掉环境变量里的代理。
    # MoviePilot/Clash 常在环境中带 HTTP_PROXY，会把浏览器出口拐到机房 IP，
    # 导致 Turnstile 永远不发令牌（和桌面版直连住宅 IP 行为不一致）。
    saved_proxy_env = {}
    proxy_env_keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    if not proxy_server.strip():
        for k in proxy_env_keys:
            if k in os.environ:
                saved_proxy_env[k] = os.environ.pop(k)
        if saved_proxy_env:
            _log(logger, f"已临时清除环境代理，浏览器直连住宅出口: {list(saved_proxy_env.keys())}")
    else:
        _log(logger, f"使用用户配置的代理: {proxy_server.strip()}")

    exe = chrome_path.strip() or ensure_chromium(logger)
    ua = user_agent.strip() or DEFAULT_UA
    results = []
    # 关键：--disable-blink-features=AutomationControlled 关掉自动化标志；
    # 配合 ignore_default_args 去掉 --enable-automation，避免暴露为自动化会话。
    launch_args = [
        "--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", f"--window-size={VIEWPORT['width']},{VIEWPORT['height']}",
        "--disable-background-networking",
        "--mute-audio", "--no-first-run", "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--lang=zh-CN",
    ]
    try:
        async with async_playwright() as p:
            launch_kwargs = {
                "headless": headless,
                "args": launch_args,
                "ignore_default_args": ["--enable-automation"],
            }
            if proxy_server.strip():
                launch_kwargs["proxy"] = {"server": proxy_server.strip()}
            # 优先用真实 Chrome 渠道（指纹比 bundled chromium 更真实，更容易过 Turnstile）；
            # 指定了 executable_path 时用它，否则尝试 channel="chrome"，都失败再退回 bundled chromium。
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
        # 还原环境代理
        for k, v in saved_proxy_env.items():
            os.environ[k] = v
    return results
