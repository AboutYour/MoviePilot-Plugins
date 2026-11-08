"""栖影 HDHaven (hdhaven.com) 每日自动签到插件。"""

import json
import random
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

try:
    import pytz
except ImportError:
    pytz = None

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase

try:
    from app.schemas.types import NotificationType
except ImportError:  # MoviePilot compatibility
    from app.schemas import NotificationType

try:
    import cloudscraper

    HAS_CLOUDSCRAPER = True
except Exception:
    HAS_CLOUDSCRAPER = False

try:
    from curl_cffi import requests as curl_requests

    HAS_CURL_CFFI = True
except Exception:
    HAS_CURL_CFFI = False


class HDHavenCheckIn(_PluginBase):
    plugin_name = "栖影 HDHaven 每日签到"
    plugin_desc = "支持栖影 HDHaven (hdhaven.com) 每日自动签到，支持多账号、API直连与浏览器双模式、积分与连续签到统计、历史记录与通知。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/movie.png"
    plugin_version = "1.0.1"
    plugin_author = "kbmgr"
    author_url = "https://github.com/AboutYour"
    plugin_config_prefix = "hdhavencheckin_"
    plugin_order = 4
    auth_level = 1

    BASE_URL = "https://hdhaven.com"
    CHECKIN_PAGE_URL = "https://hdhaven.com/me#account-points"
    AUTH_ME_URL = "https://hdhaven.com/api/auth/me"
    CHECKIN_API_URL = "https://hdhaven.com/api/account/checkin"
    CHECKIN_GAMBLE_API_URL = "https://hdhaven.com/api/account/checkin/gamble"

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    )

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "20 8 * * *"
    _cookies_text = ""
    _checkin_mode = "normal"  # normal: 立即签到; gamble: 赌狗签到
    _use_browser = False  # API 直连急速稳定，无 Playwright 自动走 API
    _use_proxy = False  # 网络代理开关
    _proxy_server = ""  # 固定代理服务器（如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080）
    _verify_ssl = False
    _max_retries = 3
    _min_delay = 3
    _max_delay = 8
    _history_days = 30
    _clear_history = False

    _scheduler: Optional[BackgroundScheduler] = None
    _running = False
    _run_lock = threading.Lock()
    _account_clients: Dict[str, Dict[str, Any]] = {}

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self._account_clients = {}
        self._enabled = False
        self._notify = True
        self._onlyonce = False
        self._cron = "20 8 * * *"
        self._cookies_text = ""
        self._checkin_mode = "normal"
        self._use_browser = False
        self._use_proxy = False
        self._proxy_server = ""
        self._verify_ssl = False
        self._max_retries = 3
        self._min_delay = 3
        self._max_delay = 8
        self._history_days = 30
        self._clear_history = False

        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._onlyonce = bool(config.get("onlyonce"))
            self._cron = str(config.get("cron") or self._cron).strip()
            self._cookies_text = str(
                config.get("cookies_text")
                or config.get("cookies")
                or config.get("cookie")
                or ""
            ).strip()
            self._checkin_mode = str(config.get("checkin_mode") or "normal").strip()
            self._use_browser = bool(config.get("use_browser", False))
            self._proxy_server = str(config.get("proxy_server") or "").strip()
            self._use_proxy = bool(config.get("use_proxy", False))
            if self._proxy_server and not self._use_proxy:
                self._use_proxy = True
            self._verify_ssl = bool(config.get("verify_ssl", False))
            self._max_retries = self._as_int(config.get("max_retries"), 3, 0, 10)
            self._min_delay = self._as_int(config.get("min_delay"), 3, 0, 300)
            self._max_delay = self._as_int(config.get("max_delay"), 8, 0, 300)
            self._history_days = self._as_int(config.get("history_days"), 30, 1, 365)
            self._clear_history = bool(config.get("clear_history"))

        if self._max_delay < self._min_delay:
            self._min_delay, self._max_delay = self._max_delay, self._min_delay

        if self._clear_history:
            self.clear_sign_history()
            self._clear_history = False
            self._update_config()

        accounts = self._parse_accounts()
        logger.info(f"栖影 HDHaven 签到 v{self.plugin_version}：已加载有效账号 {len(accounts)} 个")

        if self._onlyonce:
            self._onlyonce = False
            self._update_config()
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            tz_obj = pytz.timezone(settings.TZ) if pytz else (ZoneInfo(settings.TZ) if ZoneInfo else None)
            self._scheduler.add_job(
                func=self.sign,
                trigger="date",
                run_date=datetime.now(tz=tz_obj) + timedelta(seconds=3),
                name="栖影 HDHaven 每日签到(单次)",
            )
            self._scheduler.start()

    @staticmethod
    def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            return min(max(int(value), minimum), maximum)
        except (TypeError, ValueError):
            return default

    def _update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "cookies_text": self._cookies_text,
            "checkin_mode": self._checkin_mode,
            "use_browser": self._use_browser,
            "use_proxy": self._use_proxy,
            "proxy_server": self._proxy_server,
            "verify_ssl": self._verify_ssl,
            "max_retries": self._max_retries,
            "min_delay": self._min_delay,
            "max_delay": self._max_delay,
            "history_days": self._history_days,
            "clear_history": self._clear_history,
        })

    @staticmethod
    def _cookie_pairs(cookie_str: str) -> List[Tuple[str, str]]:
        pairs = []
        for part in cookie_str.split(";"):
            part = part.strip()
            if not part:
                continue
            name, sep, value = part.partition("=")
            if sep and name.strip():
                pairs.append((name.strip(), value.strip()))
        return pairs

    def _normalize_cookie(self, raw_cookie: str) -> str:
        cookie = raw_cookie.strip()
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()

        # 如果用户直接粘贴了 hdh_session 值，自动为其加上键名
        if "=" not in cookie and len(cookie) >= 32:
            cookie = f"hdh_session={cookie};"
        elif "hdh_session" not in cookie and ";" not in cookie and len(cookie) >= 30:
            cookie = f"hdh_session={cookie};"

        pairs = dict(self._cookie_pairs(cookie))
        if not pairs:
            return ""

        return "; ".join(f"{k}={v}" for k, v in pairs.items())

    def _parse_accounts(self) -> List[Dict[str, Any]]:
        text = str(self._cookies_text or "").strip()
        if not text:
            return []

        accounts: List[Dict[str, Any]] = []
        for idx, raw_line in enumerate(text.splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            remark = ""
            cookie = line

            if line.startswith("{") and line.endswith("}"):
                try:
                    obj = json.loads(line)
                    if obj.get("enabled", True) is False:
                        continue
                    cookie = str(obj.get("cookie") or "").strip()
                    remark = str(obj.get("remark") or obj.get("name") or "").strip()
                except Exception:
                    cookie = line
            elif "#" in line and "=" in line.split("#", 1)[1] and "=" not in line.split("#", 1)[0]:
                remark, cookie = [part.strip() for part in line.split("#", 1)]
            elif "|" in line and "=" in line.split("|", 1)[1] and "=" not in line.split("|", 1)[0]:
                remark, cookie = [part.strip() for part in line.split("|", 1)]

            normalized_cookie = self._normalize_cookie(cookie)
            if not normalized_cookie:
                logger.warning(f"栖影 HDHaven 签到：第 {idx} 行 Cookie 格式无效，已跳过")
                continue

            account_index = len(accounts) + 1
            if not remark:
                remark = f"HDHaven账号{account_index}"

            accounts.append({
                "key": f"hdhaven:{account_index}",
                "index": account_index,
                "remark": remark,
                "cookie": normalized_cookie,
                "base_url": self.BASE_URL,
            })
        return accounts

    def sign(self) -> List[Dict[str, Any]]:
        if not self._run_lock.acquire(blocking=False):
            logger.warning("栖影 HDHaven 签到：已有任务正在运行，跳过本次触发")
            return []
        self._running = True
        try:
            accounts = self._parse_accounts()
            if not accounts:
                logger.warning("栖影 HDHaven 签到：未配置有效 Cookie")
                return []
            logger.info(f"栖影 HDHaven 签到：开始处理 {len(accounts)} 个账号")
            results = []
            for index, account in enumerate(accounts):
                if index:
                    self._random_wait()
                results.append(self._sign_account_with_retries(account))
            self._save_results(results)
            self._notify_results(results)
            return results
        finally:
            self._running = False
            self._run_lock.release()

    def _sign_account_with_retries(self, account: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self._open_account_clients(account)
            result = None
            for attempt in range(self._max_retries + 1):
                if attempt:
                    delay = random.uniform(max(1, self._min_delay), max(2, self._max_delay))
                    logger.info(
                        f"栖影 HDHaven 签到：{account['remark']} 第 {attempt}/{self._max_retries} 次重试，"
                        f"等待 {delay:.1f} 秒"
                    )
                    time.sleep(delay)
                try:
                    result = self._sign_account(account)
                except Exception as exc:
                    logger.error(f"栖影 HDHaven 签到：{account['remark']} 请求异常：{exc}", exc_info=True)
                    result = self._result(account, False, f"请求异常：{exc}")
                if result.get("success"):
                    return result
                if result.get("retryable") is False:
                    logger.warning(f"栖影 HDHaven 签到：{account['remark']} 为不可重试错误，停止后续重试")
                    return result
            return result or self._result(account, False, "未知错误")
        except Exception as exc:
            logger.error(f"栖影 HDHaven 签到：{account['remark']} 创建会话失败：{exc}", exc_info=True)
            return self._result(account, False, f"创建会话失败：{exc}")
        finally:
            self._close_account_clients(account["key"])

    def _sign_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"栖影 HDHaven 签到：正在签到 {account['remark']}")

        # 1. 优先使用 Playwright 浏览器自动化处理 (如果用户显式开启)
        if self._use_browser:
            browser_result = self._sign_via_playwright(account)
            if browser_result is not None:
                return browser_result

        # 2. 备用/默认使用 API 接口签到
        return self._sign_via_api(account)

    def _browser_headers(self, account: Dict[str, Any]) -> Dict[str, str]:
        return {
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": self.BASE_URL,
            "Referer": self.CHECKIN_PAGE_URL,
            "sec-ch-ua": '"Chromium";v="136", "Not?A_Brand";v="99", "Google Chrome";v="136"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Content-Type": "application/json",
            "Cookie": account["cookie"],
        }

    def _sign_via_api(self, account: Dict[str, Any]) -> Dict[str, Any]:
        headers = self._browser_headers(account)

        # 第一步：查询当前用户身份及签到状态 (/api/auth/me)
        user_info = None
        try:
            auth_resp = self._request(
                account,
                "get",
                self.AUTH_ME_URL,
                headers=headers,
                timeout=15,
            )
            if auth_resp.status_code == 200:
                auth_data = auth_resp.json()
                if auth_data.get("success") and isinstance(auth_data.get("data"), dict):
                    user_info = auth_data["data"]
                    nickname = user_info.get("nickname") or user_info.get("id") or "用户"
                    points = user_info.get("points", 0)
                    level_name = (user_info.get("level") or {}).get("name") or "普通用户"
                    account["remark"] = f"{account['remark']}({nickname})"
                    logger.info(
                        f"栖影 HDHaven 签到：{account['remark']} 身份验证通过 "
                        f"(用户: {nickname}, 等级: {level_name}, 当前积分: {points})"
                    )

                    # 检查是否今日已签到
                    if user_info.get("checkin_today"):
                        logger.info(f"栖影 HDHaven 签到：{account['remark']} 今日已签到过")
                        return self._result(
                            account,
                            success=True,
                            message=f"今日已签到（当前积分: {points}）",
                            already_signed=True,
                            points=0,
                            balance=points,
                        )
            elif auth_resp.status_code in (401, 403):
                err_text = auth_resp.text
                logger.error(f"栖影 HDHaven 签到：{account['remark']} 身份验证失败({auth_resp.status_code})：{err_text}")
                return self._result(
                    account,
                    success=False,
                    message=f"Cookie 失效或认证失败({auth_resp.status_code})，请重新登录",
                    retryable=False,
                )
        except Exception as exc:
            logger.debug(f"栖影 HDHaven 签到：{account['remark']} 预查用户信息异常：{exc}")

        # 第二步：调用签到接口
        is_gamble = (self._checkin_mode == "gamble")
        api_url = self.CHECKIN_GAMBLE_API_URL if is_gamble else self.CHECKIN_API_URL
        mode_label = "赌狗签到" if is_gamble else "立即签到"

        logger.info(f"栖影 HDHaven 签到：{account['remark']} 正在执行【{mode_label}】...")

        try:
            resp = self._request(
                account,
                "post",
                api_url,
                headers=headers,
                data="{}",
                timeout=20,
            )
            logger.info(f"栖影 HDHaven 签到：{account['remark']} 接口响应状态码：{resp.status_code}")

            # 201 签到成功
            if resp.status_code in (200, 201):
                try:
                    res_json = resp.json()
                    res_data = res_json.get("data") or {}
                    earned = res_data.get("earned", 0)
                    total_points = res_data.get("points")
                    streak = res_data.get("checkin_streak", 1)
                    outcome = res_data.get("outcome")

                    if is_gamble:
                        msg = f"赌狗签到「{outcome}」：获得 {earned} 积分，连续签到 {streak} 天"
                    else:
                        exp_earned = res_data.get("experience_earned", 0)
                        msg = f"签到成功！获得 {earned} 积分、{exp_earned} 经验（连续签到 {streak} 天）"

                    logger.info(f"栖影 HDHaven 签到：{account['remark']} {msg}")
                    return self._result(
                        account,
                        success=True,
                        message=msg,
                        already_signed=False,
                        points=earned,
                        balance=total_points,
                    )
                except Exception as exc:
                    logger.warning(f"解析签到响应 JSON 失败：{exc}")
                    return self._result(account, success=True, message="签到成功")

            # 409 今日已签到
            elif resp.status_code == 409:
                try:
                    res_json = resp.json()
                    msg = res_json.get("message") or "今日已签到"
                except Exception:
                    msg = "今日已签到"
                logger.info(f"栖影 HDHaven 签到：{account['remark']} 提示：{msg}")
                return self._result(
                    account,
                    success=True,
                    message=msg,
                    already_signed=True,
                    points=0,
                    balance=(user_info.get("points") if user_info else None),
                )

            # 401 / 403 认证失效
            elif resp.status_code in (401, 403):
                err_msg = "Cookie 已失效或缺少浏览器标识"
                try:
                    res_json = resp.json()
                    err_msg = res_json.get("message") or err_msg
                except Exception:
                    pass
                logger.error(f"栖影 HDHaven 签到：{account['remark']} 认证错误：{err_msg}")
                return self._result(
                    account,
                    success=False,
                    message=f"认证失败：{err_msg}",
                    retryable=False,
                )

            else:
                logger.error(
                    f"栖影 HDHaven 签到：{account['remark']} 接口返回异常状态码 {resp.status_code}：{resp.text[:100]}"
                )
                return self._result(account, False, f"HTTP {resp.status_code}")

        except Exception as exc:
            logger.error(f"栖影 HDHaven 签到：{account['remark']} 签到请求失败：{exc}", exc_info=True)
            return self._result(account, False, f"请求异常：{exc}")

    def _sign_via_playwright(self, account: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        使用 Playwright 浏览器自动化：
        1. 访问 https://hdhaven.com/me#account-points
        2. 定位到 .points-checkin-actions 中的签到按钮
        3. 点击「立即签到」按钮
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            logger.info("栖影 HDHaven 签到：环境未安装 Playwright 或浏览器不可用，自动使用 API 模式签到")
            return None

        proxies = self._get_proxies()
        proxy_server = None
        if proxies:
            srv = proxies.get("http") or proxies.get("https")
            if isinstance(srv, str) and srv.strip():
                proxy_server = srv.strip()

        try:
            with sync_playwright() as p:
                launch_kwargs: Dict[str, Any] = {
                    "headless": True,
                    "args": [
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--ignore-certificate-errors",
                    ],
                }
                if proxy_server:
                    logger.info(f"栖影 HDHaven 签到：{account['remark']} Playwright 浏览器使用网络代理: {proxy_server}")
                    launch_kwargs["proxy"] = {"server": proxy_server}

                browser = p.chromium.launch(**launch_kwargs)
                try:
                    context = browser.new_context(
                        user_agent=self.USER_AGENT,
                        ignore_https_errors=True,
                    )

                    # 注入 Cookie
                    cookies = [
                        {
                            "name": name,
                            "value": value,
                            "domain": ".hdhaven.com",
                            "path": "/",
                        }
                        for name, value in self._cookie_pairs(account["cookie"])
                    ]
                    context.add_cookies(cookies)

                    page = context.new_page()

                    intercepted_payload: Dict[str, Any] = {}

                    def _on_response(resp):
                        try:
                            if "/api/account/checkin" in resp.url:
                                data = resp.json()
                                if isinstance(data, dict):
                                    intercepted_payload.update(data)
                        except Exception:
                            pass

                    page.on("response", _on_response)
                    logger.info(f"栖影 HDHaven 签到：{account['remark']} 正在打开网页...")
                    page.goto(self.CHECKIN_PAGE_URL, wait_until="networkidle", timeout=35000)
                    page.wait_for_timeout(2500)

                    # 定位签到按钮区域
                    btn = None
                    if self._checkin_mode == "gamble":
                        btn = page.locator('.points-checkin-actions button.checkin-secondary').first
                    else:
                        btn = page.locator('.points-checkin-actions button.btn:not(.checkin-secondary)').first
                        if btn.count() == 0:
                            btn = page.locator('.points-checkin-actions button:has-text("立即签到")').first

                    if btn.count() == 0:
                        btn = page.locator('button:has-text("今日已签到")').first

                    if btn.count() == 0:
                        logger.warning(f"栖影 HDHaven 签到：{account['remark']} 网页中未找到签到按钮，尝试回退 API 模式")
                        return None

                    btn_text = btn.inner_text() or ""
                    if "今日已签到" in btn_text:
                        return self._result(
                            account,
                            True,
                            "今日已签到",
                            already_signed=True,
                        )

                    logger.info(f"栖影 HDHaven 签到：{account['remark']} 找到签到按钮，正在点击...")
                    btn.click(timeout=10000)
                    page.wait_for_timeout(3000)

                    if intercepted_payload:
                        res_data = intercepted_payload.get("data") or {}
                        earned = res_data.get("earned", 0)
                        total_points = res_data.get("points")
                        streak = res_data.get("checkin_streak", 1)
                        msg = f"签到成功！获得 {earned} 积分（连续签到 {streak} 天）"
                        return self._result(
                            account,
                            success=True,
                            message=msg,
                            points=earned,
                            balance=total_points,
                        )

                    after_html = page.content()
                    if "今日已签到" in after_html or "签到成功" in after_html:
                        return self._result(account, True, "签到成功 (页面已更新)")

                    return self._result(account, True, "已点击立即签到")
                finally:
                    browser.close()
        except Exception as exc:
            logger.warning(f"栖影 HDHaven 签到：{account['remark']} 浏览器签到流程异常：{exc}，切换为 API 直连")
            return None

    def _open_account_clients(self, account: Dict[str, Any]):
        key = account["key"]
        proxies = self._get_proxies()
        cookie_str = account["cookie"]

        if proxies:
            srv = proxies.get("http") or proxies.get("https")
            logger.info(f"栖影 HDHaven 签到：{account['remark']} HTTP 会话使用网络代理: {srv}")

        # 1. Requests session
        session = requests.Session()
        session.headers.update(self._browser_headers(account))
        session.verify = self._verify_ssl
        if proxies:
            session.proxies.update(proxies)
        for name, value in self._cookie_pairs(cookie_str):
            session.cookies.set(name, value, domain="hdhaven.com")
            session.cookies.set(name, value, domain=".hdhaven.com")

        # 2. Cloudscraper session
        scraper = None
        if HAS_CLOUDSCRAPER:
            try:
                scraper = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows", "desktop": True},
                    sess=session,
                )
            except Exception:
                scraper = None

        # 3. curl_cffi session
        curl_session = None
        if HAS_CURL_CFFI:
            try:
                curl_session = curl_requests.Session(impersonate="chrome120", verify=self._verify_ssl)
                curl_session.headers.update(self._browser_headers(account))
                if proxies:
                    curl_session.proxies = proxies
                for name, value in self._cookie_pairs(cookie_str):
                    try:
                        curl_session.cookies.set(name, value, domain="hdhaven.com")
                        curl_session.cookies.set(name, value, domain=".hdhaven.com")
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning(f"栖影 HDHaven 签到：初始化 curl_cffi 失败：{exc}")
                curl_session = None

        self._account_clients[key] = {
            "session": session,
            "scraper": scraper,
            "curl_session": curl_session,
        }

    def _close_account_clients(self, key: str):
        clients = self._account_clients.pop(key, None)
        if not clients:
            return
        for name in ("session", "scraper", "curl_session"):
            client = clients.get(name)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _close_all_account_clients(self):
        for key in list(self._account_clients.keys()):
            self._close_account_clients(key)

    def _request(self, account: Dict[str, Any], method: str, url: str, **kwargs):
        clients = self._account_clients.get(account["key"]) or {}
        errors = []
        for name in ("session", "scraper", "curl_session"):
            client = clients.get(name)
            if client is None:
                continue
            try:
                return client.request(method=method, url=url, **kwargs)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                logger.warning(f"栖影 HDHaven 签到：{account['remark']} 使用 {name} 请求失败：{exc}，尝试下一个客户端")
        raise RuntimeError("；".join(errors) or "账号 HTTP 会话未初始化或请求均失败")

    def _result(
        self,
        account: Dict[str, Any],
        success: bool,
        message: str,
        already_signed: bool = False,
        points: Any = None,
        balance: Any = None,
        retryable: bool = True,
    ) -> Dict[str, Any]:
        status_label = "成功" if success else "失败"
        if already_signed:
            status_label = "已签到"
        return {
            "key": account["key"],
            "index": account["index"],
            "remark": account["remark"],
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "success": success,
            "already_signed": already_signed,
            "status": status_label,
            "message": str(message or "").strip(),
            "points": points if points is not None else "-",
            "balance": balance if balance is not None else "-",
            "retryable": retryable,
        }

    def _random_wait(self):
        delay = random.uniform(self._min_delay, self._max_delay)
        logger.info(f"栖影 HDHaven 签到：随机等待 {delay:.1f} 秒...")
        time.sleep(delay)

    def _get_proxies(self) -> Optional[Dict[str, str]]:
        # 1. 优先使用用户配置的固定网络代理
        proxy_server = (self._proxy_server or "").strip()
        if proxy_server:
            if "://" not in proxy_server:
                proxy_server = f"http://{proxy_server}"
            return {"http": proxy_server, "https": proxy_server}

        # 2. 如果未配置固定代理，但启用了代理开关，则回退使用 MoviePilot 系统全局代理
        if not self._use_proxy:
            return None
        proxy = getattr(settings, "PROXY", None)
        if not proxy:
            return None
        if isinstance(proxy, str):
            proxy = proxy.strip()
            if proxy:
                if "://" not in proxy:
                    proxy = f"http://{proxy}"
                return {"http": proxy, "https": proxy}
            return None
        if isinstance(proxy, dict):
            http_val = (
                proxy.get("http")
                or proxy.get("HTTP")
                or proxy.get("https")
                or proxy.get("HTTPS")
                or proxy.get("all")
                or proxy.get("ALL")
            )
            https_val = (
                proxy.get("https")
                or proxy.get("HTTPS")
                or proxy.get("http")
                or proxy.get("HTTP")
                or proxy.get("all")
                or proxy.get("ALL")
            )
            while isinstance(http_val, dict):
                http_val = http_val.get("http") or http_val.get("https") or next(iter(http_val.values()), None)
            while isinstance(https_val, dict):
                https_val = https_val.get("https") or https_val.get("http") or next(iter(https_val.values()), None)

            http_str = str(http_val or "").strip()
            https_str = str(https_val or "").strip()
            if http_str or https_str:
                final_http = http_str or https_str
                final_https = https_str or http_str
                if "://" not in final_http:
                    final_http = f"http://{final_http}"
                if "://" not in final_https:
                    final_https = f"http://{final_https}"
                return {"http": final_http, "https": final_https}
        return None

    def _save_results(self, results: List[Dict[str, Any]]):
        history = self.get_data("sign_history") or []
        cutoff = (datetime.now() - timedelta(days=self._history_days)).strftime("%Y-%m-%d %H:%M:%S")
        history = [item for item in history if item.get("date", "") >= cutoff]
        for res in results:
            history.insert(0, {
                "date": res["date"],
                "remark": res["remark"],
                "status": res["status"],
                "success": res["success"],
                "points": res["points"],
                "balance": res["balance"],
                "message": res["message"],
            })
        self.save_data("sign_history", history[:300])

    def clear_sign_history(self):
        self.save_data("sign_history", [])
        logger.info("栖影 HDHaven 签到：已清空历史记录")

    def _notify_results(self, results: List[Dict[str, Any]]):
        if not self._notify or not results:
            return
        success_count = sum(1 for r in results if r.get("success"))
        fail_count = len(results) - success_count
        title = f"栖影 HDHaven 签到：{success_count} 成功 / {fail_count} 失败"

        lines = []
        for r in results:
            line = f"• {r['remark']}：{r['status']}（{r['message']}）"
            if r.get("points") != "-" and r.get("points") != 0:
                line += f"，获得积分：{r['points']}"
            if r.get("balance") != "-":
                line += f"，当前总积分：{r['balance']}"
            lines.append(line)

        content = "\n".join(lines)
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=title,
                text=content,
            )
        except Exception as exc:
            logger.error(f"栖影 HDHaven 签到：发送通知失败：{exc}")

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [{
            "id": "HDHavenCheckIn",
            "name": "栖影 HDHaven 每日签到",
            "trigger": CronTrigger.from_crontab(self._cron, timezone=settings.TZ),
            "func": self.sign,
            "kwargs": {},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        example = (
            "# 方式1：直接粘贴完整 Cookie（支持换行分隔多账号）\n"
            "hdh_session=432fe35db8e8120e772a8f287695748c6e15d53a0b577495;\n\n"
            "# 方式2：备注#Cookie\n"
            "我的账号#hdh_session=432fe35db8e8120e772a8f287695748c6e15d53a0b577495;\n\n"
            "# 方式3：直接粘贴 Session Token\n"
            "432fe35db8e8120e772a8f287695748c6e15d53a0b577495"
        )
        return [{
            "component": "VForm",
            "content": [
                {
                    "component": "VRow",
                    "content": [
                        self._col_switch("enabled", "启用插件"),
                        self._col_switch("notify", "发送汇总通知"),
                        self._col_switch("onlyonce", "立即运行一次"),
                        self._col_switch("use_browser", "使用浏览器自动化(可选)"),
                        self._col_switch("use_proxy", "使用网络代理"),
                        self._col_switch("verify_ssl", "验证 SSL 证书"),
                        self._col_switch("clear_history", "清除历史记录"),
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 6},
                            "content": [{
                                "component": "VSelect",
                                "props": {
                                    "model": "checkin_mode",
                                    "label": "签到模式",
                                    "items": [
                                        {"title": "立即签到 (稳妥获取基础积分，推荐)", "value": "normal"},
                                        {"title": "赌狗签到 (高风险随机博弈)", "value": "gamble"},
                                    ],
                                },
                            }],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 6},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "proxy_server",
                                    "label": "固定代理服务器 (优先走此代理)",
                                    "placeholder": "例如: http://127.0.0.1:7890 或 socks5://127.0.0.1:1080 (留空使用系统代理)",
                                },
                            }],
                        },
                    ],
                },
                {
                    "component": "VRow",
                    "content": [{
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{
                            "component": "VTextarea",
                            "props": {
                                "model": "cookies_text",
                                "label": "Cookie 列表（多个账号通过回车换行分隔，支持「备注#Cookie」）",
                                "placeholder": example,
                                "rows": 8,
                                "auto-grow": True,
                            },
                        }],
                    }],
                },
                {
                    "component": "VRow",
                    "content": [
                        self._col_field("cron", "签到周期", component="VCronField"),
                        self._col_field("max_retries", "失败重试次数", field_type="number"),
                        self._col_field("min_delay", "最小随机延迟(秒)", field_type="number"),
                        self._col_field("max_delay", "最大随机延迟(秒)", field_type="number"),
                        self._col_field("history_days", "历史保留天数", field_type="number"),
                    ],
                },
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": (
                            "支持栖影 HDHaven (https://hdhaven.com/me#account-points) 每日自动签到。\n"
                            "多个账号请用回车换行分隔（每行一个完整 Cookie 或 hdh_session 值）。\n"
                            "网络代理：如需指定独立代理，请在「固定代理服务器」配置（支持 http:// 或 socks5://），开启后直接走该代理进行签到；留空且开启「使用网络代理」时则走系统全局代理。\n"
                            "提示：请确保 Cookie 包含「hdh_session」有效登录会话。"
                        ),
                    },
                },
            ],
        }], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "20 8 * * *",
            "cookies_text": "",
            "checkin_mode": "normal",
            "use_browser": False,
            "use_proxy": False,
            "proxy_server": "",
            "verify_ssl": False,
            "max_retries": 3,
            "min_delay": 3,
            "max_delay": 8,
            "history_days": 30,
            "clear_history": False,
        }

    @staticmethod
    def _col_switch(model: str, label: str) -> Dict[str, Any]:
        return {
            "component": "VCol",
            "props": {"cols": 12, "sm": 6, "md": 3},
            "content": [{"component": "VSwitch", "props": {"model": model, "label": label}}],
        }

    @staticmethod
    def _col_field(
        model: str,
        label: str,
        component: str = "VTextField",
        field_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        props = {"model": model, "label": label}
        if field_type:
            props["type"] = field_type
        return {
            "component": "VCol",
            "props": {"cols": 12, "sm": 6, "md": 4},
            "content": [{"component": component, "props": props}],
        }

    def get_page(self) -> List[dict]:
        history = sorted(
            self.get_data("sign_history") or [],
            key=lambda item: item.get("date", ""),
            reverse=True,
        )
        rows = []
        for item in history:
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": item.get("date", "-")},
                    {"component": "td", "text": item.get("remark", "-")},
                    {
                        "component": "td",
                        "content": [{
                            "component": "VChip",
                            "props": {
                                "size": "small",
                                "variant": "outlined",
                                "color": "success" if item.get("success") else "error",
                            },
                            "text": item.get("status", "未知"),
                        }],
                    },
                    {"component": "td", "text": str(item.get("points", "-"))},
                    {"component": "td", "text": str(item.get("balance", "-"))},
                    {"component": "td", "text": item.get("message", "-")},
                ],
            })
        return [{
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": [
                {"component": "VCardTitle", "text": "栖影 HDHaven 签到历史"},
                {
                    "component": "VCardText",
                    "content": [{
                        "component": "VTable",
                        "props": {"hover": True, "density": "compact"},
                        "content": [
                            {
                                "component": "thead",
                                "content": [{
                                    "component": "tr",
                                    "content": [
                                        {"component": "th", "text": "时间"},
                                        {"component": "th", "text": "账号"},
                                        {"component": "th", "text": "状态"},
                                        {"component": "th", "text": "获得积分"},
                                        {"component": "th", "text": "当前总积分"},
                                        {"component": "th", "text": "消息"},
                                    ],
                                }],
                            },
                            {"component": "tbody", "content": rows},
                        ],
                    }],
                },
            ],
        }]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown(wait=False)
                self._scheduler = None
        except Exception as exc:
            logger.error(f"栖影 HDHaven 签到：停止调度失败：{exc}")
        finally:
            self._close_all_account_clients()

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []


hdhavencheckin = HDHavenCheckIn
