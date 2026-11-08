"""123云盘臻藏阁 (123.qsxy.top) 每日自动签到插件。"""

import json
import random
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

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


class Top123CheckIn(_PluginBase):
    plugin_name = "123云盘臻藏阁每日签到"
    plugin_desc = "支持 123云盘臻藏阁 (123.qsxy.top) 每日自动签到，自动点击用户中心抽屉并触发签到，支持多账号、签到历史与通知。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    plugin_version = "1.0.0"
    plugin_author = "kbmgr"
    author_url = "https://github.com/AboutYour"
    plugin_config_prefix = "top123checkin_"
    plugin_order = 4
    auth_level = 1

    BASE_URL = "https://123.qsxy.top"
    CHECKIN_PAGE_URL = "https://123.qsxy.top/"
    B2_CHECKIN_API_URL = "https://123.qsxy.top/wp-json/b2/v1/userCheckIn"
    AJAX_CHECKIN_API_URL = "https://123.qsxy.top/wp-admin/admin-ajax.php"

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    )

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "20 8 * * *"
    _cookies_text = ""
    _use_browser = True  # 网站有 BTWAF 盾及抽屉弹窗，默认推荐使用浏览器
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
        self._cron = "20 8 * * *"
        self._cookies_text = ""
        self._use_browser = True
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
            self._use_browser = bool(config.get("use_browser", True))
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
        logger.info(f"123云盘臻藏阁签到 v{self.plugin_version}：已加载有效 Cookie 账号 {len(accounts)} 个")

        if self._onlyonce:
            self._onlyonce = False
            self._update_config()
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sign,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="123云盘臻藏阁每日签到(单次)",
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
    def _extract_username_from_cookie(cookie_str: str) -> str:
        for part in cookie_str.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name.strip().startswith("wordpress_logged_in_") and value.strip():
                try:
                    decoded = unquote(value.strip())
                    username = decoded.split("|")[0].strip()
                    if username:
                        return username
                except Exception:
                    pass
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
                logger.warning(f"123云盘臻藏阁签到：第 {idx} 行 Cookie 格式无效，已跳过")
                continue

            username = self._extract_username_from_cookie(cookie)
            account_index = len(accounts) + 1
            if not remark:
                remark = f"123云盘账号{account_index}" + (f"({username})" if username else "")

            accounts.append({
                "key": f"top123:{account_index}",
                "index": account_index,
                "remark": remark,
                "username": username,
                "cookie": cookie,
                "base_url": self.BASE_URL,
            })
        return accounts

    def sign(self) -> List[Dict[str, Any]]:
        if not self._run_lock.acquire(blocking=False):
            logger.warning("123云盘臻藏阁签到：已有任务正在运行，跳过本次触发")
            return []
        self._running = True
        try:
            accounts = self._parse_accounts()
            if not accounts:
                logger.warning("123云盘臻藏阁签到：未配置有效 Cookie")
                return []
            logger.info(f"123云盘臻藏阁签到：开始处理 {len(accounts)} 个账号")
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
                        f"123云盘臻藏阁签到：{account['remark']} 第 {attempt}/{self._max_retries} 次重试，"
                        f"等待 {delay:.1f} 秒"
                    )
                    time.sleep(delay)
                try:
                    result = self._sign_account(account)
                except Exception as exc:
                    logger.error(f"123云盘臻藏阁签到：{account['remark']} 请求异常：{exc}", exc_info=True)
                    result = self._result(account, False, f"请求异常：{exc}")
                if result.get("success"):
                    return result
                if result.get("retryable") is False:
                    logger.warning(f"123云盘臻藏阁签到：{account['remark']} 为不可重试错误，停止后续重试")
                    return result
            return result or self._result(account, False, "未知错误")
        except Exception as exc:
            logger.error(f"123云盘臻藏阁签到：{account['remark']} 创建独立会话失败：{exc}", exc_info=True)
            return self._result(account, False, f"创建独立会话失败：{exc}")
        finally:
            self._close_account_clients(account["key"])

    def _sign_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"123云盘臻藏阁签到：正在签到 {account['remark']}")

        # 1. 优先使用 Playwright 浏览器自动化处理
        if self._use_browser:
            browser_result = self._sign_via_playwright(account)
            if browser_result is not None:
                return browser_result

        # 2. 备用 HTTP 接口请求（如果无 Playwright 或浏览器模式失败回退）
        logger.info(f"123云盘臻藏阁签到：{account['remark']} 尝试 HTTP 接口签到")
        headers = {
            "User-Agent": self.USER_AGENT,
            "Referer": self.CHECKIN_PAGE_URL,
            "Origin": self.BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }

        # 尝试 B2 主题 REST 接口
        try:
            resp = self._request(
                account,
                "post",
                self.B2_CHECKIN_API_URL,
                headers=headers,
                timeout=20,
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    return self._parse_api_response(account, data)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning(f"123云盘臻藏阁签到：B2 REST 接口失败：{exc}")

        # 尝试 WordPress admin-ajax 接口
        try:
            ajax_data = {"action": "user_checkin"}
            resp = self._request(
                account,
                "post",
                self.AJAX_CHECKIN_API_URL,
                data=ajax_data,
                headers=headers,
                timeout=20,
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    return self._parse_api_response(account, data)
                except Exception:
                    if "今日已签到" in resp.text:
                        return self._result(account, True, "今日已签到", already_signed=True)
                    return self._result(account, True, "签到响应成功")
        except Exception as exc:
            logger.error(f"123云盘臻藏阁签到：admin-ajax 接口失败：{exc}")

        return self._result(account, False, "签到请求未成功，请检查 Cookie 或开启浏览器模式")

    def _parse_api_response(self, account: Dict[str, Any], data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            return self._result(account, True, "签到完成")
        msg = data.get("msg") or data.get("message") or ""
        credit = data.get("credit") or data.get("points") or data.get("gold")
        already_signed = "今日已签到" in msg or "已签到" in msg or "重复" in msg
        success = already_signed or bool(data.get("status") in (200, "success", 1, True)) or ("成功" in msg)
        return self._result(
            account,
            success=success,
            message=msg or ("签到成功" if success else "签到未确认"),
            already_signed=already_signed,
            points=credit,
        )

    def _sign_via_playwright(self, account: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        使用 Playwright：
        1. 打开 https://123.qsxy.top/
        2. 点击 <svg data-viewbox="50 0 924 924"> 用户图标展开个人中心
        3. 点击 <a class="initiate-checkin" form-action="user_checkin"> 签到按钮
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            logger.warning(f"123云盘臻藏阁签到：未安装 Playwright 或不可用：{exc}")
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
                            "domain": ".qsxy.top",
                            "path": "/",
                        }
                        for name, value in self._cookie_pairs(account["cookie"])
                    ]
                    context.add_cookies(cookies)

                    page = context.new_page()

                    intercepted_payload: Dict[str, Any] = {}

                    def _on_response(resp):
                        try:
                            if any(k in resp.url for k in ("user_checkin", "userCheckIn", "admin-ajax")):
                                data = resp.json()
                                if isinstance(data, dict):
                                    intercepted_payload.update(data)
                        except Exception:
                            pass

                    page.on("response", _on_response)
                    logger.info(f"123云盘臻藏阁签到：{account['remark']} 正在打开网页...")
                    page.goto(self.CHECKIN_PAGE_URL, wait_until="networkidle", timeout=35000)

                    # 第一步：点击用户图标展开抽屉/菜单
                    # 匹配用户指定的 SVG：<svg class="icon" aria-hidden="true" data-viewbox="50 0 924 924" viewBox="50 0 924 924"><use xlink:href="#icon-user"></use></svg>
                    logger.info(f"123云盘臻藏阁签到：{account['remark']} 正在寻找用户图标...")
                    user_icon = page.locator('svg:has(use[*|href*="icon-user"])').first
                    if user_icon.count() == 0:
                        user_icon = page.locator('svg[data-viewbox="50 0 924 924"]').first
                    if user_icon.count() == 0:
                        user_icon = page.locator('[data-viewbox="50 0 924 924"]').first

                    if user_icon.count() > 0:
                        try:
                            # 尝试点击该用户图标或者其父级 a/button
                            user_icon.click(timeout=8000)
                            page.wait_for_timeout(1500)
                        except Exception as e:
                            logger.info(f"直接点击 svg 提示 {e}，尝试使用 JS 触发点击")
                            page.evaluate("""
                                const svg = document.querySelector('svg use[href*="icon-user"], svg use[*|href*="icon-user"], svg[data-viewbox="50 0 924 924"]');
                                if (svg) {
                                    const target = svg.closest('a, button, div') || svg;
                                    target.click();
                                }
                            """)
                            page.wait_for_timeout(1500)
                    else:
                        logger.warning(f"123云盘臻藏阁签到：{account['remark']} 未直接找到用户图标 SVG，尝试直接查找签到按钮")

                    # 第二步：定位签到按钮
                    # <a class="but block c-yellow initiate-checkin" href="javascript:;" form-action="user_checkin" ...>
                    btn = page.locator('a.initiate-checkin').first
                    if btn.count() == 0:
                        btn = page.locator('a[form-action="user_checkin"]').first
                    if btn.count() == 0:
                        btn = page.locator('a:has-text("签到领取今日奖励")').first
                    if btn.count() == 0:
                        btn = page.locator('a:has-text("今日已签到")').first

                    if btn.count() == 0:
                        logger.warning(f"123云盘臻藏阁签到：{account['remark']} 展开菜单后未找到签到按钮")
                        return None

                    # 检查按钮是否已经显示已签到
                    btn_text = btn.inner_text() or ""
                    btn_html = btn.evaluate("el => el.outerHTML") or ""
                    if "今日已签到" in btn_text or "今日已签到" in btn_html:
                        return self._result(
                            account,
                            True,
                            "今日已签到",
                            already_signed=True,
                        )

                    logger.info(f"123云盘臻藏阁签到：{account['remark']} 找到签到按钮，正在点击...")
                    btn.click(timeout=10000)
                    page.wait_for_timeout(3000)

                    # 检查拦截到的 API 响应
                    if intercepted_payload:
                        return self._parse_api_response(account, intercepted_payload)

                    # 检查页面是否有更新提示
                    after_html = page.content()
                    if "今日已签到" in after_html or "签到成功" in after_html:
                        return self._result(account, True, "签到成功 (页面已更新)")

                    return self._result(account, True, "已点击签到按钮")
                finally:
                    browser.close()
        except Exception as exc:
            logger.warning(f"123云盘臻藏阁签到：{account['remark']} 浏览器签到流程异常：{exc}")
            return None

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
            session.cookies.set(name, value, domain="123.qsxy.top")

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
                curl_session.headers.update({"User-Agent": self.USER_AGENT})
                if proxies:
                    curl_session.proxies = proxies
                for name, value in self._cookie_pairs(account["cookie"]):
                    curl_session.cookies.set(name, value, domain="123.qsxy.top")
            except Exception:
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
        client = clients.get("curl_session") or clients.get("scraper") or clients.get("session")
        if client is None:
            raise RuntimeError("账号 HTTP 会话未初始化")
        return client.request(method=method, url=url, **kwargs)

    def _result(
        self,
        account: Dict[str, Any],
        success: bool,
        message: str,
        already_signed: bool = False,
        points: Any = None,
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
            "retryable": retryable,
        }

    def _random_wait(self):
        delay = random.uniform(self._min_delay, self._max_delay)
        logger.info(f"123云盘臻藏阁签到：随机等待 {delay:.1f} 秒...")
        time.sleep(delay)

    def _get_proxies(self) -> Optional[Dict[str, str]]:
        if not self._use_proxy or not settings.PROXY:
            return None
        proxy_url = settings.PROXY
        return {
            "http": proxy_url,
            "https": proxy_url,
        }

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
                "message": res["message"],
            })
        self.save_data("sign_history", history[:300])

    def clear_sign_history(self):
        self.save_data("sign_history", [])
        logger.info("123云盘臻藏阁签到：已清空历史记录")

    def _notify_results(self, results: List[Dict[str, Any]]):
        if not self._notify or not results:
            return
        success_count = sum(1 for r in results if r.get("success"))
        fail_count = len(results) - success_count
        title = f"123云盘臻藏阁签到：{success_count} 成功 / {fail_count} 失败"

        lines = []
        for r in results:
            line = f"• {r['remark']}：{r['status']}（{r['message']}）"
            if r.get("points") != "-":
                line += f"，获得积分：{r['points']}"
            lines.append(line)

        content = "\n".join(lines)
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=title,
                text=content,
            )
        except Exception as exc:
            logger.error(f"123云盘臻藏阁签到：发送通知失败：{exc}")

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [{
            "id": "Top123CheckIn",
            "name": "123云盘臻藏阁每日签到",
            "trigger": CronTrigger.from_crontab(self._cron, timezone=settings.TZ),
            "func": self.sign,
            "kwargs": {},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        example = (
            "# 方式1：直接粘贴完整 Cookie（支持换行分隔多账号）\n"
            "PHPSESSID=...; wordpress_logged_in_...; fps_accelerat=...;\n\n"
            "# 方式2：备注#Cookie\n"
            "我的账号#PHPSESSID=...; wordpress_logged_in_...; fps_accelerat=...;"
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
                        self._col_switch("use_browser", "使用浏览器自动化点击(推荐)"),
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
                            "支持 123云盘臻藏阁 (https://123.qsxy.top/) 每日自动签到。\n"
                            "多个账号请用回车换行分隔（每行一个完整 Cookie）。\n"
                            "推荐开启浏览器自动化点击：插件将自动访问网站，点击右上角用户图标唤出个人抽屉，并点击「签到领取今日奖励」按钮。"
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
            "use_browser": True,
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
                    {"component": "td", "text": item.get("message", "-")},
                ],
            })
        return [{
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": [
                {"component": "VCardTitle", "text": "123云盘臻藏阁签到历史"},
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
            logger.error(f"123云盘臻藏阁签到：停止调度失败：{exc}")
        finally:
            self._close_all_account_clients()

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []


top123checkin = Top123CheckIn

