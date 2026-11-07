"""GLaDOS multi-account daily check-in plugin for MoviePilot."""

import base64
import json
import random
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
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


class GladosBatch(_PluginBase):
    plugin_name = "GLaDOS 多账号签到"
    plugin_desc = "支持 GLaDOS 多账号每日自动签到，多个 Cookie 通过回车分隔，签到完成后发送通知。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    plugin_version = "1.0.0"
    plugin_author = "kbmgr"
    author_url = "https://github.com/AboutYour"
    plugin_config_prefix = "gladosbatch_"
    plugin_order = 2
    auth_level = 1

    BASE_URL = "https://glados.cloud"
    CHECKIN_PAGE_URL = "https://glados.cloud/console/checkin"
    CHECKIN_API_URL = "https://glados.cloud/api/user/checkin"
    STATUS_API_URL = "https://glados.cloud/api/user/status"
    CHECKIN_TOKEN = "glados.cloud"
    CHECKIN_BUTTON_SELECTOR = "button.ckb"

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    )

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "10 8 * * *"
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
        self._cron = "10 8 * * *"
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
        logger.info(f"GLaDOS签到 v{self.plugin_version}：已加载有效 Cookie 账号 {len(accounts)} 个")

        if self._onlyonce:
            self._onlyonce = False
            self._update_config()
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sign,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="GLaDOS多账号签到(单次)",
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
    def _extract_user_id_from_cookie(cookie_str: str) -> str:
        for part in cookie_str.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name.strip() == "koa:sess" and value.strip():
                raw_val = value.strip()
                padding = "=" * (-len(raw_val) % 4)
                try:
                    decoded = base64.urlsafe_b64decode(raw_val + padding).decode("utf-8", errors="ignore")
                    data = json.loads(decoded)
                    user_id = data.get("userId")
                    if user_id is not None:
                        return str(user_id)
                except Exception:
                    pass
        return ""

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

            # Support optional JSON line or "备注#cookie" / "备注|cookie" format,
            # while defaulting to plain 1-cookie-per-line separated by newline.
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

            if cookie.lower().startswith("cookie:"):
                cookie = cookie.split(":", 1)[1].strip()

            pairs = self._cookie_pairs(cookie)
            if not pairs:
                logger.warning(f"GLaDOS签到：第 {idx} 行 Cookie 格式无效，已跳过")
                continue

            user_id = self._extract_user_id_from_cookie(cookie)
            account_index = len(accounts) + 1
            if not remark:
                remark = f"账号{account_index}" + (f"({user_id})" if user_id else "")

            accounts.append({
                "key": f"glados:{account_index}",
                "index": account_index,
                "remark": remark,
                "user_id": user_id,
                "cookie": cookie,
                "base_url": self.BASE_URL,
            })
        return accounts

    def sign(self) -> List[Dict[str, Any]]:
        if not self._run_lock.acquire(blocking=False):
            logger.warning("GLaDOS签到：已有任务正在运行，跳过本次触发")
            return []
        self._running = True
        try:
            accounts = self._parse_accounts()
            if not accounts:
                logger.warning("GLaDOS签到：未配置有效 Cookie")
                return []
            logger.info(f"GLaDOS签到：开始处理 {len(accounts)} 个账号")
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
                        f"GLaDOS签到：{account['remark']} 第 {attempt}/{self._max_retries} 次重试，"
                        f"等待 {delay:.1f} 秒"
                    )
                    time.sleep(delay)
                try:
                    result = self._sign_account(account)
                except Exception as exc:
                    logger.error(f"GLaDOS签到：{account['remark']} 请求异常：{exc}", exc_info=True)
                    result = self._result(account, False, f"请求异常：{exc}")
                if result.get("success"):
                    return result
                if result.get("retryable") is False:
                    logger.warning(f"GLaDOS签到：{account['remark']} 为不可重试错误，停止后续重试")
                    return result
            return result or self._result(account, False, "未知错误")
        except Exception as exc:
            logger.error(f"GLaDOS签到：{account['remark']} 创建独立会话失败：{exc}", exc_info=True)
            return self._result(account, False, f"创建独立会话失败：{exc}")
        finally:
            self._close_account_clients(account["key"])

    def _sign_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"GLaDOS签到：正在签到 {account['remark']}")

        if self._use_browser:
            browser_result = self._sign_via_playwright(account)
            if browser_result is not None:
                return browser_result

        # 1. 打开签到页面 https://glados.cloud/console/checkin
        try:
            page_headers = self._page_headers()
            self._request(
                account,
                "get",
                self.CHECKIN_PAGE_URL,
                headers=page_headers,
                expect_json=False,
            )
        except Exception as exc:
            logger.debug(f"GLaDOS签到：{account['remark']} 预访问签到页异常（继续调用签到接口）：{exc}")

        # 2. 执行签到请求（对应页面点击 <button class="ckb">Checkin</button>）
        api_headers = self._api_headers()
        payload = json.dumps({"token": self.CHECKIN_TOKEN}).encode("utf-8")
        response = self._request(
            account,
            "post",
            self.CHECKIN_API_URL,
            headers=api_headers,
            data=payload,
            expect_json=True,
        )
        checkin_info = self._parse_checkin_response(response)

        # 3. 查询账号状态（邮箱、剩余天数等）
        status_info = (
            self._fetch_user_status(account)
            if checkin_info.get("retryable", True) or checkin_info.get("success")
            else {}
        )

        email = status_info.get("email") or ""
        left_days = status_info.get("left_days")
        display_remark = account["remark"]
        if email and email not in display_remark:
            display_remark = f"{account['remark']} ({email})"

        result = self._result(
            account,
            checkin_info["success"],
            checkin_info.get("message") or ("签到成功" if checkin_info["success"] else "签到失败"),
            already_signed=checkin_info.get("already_signed", False),
            remark=display_remark,
            email=email,
            points=checkin_info.get("points"),
            balance=checkin_info.get("balance"),
            left_days=left_days,
            retryable=checkin_info.get("retryable", True),
        )
        logger.info(
            f"GLaDOS签到：{result['remark']} "
            f"{result['status']}：{result['message']}"
            + (f"，积分变动={result.get('points')}" if result.get("points") is not None else "")
            + (f"，总积分={result.get('balance')}" if result.get("balance") is not None else "")
            + (f"，剩余天数={result.get('left_days')}" if result.get("left_days") is not None else "")
        )
        return result

    def _sign_via_playwright(self, account: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """打开 https://glados.cloud/console/checkin 并点击 <button class="ckb">Checkin</button>。"""
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            logger.warning(f"GLaDOS签到：未安装 Playwright，自动回退 HTTP 签到模式：{exc}")
            return None

        try:
            with sync_playwright() as p:
                launch_kwargs: Dict[str, Any] = {"headless": True}
                proxies = self._get_proxies()
                if proxies and proxies.get("http"):
                    launch_kwargs["proxy"] = {"server": proxies["http"]}

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
                            "domain": "glados.cloud",
                            "path": "/",
                        }
                        for name, value in self._cookie_pairs(account["cookie"])
                    ]
                    context.add_cookies(cookies)
                    page = context.new_page()

                    checkin_payload: Dict[str, Any] = {}
                    status_payload: Dict[str, Any] = {}

                    def _on_response(resp):
                        try:
                            if "/api/user/checkin" in resp.url and resp.request.method.upper() == "POST":
                                checkin_payload.update(resp.json())
                            elif "/api/user/status" in resp.url:
                                status_payload.update(resp.json())
                        except Exception:
                            pass

                    page.on("response", _on_response)
                    page.goto(self.CHECKIN_PAGE_URL, wait_until="networkidle", timeout=45000)

                    btn = page.locator(self.CHECKIN_BUTTON_SELECTOR).first
                    if btn.count() == 0:
                        btn = page.locator("button:has-text('Checkin')").first

                    if btn.count() > 0:
                        btn.click(timeout=15000)
                        page.wait_for_timeout(4000)
                    else:
                        logger.warning(f"GLaDOS签到：{account['remark']} 页面未找到 button.ckb，回退接口签到")
                        return None

                    if not checkin_payload:
                        return None

                    checkin_info = self._parse_checkin_dict(checkin_payload, 200)
                    status_data = status_payload.get("data") if isinstance(status_payload, dict) else {}
                    if not isinstance(status_data, dict) or not status_data:
                        status_info = self._fetch_user_status(account)
                    else:
                        status_info = {
                            "email": str(status_data.get("email") or "").strip(),
                            "left_days": self._format_number(status_data.get("leftDays")),
                        }

                    email = status_info.get("email") or ""
                    display_remark = account["remark"]
                    if email and email not in display_remark:
                        display_remark = f"{account['remark']} ({email})"

                    return self._result(
                        account,
                        checkin_info["success"],
                        checkin_info.get("message") or ("签到成功" if checkin_info["success"] else "签到失败"),
                        already_signed=checkin_info.get("already_signed", False),
                        remark=display_remark,
                        email=email,
                        points=checkin_info.get("points"),
                        balance=checkin_info.get("balance"),
                        left_days=status_info.get("left_days"),
                        retryable=checkin_info.get("retryable", True),
                    )
                finally:
                    browser.close()
        except Exception as exc:
            logger.warning(f"GLaDOS签到：{account['remark']} 浏览器点击签到异常，回退 HTTP 模式：{exc}")
            return None

    def _page_headers(self) -> Dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": self.BASE_URL + "/",
            "User-Agent": self.USER_AGENT,
        }

    def _api_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": self.BASE_URL,
            "Referer": self.CHECKIN_PAGE_URL,
            "User-Agent": self.USER_AGENT,
        }

    @staticmethod
    def _format_number(value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        try:
            dec = Decimal(str(value))
            normalized = dec.normalize()
            text = format(normalized, "f")
            return text if text != "-0" else "0"
        except (InvalidOperation, ValueError, TypeError):
            return str(value)

    @classmethod
    def _parse_checkin_dict(cls, data: Dict[str, Any], status_code: int = 200) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "success": False,
            "already_signed": False,
            "retryable": True,
            "message": "",
            "points": None,
            "balance": None,
        }
        code = data.get("code")
        message = str(data.get("message") or "").strip()
        repeated = bool(data.get("repeated"))

        records = data.get("list")
        latest = records[0] if isinstance(records, list) and records and isinstance(records[0], dict) else {}
        points = cls._format_number(data.get("points"))
        if (points is None or points == "0") and latest.get("change") is not None:
            points = cls._format_number(latest.get("change"))
        balance = cls._format_number(latest.get("balance"))

        result["points"] = points
        result["balance"] = balance
        result["message"] = message or f"HTTP {status_code}"

        msg_lower = message.lower()
        if code == 0 and not repeated:
            result["success"] = True
        elif repeated or (code == 1 and ("logged" in msg_lower or "tomorrow" in msg_lower or "try tomorrow" in msg_lower or "already" in msg_lower)):
            result.update(success=True, already_signed=True)
        elif "checkin!" in msg_lower or "got" in msg_lower:
            result["success"] = True
        elif code == -2 or "没有权限" in message or "unauthorized" in msg_lower or "login" in msg_lower:
            result.update(
                retryable=False,
                message=f"Cookie 已失效或无权限 ({message or 'code=-2'})",
            )
        elif status_code == 429:
            result["message"] = "请求过于频繁，被站点限流"
        elif status_code == 403:
            result["message"] = "请求被 Cloudflare 拦截 (HTTP 403)"
        return result

    @classmethod
    def _parse_checkin_response(cls, response: Any) -> Dict[str, Any]:
        status_code = getattr(response, "status_code", 0)
        try:
            data = response.json()
            if isinstance(data, dict):
                return cls._parse_checkin_dict(data, status_code)
        except Exception:
            pass

        text = str(getattr(response, "text", "") or "")
        result = {
            "success": False,
            "already_signed": False,
            "retryable": True,
            "message": f"非 JSON 响应 (HTTP {status_code or '-'})",
            "points": None,
            "balance": None,
        }
        if status_code == 429:
            result["message"] = "请求过于频繁，被站点限流"
        elif status_code == 403:
            result["message"] = "请求被 Cloudflare 拦截 (HTTP 403)"
        elif "没有权限" in text:
            result.update(retryable=False, message="Cookie 已失效或无权限")
        return result

    def _fetch_user_status(self, account: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = self._request(
                account,
                "get",
                self.STATUS_API_URL,
                headers=self._api_headers(),
                expect_json=True,
            )
            data = response.json()
            if not isinstance(data, dict) or data.get("code") != 0:
                return {}
            info = data.get("data") or {}
            if not isinstance(info, dict):
                return {}
            return {
                "email": str(info.get("email") or "").strip(),
                "user_id": str(info.get("userId") or "").strip(),
                "left_days": self._format_number(info.get("leftDays")),
                "vip": info.get("vip"),
            }
        except Exception as exc:
            logger.warning(f"GLaDOS签到：{account['remark']} 获取账号状态失败：{exc}")
            return {}

    @staticmethod
    def _result(
        account: Dict[str, Any],
        success: bool,
        message: str,
        already_signed: bool = False,
        **extra: Any,
    ) -> Dict[str, Any]:
        result = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "account_key": account["key"],
            "remark": extra.pop("remark", account["remark"]),
            "success": bool(success),
            "status": "今日已签到" if already_signed else ("签到成功" if success else "签到失败"),
            "message": str(message or ""),
        }
        result.update({key: value for key, value in extra.items() if value not in (None, "")})
        return result

    @staticmethod
    def _cookie_pairs(cookie_header: str) -> List[Tuple[str, str]]:
        cookie_header = str(cookie_header or "").strip()
        if cookie_header.lower().startswith("cookie:"):
            cookie_header = cookie_header.split(":", 1)[1].strip()
        pairs = []
        for part in cookie_header.replace("\r", "").replace("\n", "").split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name.strip():
                pairs.append((name.strip(), value.strip()))
        return pairs

    def _seed_client_cookies(self, client: Any, account: Dict[str, Any]):
        hostname = urlparse(account["base_url"]).hostname or "glados.cloud"
        for name, value in self._cookie_pairs(account["cookie"]):
            client.cookies.set(name, value, domain=hostname, path="/")

    def _open_account_clients(self, account: Dict[str, Any]):
        key = account["key"]
        self._close_account_clients(key)
        clients: Dict[str, Any] = {}

        if HAS_CLOUDSCRAPER:
            try:
                clients["cloudscraper"] = cloudscraper.create_scraper(browser="chrome")
            except Exception as exc:
                logger.warning(f"GLaDOS签到：{account['remark']} 初始化 cloudscraper 失败：{exc}")
        if HAS_CURL_CFFI:
            try:
                clients["curl_cffi"] = curl_requests.Session(impersonate="chrome110")
            except Exception as exc:
                logger.warning(f"GLaDOS签到：{account['remark']} 初始化 curl_cffi 失败：{exc}")
        clients["requests"] = requests.Session()

        for backend, client in list(clients.items()):
            try:
                self._seed_client_cookies(client, account)
            except Exception as exc:
                try:
                    client.close()
                except Exception:
                    pass
                clients.pop(backend, None)
                logger.warning(f"GLaDOS签到：{account['remark']} 初始化 {backend} Cookie 会话失败：{exc}")
        if "requests" not in clients:
            for client in clients.values():
                try:
                    client.close()
                except Exception:
                    pass
            raise RuntimeError("无法创建账号独立 HTTP 会话")
        self._account_clients[key] = clients

    def _close_account_clients(self, account_key: str):
        clients = self._account_clients.pop(account_key, {})
        for client in clients.values():
            try:
                client.close()
            except Exception:
                pass

    def _close_all_account_clients(self):
        for account_key in list(getattr(self, "_account_clients", {})):
            self._close_account_clients(account_key)

    def _request(
        self,
        account: Dict[str, Any],
        method: str,
        url: str,
        headers: Dict[str, str],
        data: Any = None,
        expect_json: bool = True,
    ):
        proxies = self._get_proxies()
        verify = self._verify_ssl
        errors = []
        clients = self._account_clients.get(account["key"])
        if not clients:
            self._open_account_clients(account)
            clients = self._account_clients[account["key"]]

        for backend in ("cloudscraper", "curl_cffi", "requests"):
            if backend not in clients:
                continue
            try:
                response = clients[backend].request(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    proxies=proxies,
                    timeout=30,
                    verify=verify,
                )
                if self._usable_response(response, expect_json=expect_json):
                    return response
                errors.append(f"{backend} HTTP {response.status_code}")
                if backend == "requests":
                    return response
            except Exception as exc:
                errors.append(f"{backend}: {exc}")
                if backend == "requests":
                    raise RuntimeError("；".join(errors)) from exc

        raise RuntimeError("；".join(errors) or "请求失败")

    @staticmethod
    def _usable_response(response: Any, expect_json: bool = True) -> bool:
        if response.status_code in (403, 429):
            return False
        if not expect_json:
            return response.status_code < 500
        content_type = str(response.headers.get("Content-Type") or "").lower()
        return "text/html" not in content_type

    def _get_proxies(self) -> Optional[Dict[str, str]]:
        if not self._use_proxy:
            return None
        proxy = getattr(settings, "PROXY", None)
        if isinstance(proxy, str) and proxy:
            return {"http": proxy, "https": proxy}
        if isinstance(proxy, dict):
            http = proxy.get("http") or proxy.get("HTTP") or proxy.get("https") or proxy.get("HTTPS")
            https = proxy.get("https") or proxy.get("HTTPS") or proxy.get("http") or proxy.get("HTTP")
            if http or https:
                return {"http": http or https, "https": https or http}
        return None

    def _random_wait(self):
        if self._max_delay <= 0:
            return
        delay = random.uniform(self._min_delay, self._max_delay)
        logger.info(f"GLaDOS签到：账号间随机等待 {delay:.1f} 秒")
        time.sleep(delay)

    def _save_results(self, results: List[Dict[str, Any]]):
        history = self.get_data("sign_history") or []
        history.extend(results)
        cutoff = datetime.now() - timedelta(days=self._history_days)
        kept = []
        for item in history:
            try:
                if datetime.strptime(item.get("date", ""), "%Y-%m-%d %H:%M:%S") >= cutoff:
                    kept.append(item)
            except (TypeError, ValueError):
                continue
        self.save_data("sign_history", kept[-1000:])
        self.save_data("last_results", results)

    def _notify_results(self, results: List[Dict[str, Any]]):
        if not self._notify or not results:
            return
        success_count = sum(1 for item in results if item.get("success"))
        lines = [f"共 {len(results)} 个账号，成功 {success_count} 个，失败 {len(results) - success_count} 个"]
        for item in results:
            extras = []
            if item.get("points") is not None:
                extras.append(f"本次 +{item['points']} 积分")
            if item.get("balance") is not None:
                extras.append(f"总积分 {item['balance']}")
            if item.get("left_days") is not None:
                extras.append(f"剩余 {item['left_days']} 天")
            extra_str = f"（{'，'.join(extras)}）" if extras else ""
            lines.append(
                f"{item['remark']}：{item['status']}{extra_str}；{item.get('message') or '-'}"
            )
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title="【GLaDOS 多账号签到】",
            text="\n".join(lines),
        )

    def clear_sign_history(self):
        self.save_data("sign_history", [])
        self.save_data("last_results", [])
        logger.info("GLaDOS签到：历史记录已清空")

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "gladosbatch",
                "name": "GLaDOS多账号签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sign,
                "kwargs": {},
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        example = (
            "koa:sess=eyJ1c2VySWQiOjc1NTc5MywiX2V4cGlyZSI6MTgxMzUzOTYwNzg4NiwiX21heEFnZSI6MjU5MjAwMDAwMDB9; "
            "koa:sess.sig=WxmMYCCjOaRk9Bb9HMcR29_cd90; "
            "gld:sess=gld_a1c9f39401fa4a71c5a96cc35c10d16eb0ef2a45948d6ccd; "
            "gld:sess.sig=bojSCkUzCy760lW3wxQe0WvuRqo\n"
            "主号#koa:sess=...; koa:sess.sig=...; gld:sess=...; gld:sess.sig=..."
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
                                "label": "Cookie 列表（多个 Cookie 通过回车换行分隔，每行一个账号）",
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
                            "多个账号的 Cookie 请用回车换行分隔（每行一个 Cookie），"
                            "支持直接粘贴完整 Cookie，也支持「备注#Cookie」格式。"
                            "插件会访问 https://glados.cloud/console/checkin 完成签到并汇总通知。"
                        ),
                    },
                },
            ],
        }], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "10 8 * * *",
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
                    {"component": "td", "text": str(item.get("balance", "-"))},
                    {"component": "td", "text": str(item.get("left_days", "-"))},
                    {"component": "td", "text": item.get("message", "-")},
                ],
            })
        return [{
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": [
                {"component": "VCardTitle", "text": "GLaDOS 多账号签到历史"},
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
                                        {"component": "th", "text": "本次积分"},
                                        {"component": "th", "text": "总积分"},
                                        {"component": "th", "text": "剩余天数"},
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
            logger.error(f"GLaDOS签到：停止调度失败：{exc}")
        finally:
            self._close_all_account_clients()

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

