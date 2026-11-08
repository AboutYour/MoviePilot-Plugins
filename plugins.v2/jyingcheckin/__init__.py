"""聚影 (jying.top) 每日自动签到插件。"""

import json
import random
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytz
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


class JYingCheckIn(_PluginBase):
    plugin_name = "聚影每日签到"
    plugin_desc = "支持聚影 (jying.top) 每日自动签到，支持多账号 Cookie 回车换行分隔配置、API 与浏览器双模式、签到历史与通知。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/movie.png"
    plugin_version = "1.0.0"
    plugin_author = "kbmgr"
    author_url = "https://github.com/AboutYour"
    plugin_config_prefix = "jyingcheckin_"
    plugin_order = 3
    auth_level = 1

    BASE_URL = "https://www.jying.top"
    CHECKIN_PAGE_URL = "https://www.jying.top/checkin"
    STATS_API_URL = "https://www.jying.top/api/app/checkin/stats/"
    CHECKIN_API_URL = "https://www.jying.top/api/app/checkin/do/"
    LOGIN_API_URL = "https://www.jying.top/api/app/login/"
    CHECKIN_BUTTON_SELECTOR = 'button[data-v-b974fb4e], button.n-button:has-text("立即签到")'

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    )

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "15 8 * * *"
    _cookies_text = ""
    _use_browser = False
    _use_proxy = True
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
        self._cron = "15 8 * * *"
        self._cookies_text = ""
        self._use_browser = False
        self._use_proxy = True
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
            self._use_browser = bool(config.get("use_browser", False))
            self._use_proxy = bool(config.get("use_proxy", True))
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
        logger.info(f"聚影签到 v{self.plugin_version}：已加载有效 Cookie 账号 {len(accounts)} 个")

        if self._onlyonce:
            self._onlyonce = False
            self._update_config()
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sign,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="聚影每日签到(单次)",
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
            "use_browser": self._use_browser,
            "use_proxy": self._use_proxy,
            "verify_ssl": self._verify_ssl,
            "max_retries": self._max_retries,
            "min_delay": self._min_delay,
            "max_delay": self._max_delay,
            "history_days": self._history_days,
            "clear_history": self._clear_history,
        })

    @staticmethod
    def _extract_cookie_val(cookie_str: str, key_name: str) -> str:
        for part in cookie_str.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name.strip().lower() == key_name.lower() and value.strip():
                return value.strip()
        return ""

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
            username = ""
            password = ""

            if line.startswith("{") and line.endswith("}"):
                try:
                    obj = json.loads(line)
                    if obj.get("enabled", True) is False:
                        continue
                    cookie = str(obj.get("cookie") or obj.get("token") or obj.get("app_user_token") or "").strip()
                    remark = str(obj.get("remark") or obj.get("name") or "").strip()
                    username = str(obj.get("username") or obj.get("user") or "").strip()
                    password = str(obj.get("password") or obj.get("pwd") or "").strip()
                except Exception:
                    cookie = line
            elif "#" in line and "=" in line.split("#", 1)[1] and "=" not in line.split("#", 1)[0]:
                remark, cookie = [part.strip() for part in line.split("#", 1)]
            elif "|" in line and "=" in line.split("|", 1)[1] and "=" not in line.split("|", 1)[0]:
                remark, cookie = [part.strip() for part in line.split("|", 1)]
            elif "#" in line and "=" not in line:
                parts = [part.strip() for part in line.split("#") if part.strip()]
                if len(parts) == 3:
                    remark, username, password = parts
                elif len(parts) == 2:
                    username, password = parts
            elif ":" in line and "=" not in line and ";" not in line:
                parts = [part.strip() for part in line.split(":", 1) if part.strip()]
                if len(parts) == 2:
                    username, password = parts

            if cookie.lower().startswith("cookie:"):
                cookie = cookie.split(":", 1)[1].strip()

            # 提取 username 与 password（如果写在键值对中，如 username=xxx; password=yyy;）
            if not username:
                username = self._extract_cookie_val(cookie, "username") or self._extract_cookie_val(cookie, "user")
            if not password:
                password = self._extract_cookie_val(cookie, "password") or self._extract_cookie_val(cookie, "pwd")

            # 提取 csrftoken 与 app_user_token
            csrftoken = self._extract_cookie_val(cookie, "csrftoken")
            app_user_token = (
                self._extract_cookie_val(cookie, "app_user_token")
                or self._extract_cookie_val(cookie, "token")
                or self._extract_cookie_val(cookie, "jwt")
                or self._extract_cookie_val(cookie, "access_token")
            )

            # 智能纠偏：如果 username 或 password 实际被误填成了 Token（以 eyJ 开头或长 Base64 字符串）
            if username and (username.startswith("eyJ") or len(username) >= 30):
                if not app_user_token:
                    app_user_token = username
                username = ""
            if password and (password.startswith("eyJ") or len(password) >= 30):
                if not app_user_token:
                    app_user_token = password
                password = ""

            # 如果用户直接粘贴了纯 Token 字符串（包含 eyJ 或长字符串）
            if not app_user_token:
                if "eyJ" in cookie:
                    for part in cookie.split(";"):
                        part = part.strip()
                        if "eyJ" in part:
                            app_user_token = part.split("=", 1)[-1].strip()
                            break
                elif "=" not in cookie and len(cookie) >= 20:
                    app_user_token = cookie.strip()

            if app_user_token and "app_user_token" not in cookie:
                cookie = f"app_user_token={app_user_token}; {cookie}".strip()

            pairs = self._cookie_pairs(cookie)
            if not pairs and not app_user_token and not (username and password):
                logger.warning(f"聚影签到：第 {idx} 行凭证格式无效，已跳过")
                continue

            account_index = len(accounts) + 1
            if not remark:
                if username:
                    remark = f"聚影账号{account_index}({username})"
                elif app_user_token:
                    # 避免把长 Token 打进 remark
                    remark = f"聚影账号{account_index}(Token)"
                else:
                    remark = f"聚影账号{account_index}"

            accounts.append({
                "key": f"jying:{account_index}",
                "index": account_index,
                "remark": remark,
                "username": username,
                "password": password,
                "csrftoken": csrftoken,
                "app_user_token": app_user_token,
                "cookie": cookie,
                "base_url": self.BASE_URL,
            })
        return accounts

    def sign(self) -> List[Dict[str, Any]]:
        if not self._run_lock.acquire(blocking=False):
            logger.warning("聚影签到：已有任务正在运行，跳过本次触发")
            return []
        self._running = True
        try:
            accounts = self._parse_accounts()
            if not accounts:
                logger.warning("聚影签到：未配置有效 Cookie")
                return []
            logger.info(f"聚影签到：开始处理 {len(accounts)} 个账号")
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
                        f"聚影签到：{account['remark']} 第 {attempt}/{self._max_retries} 次重试，"
                        f"等待 {delay:.1f} 秒"
                    )
                    time.sleep(delay)
                try:
                    result = self._sign_account(account)
                except Exception as exc:
                    logger.error(f"聚影签到：{account['remark']} 请求异常：{exc}", exc_info=True)
                    result = self._result(account, False, f"请求异常：{exc}")
                if result.get("success"):
                    return result
                if result.get("retryable") is False:
                    logger.warning(f"聚影签到：{account['remark']} 为不可重试错误，停止后续重试")
                    return result
            return result or self._result(account, False, "未知错误")
        except Exception as exc:
            logger.error(f"聚影签到：{account['remark']} 创建独立会话失败：{exc}", exc_info=True)
            return self._result(account, False, f"创建独立会话失败：{exc}")
        finally:
            self._close_account_clients(account["key"])

    def _ensure_csrftoken(self, account: Dict[str, Any]):
        if account.get("csrftoken"):
            return
        try:
            resp = self._request(
                account,
                "get",
                "https://www.jying.top/api/csrf/",
                headers={"User-Agent": self.USER_AGENT, "Referer": self.CHECKIN_PAGE_URL},
                timeout=10,
            )
            # 从响应或 cookies 中提取 csrftoken
            for name, val in resp.cookies.items():
                if name.lower() == "csrftoken" and val:
                    account["csrftoken"] = val
                    logger.info(f"聚影签到：{account['remark']} 已自动获取 csrftoken")
                    break
        except Exception:
            pass

    def _login_account(self, account: Dict[str, Any]) -> bool:
        username = account.get("username")
        password = account.get("password")
        if not username or not password:
            return False

        logger.info(f"聚影签到：{account['remark']} 正在使用账号密码自动登录...")
        self._ensure_csrftoken(account)
        headers = {
            "User-Agent": self.USER_AGENT,
            "Referer": "https://www.jying.top/login",
            "Origin": self.BASE_URL,
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if account.get("csrftoken"):
            headers["X-CSRFToken"] = account["csrftoken"]

        try:
            resp = self._request(
                account,
                "post",
                self.LOGIN_API_URL,
                headers=headers,
                json={"username": username, "password": password},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("token") or (data.get("user") or {}).get("token")
                if token:
                    account["app_user_token"] = token
                    logger.info(f"聚影签到：{account['remark']} 自动登录成功，已获取 Token")
                    return True
                logger.warning(f"聚影签到：{account['remark']} 登录响应无 token：{data}")
            else:
                try:
                    err_msg = resp.json().get("message")
                except Exception:
                    err_msg = resp.text[:100]
                logger.error(f"聚影签到：{account['remark']} 登录失败({resp.status_code})：{err_msg}")
        except Exception as exc:
            logger.error(f"聚影签到：{account['remark']} 登录请求异常：{exc}")
        return False

    def _sign_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"聚影签到：正在签到 {account['remark']}")

        # 补全 csrftoken（如未手动提供）
        self._ensure_csrftoken(account)

        # 如果未提供 token 但提供了账号和密码，先自动登录获取 token
        if not account.get("app_user_token") and account.get("username") and account.get("password"):
            if not self._login_account(account):
                return self._result(account, False, "账号密码自动登录失败", retryable=False)

        if not account.get("app_user_token") and not account.get("cookie"):
            return self._result(account, False, "未配置有效的 app_user_token 或账号密码", retryable=False)

        if self._use_browser:
            browser_result = self._sign_via_playwright(account)
            if browser_result is not None:
                return browser_result

        # 1. 尝试直接请求签到状态 API
        stats_headers = self._api_headers(account)
        try:
            stats_resp = self._request(
                account,
                "get",
                self.STATS_API_URL,
                headers=stats_headers,
                timeout=20,
            )
            if stats_resp.status_code == 401:
                # 尝试通过已配置的账号密码重新自动登录
                if account.get("username") and account.get("password") and self._login_account(account):
                    stats_headers = self._api_headers(account)
                    stats_resp = self._request(
                        account,
                        "get",
                        self.STATS_API_URL,
                        headers=stats_headers,
                        timeout=20,
                    )
                else:
                    err_msg = "登录已失效，请重新登录"
                    try:
                        err_msg = stats_resp.json().get("message") or err_msg
                    except Exception:
                        pass
                    logger.error(f"聚影签到：{account['remark']} 认证失败：{err_msg}（可直接在配置中输入 账号#密码 开启自动登录）")
                    return self._result(account, False, f"认证失败：{err_msg}(401)", retryable=False)
            
            if stats_resp.status_code == 200:
                stats_json = stats_resp.json()
                if isinstance(stats_json, dict):
                    if stats_json.get("checked_today"):
                        msg = "今日已签到"
                        if stats_json.get("checkin_mode") == "luck":
                            msg = "今日已选择幸运签到"
                        return self._result(
                            account,
                            True,
                            msg,
                            already_signed=True,
                            points=stats_json.get("reward_points"),
                            exp=stats_json.get("reward_exp"),
                            total_days=stats_json.get("my_total_days"),
                        )
        except Exception as exc:
            logger.warning(f"聚影签到：{account['remark']} 查询状态失败：{exc}")

        # 2. 发起签到 POST 请求
        checkin_headers = self._api_headers(account)
        try:
            resp = self._request(
                account,
                "post",
                self.CHECKIN_API_URL,
                headers=checkin_headers,
                json={},
                timeout=20,
            )
        except Exception as exc:
            logger.error(f"聚影签到：{account['remark']} POST 请求失败：{exc}")
            # 如果请求接口报错且未开启 use_browser，尝试自动降级到 playwright
            if not self._use_browser:
                browser_result = self._sign_via_playwright(account)
                if browser_result is not None:
                    return browser_result
            return self._result(account, False, f"网络请求失败：{exc}")

        if resp.status_code == 401:
            err_msg = "登录已失效，请重新登录"
            try:
                err_msg = resp.json().get("message") or err_msg
            except Exception:
                pass
            logger.error(f"聚影签到：{account['remark']} 签到认证失败：{err_msg}")
            return self._result(account, False, f"认证失败：{err_msg}(401)", retryable=False)

        try:
            data = resp.json()
        except Exception:
            data = {"message": resp.text[:200]}

        status_text = data.get("status") or ""
        msg = data.get("message") or ("签到成功" if resp.status_code == 200 else "签到失败")
        success = (resp.status_code == 200 and status_text != "error") or ("成功" in msg) or ("已签到" in msg)
        already_signed = "已" in msg or "重复" in msg

        # 成功后再次查询最新状态（获取累计天数与获得积分）
        reward_points = data.get("points") or data.get("reward_points")
        reward_exp = data.get("exp") or data.get("reward_exp")
        total_days = data.get("my_total_days")
        try:
            r = self._request(account, "get", self.STATS_API_URL, headers=stats_headers, timeout=10)
            if r.status_code == 200:
                s_json = r.json()
                if isinstance(s_json, dict):
                    reward_points = reward_points or s_json.get("reward_points")
                    reward_exp = reward_exp or s_json.get("reward_exp")
                    total_days = total_days or s_json.get("my_total_days")
        except Exception:
            pass

        return self._result(
            account,
            success,
            msg,
            already_signed=already_signed,
            points=reward_points,
            exp=reward_exp,
            total_days=total_days,
            retryable=not (resp.status_code in (400, 401, 403)),
        )

    def _sign_via_playwright(self, account: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """使用 Playwright 访问 https://www.jying.top/checkin 并点击「立即签到」按钮。"""
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            logger.warning(f"聚影签到：未安装 Playwright 或不可用：{exc}")
            return None

        try:
            with sync_playwright() as p:
                launch_kwargs: Dict[str, Any] = {"headless": True}
                proxies = self._get_proxies()
                if proxies:
                    proxy_server = proxies.get("http") or proxies.get("https")
                    if isinstance(proxy_server, str) and proxy_server.strip():
                        launch_kwargs["proxy"] = {"server": proxy_server.strip()}

                browser = p.chromium.launch(**launch_kwargs)
                try:
                    context = browser.new_context(
                        user_agent=self.USER_AGENT,
                        ignore_https_errors=not self._verify_ssl,
                    )
                    cookies = [
                        {
                            "name": name,
                            "value": value,
                            "domain": ".jying.top",
                            "path": "/",
                        }
                        for name, value in self._cookie_pairs(account["cookie"])
                    ]
                    context.add_cookies(cookies)

                    page = context.new_page()

                    # 如果有 token，注入 localStorage
                    if account.get("app_user_token"):
                        token = account["app_user_token"]
                        page.add_init_script(f"""
                            localStorage.setItem('app_user_token', '{token}');
                        """)

                    api_response_data: Dict[str, Any] = {}

                    def _on_response(resp):
                        try:
                            if "/api/app/checkin/do/" in resp.url and resp.request.method.upper() == "POST":
                                api_response_data.update(resp.json())
                        except Exception:
                            pass

                    page.on("response", _on_response)
                    page.goto(self.CHECKIN_PAGE_URL, wait_until="networkidle", timeout=30000)

                    # 检查是否已签到
                    page_text = page.content()
                    if "今天已经签到" in page_text or "今日已选择幸运签到" in page_text:
                        return self._result(
                            account,
                            True,
                            "今日已签到 (页面已完成)",
                            already_signed=True,
                        )

                    # 定位签到按钮：
                    btn = page.locator('button:has(.n-button__content:text-is("立即签到"))').first
                    if btn.count() == 0:
                        btn = page.locator('button:has-text("立即签到")').first
                    if btn.count() == 0:
                        btn = page.locator('button[data-v-b974fb4e]').first

                    if btn.count() > 0:
                        btn.click(timeout=10000)
                        page.wait_for_timeout(3000)
                    else:
                        logger.warning(f"聚影签到：{account['remark']} 页面未找到立即签到按钮，尝试回退接口")
                        return None

                    if api_response_data:
                        msg = api_response_data.get("message") or "签到成功"
                        return self._result(
                            account,
                            True,
                            msg,
                            points=api_response_data.get("points"),
                        )

                    # 再次检查文字
                    after_text = page.content()
                    if "今天已经签到" in after_text or "已签到" in after_text:
                        return self._result(account, True, "签到成功 (页面已变更)")

                    return self._result(account, True, "已点击签到按钮")
                finally:
                    browser.close()
        except Exception as exc:
            logger.warning(f"聚影签到：{account['remark']} 浏览器签到异常：{exc}")
            return None

    def _api_headers(self, account: Dict[str, Any]) -> Dict[str, str]:
        headers = {
            "User-Agent": self.USER_AGENT,
            "Referer": self.CHECKIN_PAGE_URL,
            "Origin": self.BASE_URL,
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }
        if account.get("csrftoken"):
            headers["X-CSRFToken"] = account["csrftoken"]
        if account.get("app_user_token"):
            headers["X-App-User-Token"] = account["app_user_token"]
        return headers

    def _open_account_clients(self, account: Dict[str, Any]):
        key = account["key"]
        proxies = self._get_proxies()

        # 1. Requests session
        session = requests.Session()
        session.headers.update({"User-Agent": self.USER_AGENT})
        session.verify = self._verify_ssl
        if proxies:
            session.proxies.update(proxies)
        for name, value in self._cookie_pairs(account["cookie"]):
            session.cookies.set(name, value, domain="www.jying.top")

        # 2. Cloudscraper session
        scraper = None
        if HAS_CLOUDSCRAPER:
            try:
                scraper = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows", "desktop": True},
                    sess=session,
                )
            except Exception as exc:
                logger.warning(f"聚影签到：初始化 cloudscraper 失败：{exc}")
                scraper = None

        # 3. curl_cffi session
        curl_session = None
        if HAS_CURL_CFFI:
            try:
                curl_session = curl_requests.Session(impersonate="chrome120", verify=self._verify_ssl)
                curl_session.headers.update({"User-Agent": self.USER_AGENT})
                if proxies:
                    curl_session.proxies = proxies
                for name, value in self._cookie_pairs(account["cookie"]):
                    try:
                        curl_session.cookies.set(name, value, domain="www.jying.top")
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning(f"聚影签到：初始化 curl_cffi 失败：{exc}")
                curl_session = None

        self._account_clients[key] = {
            "curl_session": curl_session,
            "scraper": scraper,
            "session": session,
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
                logger.warning(f"聚影签到：{account['remark']} 使用 {name} 请求失败：{exc}，尝试下一个客户端")
        raise RuntimeError("；".join(errors) or "账号 HTTP 会话未初始化或请求均失败")

    def _result(
        self,
        account: Dict[str, Any],
        success: bool,
        message: str,
        already_signed: bool = False,
        points: Any = None,
        exp: Any = None,
        total_days: Any = None,
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
            "exp": exp if exp is not None else "-",
            "total_days": total_days if total_days is not None else "-",
            "retryable": retryable,
        }

    def _random_wait(self):
        delay = random.uniform(self._min_delay, self._max_delay)
        logger.info(f"聚影签到：随机等待 {delay:.1f} 秒...")
        time.sleep(delay)

    def _get_proxies(self) -> Optional[Dict[str, str]]:
        if not self._use_proxy:
            return None
        proxy = getattr(settings, "PROXY", None)
        if not proxy:
            return None
        if isinstance(proxy, str):
            proxy = proxy.strip()
            if proxy:
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
                "exp": res["exp"],
                "total_days": res["total_days"],
                "message": res["message"],
            })
        self.save_data("sign_history", history[:300])

    def clear_sign_history(self):
        self.save_data("sign_history", [])
        logger.info("聚影签到：已清空历史记录")

    def _notify_results(self, results: List[Dict[str, Any]]):
        if not self._notify or not results:
            return
        success_count = sum(1 for r in results if r.get("success"))
        fail_count = len(results) - success_count
        title = f"聚影每日签到：{success_count} 成功 / {fail_count} 失败"

        lines = []
        for r in results:
            line = f"• {r['remark']}：{r['status']}（{r['message']}）"
            if r.get("points") != "-":
                line += f"，积分：{r['points']}"
            if r.get("total_days") != "-":
                line += f"，累计：{r['total_days']}天"
            lines.append(line)

        content = "\n".join(lines)
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=title,
                text=content,
            )
        except Exception as exc:
            logger.error(f"聚影签到：发送通知失败：{exc}")

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [{
            "id": "JYingCheckIn",
            "name": "聚影每日签到",
            "trigger": CronTrigger.from_crontab(self._cron, timezone=settings.TZ),
            "func": self.sign,
            "kwargs": {},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        example = (
            "# 【推荐方式 1】直接填聚影的用户名与密码（插件全自动登录+签到）：\n"
            "你的用户名#你的密码\n"
            "# 示例：带备注\n"
            "主账号#user1#pass123\n\n"
            "# 【方式 2】填 Token（在浏览器控制台执行 console.log(localStorage.getItem('app_user_token'))）：\n"
            "app_user_token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...;\n\n"
            "# 【方式 3】完整 Cookie 凭据：\n"
            "app_user_token=eyJhbGci...; csrftoken=43s2HQnUr6DifCdxtDk0C9zF1mtGEErG;"
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
                        self._col_switch("use_browser", "使用浏览器点击签到"),
                        self._col_switch("use_proxy", "使用系统代理"),
                        self._col_switch("verify_ssl", "验证 SSL 证书"),
                        self._col_switch("clear_history", "清除历史记录"),
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
                            "支持聚影 (https://www.jying.top/checkin) 每日自动签到。\n"
                            "多个账号请换行分隔，每行一个 Cookie。支持直接请求后台接口或通过 Playwright 自动化浏览器点击「立即签到」按钮。"
                        ),
                    },
                },
            ],
        }], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "15 8 * * *",
            "cookies_text": "",
            "use_browser": False,
            "use_proxy": True,
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
                    {"component": "td", "text": str(item.get("exp", "-"))},
                    {"component": "td", "text": str(item.get("total_days", "-"))},
                    {"component": "td", "text": item.get("message", "-")},
                ],
            })
        return [{
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": [
                {"component": "VCardTitle", "text": "聚影每日签到历史"},
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
                                        {"component": "th", "text": "获得经验"},
                                        {"component": "th", "text": "累计签到"},
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
            logger.error(f"聚影签到：停止调度失败：{exc}")
        finally:
            self._close_all_account_clients()

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []


jyingcheckin = JYingCheckIn

