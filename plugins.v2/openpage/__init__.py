"""网页批量登录与自动签到插件 (OpenPage) for MoviePilot。"""

import json
import random
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

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


class OpenPage(_PluginBase):
    plugin_name = "网页批量登录签到"
    plugin_desc = "支持批量维护多个站点进行自动登录与网页签到，支持「签到地址+用户名+密码」格式维护、固定网络代理、多站点依次顺序执行及汇总通知。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/movie.png"
    plugin_version = "1.0.0"
    plugin_author = "kbmgr"
    author_url = "https://github.com/AboutYour"
    plugin_config_prefix = "openpage_"
    plugin_order = 5
    auth_level = 1

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    )

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "30 8 * * *"
    _sites_text = ""
    _proxy_server = ""  # 固定代理服务器，例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080
    _use_proxy = False  # 是否启用网络代理
    _headless = True
    _min_delay = 3
    _max_delay = 8
    _page_timeout = 35
    _history_days = 30
    _clear_history = False

    _scheduler: Optional[BackgroundScheduler] = None
    _running = False
    _run_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self._enabled = False
        self._notify = True
        self._onlyonce = False
        self._cron = "30 8 * * *"
        self._sites_text = ""
        self._proxy_server = ""
        self._use_proxy = False
        self._headless = True
        self._min_delay = 3
        self._max_delay = 8
        self._page_timeout = 35
        self._history_days = 30
        self._clear_history = False

        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._onlyonce = bool(config.get("onlyonce"))
            self._cron = str(config.get("cron") or self._cron).strip()
            self._sites_text = str(
                config.get("sites_text")
                or config.get("sites")
                or config.get("accounts")
                or ""
            ).strip()
            self._proxy_server = str(config.get("proxy_server") or "").strip()
            self._use_proxy = bool(config.get("use_proxy", False))
            # 智能联动：如果填写了固定代理服务器，但未显式开启代理开关，自动启用
            if self._proxy_server and not self._use_proxy:
                self._use_proxy = True

            self._headless = bool(config.get("headless", True))
            self._min_delay = self._as_int(config.get("min_delay"), 3, 0, 300)
            self._max_delay = self._as_int(config.get("max_delay"), 8, 0, 300)
            self._page_timeout = self._as_int(config.get("page_timeout"), 35, 10, 180)
            self._history_days = self._as_int(config.get("history_days"), 30, 1, 365)
            self._clear_history = bool(config.get("clear_history"))

        if self._max_delay < self._min_delay:
            self._min_delay, self._max_delay = self._max_delay, self._min_delay

        if self._clear_history:
            self.clear_sign_history()
            self._clear_history = False
            self._update_config()

        sites = self._parse_sites()
        logger.info(
            f"网页批量签到 v{self.plugin_version}：已加载有效维护站点 {len(sites)} 个，"
            f"固定代理: {self._proxy_server or '未设置'}"
        )

        if self._onlyonce:
            self._onlyonce = False
            self._update_config()
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            tz_obj = pytz.timezone(settings.TZ) if pytz else (ZoneInfo(settings.TZ) if ZoneInfo else None)
            self._scheduler.add_job(
                func=self.sign,
                trigger="date",
                run_date=datetime.now(tz=tz_obj) + timedelta(seconds=3),
                name="网页批量签到(单次触发)",
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
            "sites_text": self._sites_text,
            "proxy_server": self._proxy_server,
            "use_proxy": self._use_proxy,
            "headless": self._headless,
            "min_delay": self._min_delay,
            "max_delay": self._max_delay,
            "page_timeout": self._page_timeout,
            "history_days": self._history_days,
            "clear_history": self._clear_history,
        })

    def _parse_sites(self) -> List[Dict[str, Any]]:
        """
        解析站点列表。
        支持格式：
          1. 签到地址+用户名+密码
             https://example.com/login+myuser+mypassword
          2. 备注#签到地址+用户名+密码
             我的站点#https://example.com/login+myuser+mypassword
          3. 签到地址#用户名#密码 或 签到地址|用户名|密码
          4. 包含每站专属代理（第4项）：
             https://example.com/login+myuser+mypassword+http://127.0.0.1:7890
          5. JSON 对象字符串
        """
        text = str(self._sites_text or "").strip()
        if not text:
            return []

        sites: List[Dict[str, Any]] = []
        for line_num, raw_line in enumerate(text.splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("//"):
                continue

            # 过滤纯注释行
            if line.startswith("#") and not ("http://" in line or "https://" in line):
                continue

            site_index = len(sites) + 1
            parsed = self._parse_single_site_line(line, site_index)
            if parsed:
                sites.append(parsed)
            else:
                logger.warning(f"网页批量签到：第 {line_num} 行格式无法解析，已跳过：{line[:40]}...")

        return sites

    def _parse_single_site_line(self, line: str, index: int) -> Optional[Dict[str, Any]]:
        # 1. 尝试 JSON 格式
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                if not data.get("enabled", True):
                    return None
                url = str(data.get("url") or data.get("site") or "").strip()
                username = str(data.get("username") or data.get("user") or "").strip()
                password = str(data.get("password") or data.get("pass") or "").strip()
                remark = str(data.get("remark") or data.get("name") or "").strip()
                proxy = str(data.get("proxy") or data.get("proxy_server") or "").strip()
                if url:
                    return self._build_site_item(index, url, username, password, remark, proxy)
            except Exception:
                pass

        # 2. 检查前置备注（如 "我的站点#https://..." 或 "我的站点|https://..."）
        remark = ""
        target_str = line

        for sep in ("#", "|"):
            if sep in target_str:
                prefix, remainder = target_str.split(sep, 1)
                remainder_clean = remainder.strip()
                if remainder_clean.startswith("http://") or remainder_clean.startswith("https://"):
                    remark = prefix.strip()
                    target_str = remainder_clean
                    break

        # 3. 检查后置备注（如 "https://...+user+pass#我的站点"）
        if not remark and "+" in target_str and "#" in target_str:
            creds_part, trailing_remark = target_str.split("#", 1)
            if "+" in creds_part:
                target_str = creds_part.strip()
                remark = trailing_remark.strip()

        # 4. 根据主分隔符拆分凭据
        if "+" in target_str:
            parts = [p.strip() for p in target_str.split("+") if p.strip()]
        elif "#" in target_str:
            parts = [p.strip() for p in target_str.split("#") if p.strip()]
        elif "|" in target_str:
            parts = [p.strip() for p in target_str.split("|") if p.strip()]
        elif "," in target_str:
            parts = [p.strip() for p in target_str.split(",") if p.strip()]
        else:
            parts = [p.strip() for p in target_str.split() if p.strip()]

        if not parts:
            return None

        url = parts[0]
        username = parts[1] if len(parts) >= 2 else ""
        password = parts[2] if len(parts) >= 3 else ""
        proxy = parts[3] if len(parts) >= 4 else ""

        if not url:
            return None

        return self._build_site_item(index, url, username, password, remark, proxy)

    def _build_site_item(
        self,
        index: int,
        url: str,
        username: str,
        password: str,
        remark: str = "",
        proxy: str = "",
    ) -> Dict[str, Any]:
        # 规范化 URL
        cleaned_url = url.strip()
        if not cleaned_url.startswith("http://") and not cleaned_url.startswith("https://"):
            cleaned_url = f"https://{cleaned_url}"

        parsed_url = urlparse(cleaned_url)
        hostname = parsed_url.hostname or cleaned_url

        if not remark:
            remark = f"站点{index} ({hostname})"

        return {
            "key": f"site:{index}:{hostname}",
            "index": index,
            "url": cleaned_url,
            "hostname": hostname,
            "username": username.strip(),
            "password": password.strip(),
            "remark": remark.strip(),
            "proxy": proxy.strip(),
        }

    def _get_site_proxy(self, site: Dict[str, Any]) -> Optional[str]:
        """获取特定站点使用的有效代理地址。"""
        # 1. 站点独立配置的代理优先
        site_proxy = (site.get("proxy") or "").strip()
        if site_proxy:
            if "://" not in site_proxy:
                site_proxy = f"http://{site_proxy}"
            return site_proxy

        # 2. 全局固定代理
        fixed_proxy = (self._proxy_server or "").strip()
        if fixed_proxy:
            if "://" not in fixed_proxy:
                fixed_proxy = f"http://{fixed_proxy}"
            return fixed_proxy

        # 3. 回退系统全局代理
        if self._use_proxy:
            proxy = getattr(settings, "PROXY", None)
            if isinstance(proxy, str) and proxy.strip():
                p = proxy.strip()
                return p if "://" in p else f"http://{p}"
            if isinstance(proxy, dict):
                p_val = proxy.get("http") or proxy.get("https") or proxy.get("all")
                if p_val and isinstance(p_val, str) and p_val.strip():
                    p = p_val.strip()
                    return p if "://" in p else f"http://{p}"

        return None

    def sign(self) -> List[Dict[str, Any]]:
        """执行所有站点的批量签到任务：依次执行，全部维护完后统一发送汇总通知。"""
        if not self._run_lock.acquire(blocking=False):
            logger.warning("网页批量签到：已有任务正在运行中，跳过本次触发")
            return []

        self._running = True
        try:
            sites = self._parse_sites()
            if not sites:
                logger.warning("网页批量签到：未配置有效站点，请在插件配置中添加站点账号")
                return []

            total = len(sites)
            logger.info(f"网页批量签到：开始依次处理 {total} 个维护站点...")
            results: List[Dict[str, Any]] = []

            for index, site in enumerate(sites):
                if index > 0:
                    delay = random.uniform(max(1, self._min_delay), max(2, self._max_delay))
                    logger.info(f"网页批量签到：等待随机间隔 {delay:.1f} 秒后处理下一个站点...")
                    time.sleep(delay)

                logger.info(f"网页批量签到：[{index + 1}/{total}] 正在执行 -> {site['remark']} ({site['url']})")
                try:
                    res = self._process_site(site)
                except Exception as exc:
                    logger.error(f"网页批量签到：处理站点 {site['remark']} 发生未捕获异常：{exc}", exc_info=True)
                    res = self._result(site, False, "失败", f"执行异常：{exc}")

                results.append(res)

            # 全部站点依次执行完毕后，统一保存历史记录并统一发送汇总通知
            self._save_results(results)
            self._notify_results(results)
            logger.info(f"网页批量签到：已全部完成 {total} 个站点，结果已统一推送通知")
            return results
        finally:
            self._running = False
            self._run_lock.release()

    def _process_site(self, site: Dict[str, Any]) -> Dict[str, Any]:
        """优先使用 Playwright 自动化执行登录与签到；环境不可用时回退 HTTP 基础模式。"""
        proxy = self._get_site_proxy(site)
        if proxy:
            logger.info(f"网页批量签到：{site['remark']} 使用代理服务器: {proxy}")
        else:
            logger.info(f"网页批量签到：{site['remark']} 使用网络直连访问")

        # 优先使用 Playwright
        browser_res = self._sign_via_playwright(site, proxy)
        if browser_res is not None:
            return browser_res

        # 降级备用：HTTP 请求模式
        logger.info(f"网页批量签到：{site['remark']} 使用 HTTP 模式进行探测与请求")
        return self._sign_via_requests(site, proxy)

    def _sign_via_playwright(self, site: Dict[str, Any], proxy: Optional[str]) -> Optional[Dict[str, Any]]:
        """Playwright 浏览器自动化：自动识别登录表单、填写账号密码并点击签到按钮。"""
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            logger.warning(f"网页批量签到：未安装 Playwright 或不可用：{exc}，切换为 HTTP 请求模式")
            return None

        launch_kwargs: Dict[str, Any] = {
            "headless": self._headless,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--ignore-certificate-errors",
            ],
        }
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}

        timeout_ms = self._page_timeout * 1000

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(**launch_kwargs)
                try:
                    context = browser.new_context(
                        user_agent=self.USER_AGENT,
                        ignore_https_errors=True,
                        viewport={"width": 1280, "height": 800},
                    )
                    page = context.new_page()

                    # 1. 打开签到/登录页面
                    logger.info(f"网页批量签到：{site['remark']} 正在打开页面 {site['url']} ...")
                    try:
                        page.goto(site["url"], wait_until="domcontentloaded", timeout=timeout_ms)
                    except Exception as goto_exc:
                        logger.warning(f"网页批量签到：{site['remark']} 页面初次加载超时，尝试继续：{goto_exc}")

                    page.wait_for_timeout(2000)

                    # 2. 检查是否已经处于签到状态
                    already_signed_text = self._check_already_signed_text(page)
                    if already_signed_text:
                        logger.info(f"网页批量签到：{site['remark']} 页面检测到已签到：{already_signed_text}")
                        return self._result(
                            site,
                            success=True,
                            status="已签到",
                            message=already_signed_text,
                            already_signed=True,
                            proxy=proxy,
                        )

                    # 3. 登录处理（如果页面包含账号/密码输入框，或者需要登录）
                    username = site["username"]
                    password = site["password"]
                    if username and password:
                        logged_in = self._perform_auto_login(page, username, password, site["remark"])
                        if logged_in is False:
                            # 明确检测到登录失败错误提示
                            err_msg = self._extract_error_message(page) or "登录失败（账号或密码错误）"
                            logger.error(f"网页批量签到：{site['remark']} {err_msg}")
                            return self._result(
                                site,
                                success=False,
                                status="失败",
                                message=err_msg,
                                retryable=False,
                                proxy=proxy,
                            )

                    # 4. 再次检查是否已签到
                    page.wait_for_timeout(2000)
                    already_signed_text = self._check_already_signed_text(page)
                    if already_signed_text:
                        logger.info(f"网页批量签到：{site['remark']} 登录后检测到今日已签到：{already_signed_text}")
                        return self._result(
                            site,
                            success=True,
                            status="已签到",
                            message=already_signed_text,
                            already_signed=True,
                            proxy=proxy,
                        )

                    # 5. 寻找并点击签到按钮
                    click_res = self._find_and_click_checkin(page, site["remark"])
                    if click_res:
                        return self._result(
                            site,
                            success=click_res.get("success", True),
                            status=click_res.get("status", "成功"),
                            message=click_res.get("message", "签到成功"),
                            already_signed=click_res.get("already_signed", False),
                            proxy=proxy,
                        )

                    # 6. 未找到显式签到按钮：若已成功访问/登录保活，作为成功处理
                    page_title = page.title() or site["hostname"]
                    logger.info(f"网页批量签到：{site['remark']} 页面已成功加载并完成保活访问 ({page_title})")
                    return self._result(
                        site,
                        success=True,
                        status="已访问",
                        message=f"登录并访问成功（{page_title}）",
                        already_signed=False,
                        proxy=proxy,
                    )
                finally:
                    browser.close()
        except Exception as exc:
            logger.error(f"网页批量签到：{site['remark']} Playwright 运行异常：{exc}", exc_info=True)
            return self._result(site, False, "失败", f"浏览器异常：{exc}", proxy=proxy)

    def _perform_auto_login(self, page, username: str, password: str, remark: str) -> Optional[bool]:
        """
        自动探测并填充登录表单。
        返回：
          True: 成功触发登录并完成提交
          False: 检测到明确的登录错误提示
          None: 页面无需登录或已登录
        """
        user_selectors = [
            'input[name="username"]',
            'input[name="user"]',
            'input[name="login"]',
            'input[name="email"]',
            'input[name="account"]',
            'input[id="username"]',
            'input[id="user"]',
            'input[id="login"]',
            'input[id="email"]',
            'input[id="account"]',
            'input[type="email"]',
            'input[autocomplete="username"]',
            'input[placeholder*="账号"]',
            'input[placeholder*="用户"]',
            'input[placeholder*="邮箱"]',
            'input[placeholder*="手机"]',
            'input[placeholder*="user" i]',
            'input[placeholder*="email" i]',
            'input[placeholder*="login" i]',
        ]

        pass_selectors = [
            'input[type="password"]',
            'input[name="password"]',
            'input[name="passwd"]',
            'input[name="pass"]',
            'input[name="pwd"]',
            'input[id="password"]',
            'input[id="passwd"]',
            'input[id="pass"]',
            'input[id="pwd"]',
            'input[placeholder*="密"]',
            'input[placeholder*="pass" i]',
        ]

        # 查找可见的密码输入框
        pass_input = None
        for sel in pass_selectors:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                pass_input = loc
                break

        # 如果没有直接找到密码框，检查是否有“登录”链接触发登录弹窗
        if not pass_input:
            login_link_selectors = [
                'a:has-text("登录")',
                'button:has-text("登录")',
                'a:has-text("登 录")',
                'button:has-text("登 录")',
                'a:has-text("Sign In")',
                'button:has-text("Sign In")',
                'a:has-text("Log In")',
                'button:has-text("Log In")',
                '.login-link',
                '#login-btn',
            ]
            for sel in login_link_selectors:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    try:
                        logger.info(f"网页批量签到：{remark} 点击页面登录入口：{sel}")
                        loc.click(timeout=3000)
                        page.wait_for_timeout(1500)
                        break
                    except Exception:
                        pass

            # 重新查找密码框
            for sel in pass_selectors:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    pass_input = loc
                    break

        if not pass_input:
            # 页面无密码输入框，可能已经处于登录状态或不需要密码登录
            return None

        # 查找用户名输入框
        user_input = None
        for sel in user_selectors:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                user_input = loc
                break

        if not user_input:
            # 降级：查找在密码框之前的第一个可见 text 框
            text_loc = page.locator('input[type="text"]').first
            if text_loc.count() > 0 and text_loc.is_visible():
                user_input = text_loc

        if not user_input:
            logger.warning(f"网页批量签到：{remark} 发现密码框但未定位到用户名输入框")
            return None

        logger.info(f"网页批量签到：{remark} 定位到登录表单，正在输入账号密码...")
        try:
            user_input.fill("")
            user_input.fill(username)
            time.sleep(0.2)
            pass_input.fill("")
            pass_input.fill(password)
            time.sleep(0.3)
        except Exception as fill_exc:
            logger.warning(f"网页批量签到：{remark} 填写表单异常：{fill_exc}")

        # 查找登录提交按钮
        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("登录")',
            'button:has-text("登 录")',
            'button:has-text("Sign In")',
            'button:has-text("Log In")',
            'button:has-text("Login")',
            'a:has-text("立即登录")',
            'a:has-text("登录")',
            '.login-button',
            '.btn-login',
            '.btn-submit',
            'button.el-button--primary',
        ]

        submitted = False
        for sel in submit_selectors:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                try:
                    logger.info(f"网页批量签到：{remark} 点击登录提交按钮：{sel}")
                    loc.click(timeout=5000)
                    submitted = True
                    break
                except Exception:
                    pass

        if not submitted:
            # 尝试回车提交
            try:
                logger.info(f"网页批量签到：{remark} 未找到显式提交按钮，在密码框按下 Enter 回车提交")
                pass_input.press("Enter")
                submitted = True
            except Exception:
                pass

        page.wait_for_timeout(3000)

        # 检查是否有明确错误提示
        err_msg = self._extract_error_message(page)
        if err_msg:
            return False

        return True

    def _check_already_signed_text(self, page) -> Optional[str]:
        """检查页面是否包含已签到关键字。"""
        keywords = [
            "今日已签到",
            "今天已经签到",
            "已经签到",
            "已签到",
            "今日已打卡",
            "已经打卡",
            "已打卡",
            "今日已完成",
            "今日已选择",
            "Already checked in",
            "Checked in today",
        ]
        for kw in keywords:
            loc = page.locator(f':text-is("{kw}"), button:has-text("{kw}"), span:has-text("{kw}")').first
            if loc.count() > 0 and loc.is_visible():
                return f"今日已签到（检测到「{kw}」）"

        # 检查全页文本
        try:
            content = page.content()
            for kw in ("今日已签到", "今天已经签到", "您今日已签到"):
                if kw in content:
                    return f"今日已签到（页面包含「{kw}」）"
        except Exception:
            pass

        return None

    def _find_and_click_checkin(self, page, remark: str) -> Optional[Dict[str, Any]]:
        """寻找并点击签到按钮。"""
        checkin_selectors = [
            'button:has-text("立即签到")',
            'a:has-text("立即签到")',
            'button:has-text("今日签到")',
            'a:has-text("今日签到")',
            'button:has-text("每日签到")',
            'a:has-text("每日签到")',
            'button:has-text("签到")',
            'a:has-text("签到")',
            'button:has-text("打卡")',
            'a:has-text("打卡")',
            'button:has-text("Check in")',
            'a:has-text("Check in")',
            'button:has-text("Checkin")',
            'a:has-text("Checkin")',
            'button[id*="checkin" i]',
            'a[id*="checkin" i]',
            'button[class*="checkin" i]',
            'a[class*="checkin" i]',
            'button[id*="qiandao" i]',
            'a[id*="qiandao" i]',
            '#checkin',
            '.btn-checkin',
        ]

        for sel in checkin_selectors:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                btn_text = (loc.inner_text() or "").strip()
                if any(k in btn_text for k in ("已签到", "今日已", "已经签到")):
                    logger.info(f"网页批量签到：{remark} 签到按钮当前显示为：{btn_text}")
                    return {"success": True, "status": "已签到", "message": btn_text, "already_signed": True}

                logger.info(f"网页批量签到：{remark} 定位到签到按钮「{btn_text or sel}」，正在点击...")
                try:
                    loc.click(timeout=8000)
                    page.wait_for_timeout(3000)

                    # 尝试捕获点击后的反馈消息（如 toast、弹窗提示）
                    feedback_msg = self._extract_feedback_message(page)
                    if feedback_msg:
                        logger.info(f"网页批量签到：{remark} 签到反馈：{feedback_msg}")
                        return {"success": True, "status": "成功", "message": feedback_msg}

                    # 检查按钮文本是否变为已签到
                    try:
                        new_text = (loc.inner_text() or "").strip()
                        if "已" in new_text or "签到" in new_text:
                            return {"success": True, "status": "成功", "message": f"签到完成（{new_text}）"}
                    except Exception:
                        pass

                    return {"success": True, "status": "成功", "message": "已点击签到按钮"}
                except Exception as click_err:
                    logger.warning(f"网页批量签到：{remark} 点击签到按钮失败：{click_err}")

        return None

    def _extract_feedback_message(self, page) -> Optional[str]:
        """尝试从页面弹窗或 Toast 提取提示信息。"""
        toast_selectors = [
            ".el-message__content",
            ".toast-message",
            ".ant-message-notice-content",
            ".swal2-html-container",
            ".swal2-title",
            ".layui-layer-content",
            ".v-alert",
            ".alert-success",
            ".alert-info",
            ".notification-message",
            ".notice-message",
            'div[role="alert"]',
        ]
        for sel in toast_selectors:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                txt = (loc.inner_text() or "").strip()
                if txt and len(txt) < 150:
                    return txt
        return None

    def _extract_error_message(self, page) -> Optional[str]:
        """尝试从页面提取明确的错误提示。"""
        err_keywords = [
            "密码错误",
            "用户名不存在",
            "账号或密码错误",
            "密码不正确",
            "验证码错误",
            "账号不存在",
            "登录失败",
            "Invalid username",
            "Invalid password",
            "Incorrect password",
        ]
        for kw in err_keywords:
            loc = page.locator(f':text("{kw}")').first
            if loc.count() > 0 and loc.is_visible():
                txt = (loc.inner_text() or "").strip()
                return txt or kw
        return None

    def _sign_via_requests(self, site: Dict[str, Any], proxy: Optional[str]) -> Dict[str, Any]:
        """HTTP 请求模式（Playwright 不可用时的备选与基础保活）。"""
        proxies = {"http": proxy, "https": proxy} if proxy else None
        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        session = requests.Session()
        session.headers.update(headers)
        if proxies:
            session.proxies.update(proxies)

        try:
            resp = session.get(site["url"], timeout=self._page_timeout, verify=False)
            if resp.status_code == 200:
                html = resp.text
                if any(kw in html for kw in ("今日已签到", "今天已经签到", "已签到")):
                    return self._result(site, True, "已签到", "今日已签到(页面检测)", already_signed=True, proxy=proxy)
                return self._result(
                    site,
                    True,
                    "已访问",
                    "HTTP 请求成功（已访问页面保活）",
                    already_signed=False,
                    proxy=proxy,
                )
            return self._result(
                site,
                False,
                "失败",
                f"HTTP 状态码异常：{resp.status_code}",
                proxy=proxy,
            )
        except Exception as exc:
            return self._result(site, False, "失败", f"网络请求失败：{exc}", proxy=proxy)
        finally:
            session.close()

    def _result(
        self,
        site: Dict[str, Any],
        success: bool,
        status: str,
        message: str,
        already_signed: bool = False,
        retryable: bool = True,
        proxy: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "key": site.get("key", site["url"]),
            "index": site.get("index", 1),
            "remark": site.get("remark") or site.get("hostname") or site["url"],
            "url": site.get("url", ""),
            "username": site.get("username", ""),
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "success": success,
            "already_signed": already_signed,
            "status": status,
            "message": str(message or "").strip(),
            "proxy": proxy or "直连",
            "retryable": retryable,
        }

    def _save_results(self, results: List[Dict[str, Any]]):
        history = self.get_data("sign_history") or []
        cutoff = (datetime.now() - timedelta(days=self._history_days)).strftime("%Y-%m-%d %H:%M:%S")
        history = [item for item in history if item.get("date", "") >= cutoff]
        for res in results:
            history.insert(0, {
                "date": res["date"],
                "remark": res["remark"],
                "url": res["url"],
                "username": res["username"],
                "status": res["status"],
                "success": res["success"],
                "message": res["message"],
                "proxy": res["proxy"],
            })
        self.save_data("sign_history", history[:500])

    def clear_sign_history(self):
        self.save_data("sign_history", [])
        logger.info("网页批量签到：已清空历史记录")

    def _notify_results(self, results: List[Dict[str, Any]]):
        """全部维护站点执行完毕后，统一发送汇总通知。"""
        if not self._notify or not results:
            return

        success_count = sum(1 for r in results if r.get("success"))
        fail_count = len(results) - success_count
        total = len(results)

        title = f"网页批量签到：{success_count} 成功 / {fail_count} 失败（共 {total} 个站点）"

        lines = []
        for r in results:
            icon = "✅" if r.get("success") else "❌"
            if r.get("already_signed"):
                icon = "ℹ️"

            remark = r.get("remark")
            user = r.get("username")
            user_label = f"({user})" if user else ""
            status = r.get("status")
            msg = r.get("message")
            proxy_label = f" [{r.get('proxy')}]" if r.get("proxy") and r.get("proxy") != "直连" else ""

            lines.append(f"{icon} 【{remark}】{user_label}：{status} - {msg}{proxy_label}")

        content = "\n".join(lines)
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=title,
                text=content,
            )
            logger.info("网页批量签到：已成功发送统一汇总通知")
        except Exception as exc:
            logger.error(f"网页批量签到：发送统一汇总通知失败：{exc}")

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [{
            "id": "OpenPage",
            "name": "网页批量登录签到",
            "trigger": CronTrigger.from_crontab(self._cron, timezone=settings.TZ),
            "func": self.sign,
            "kwargs": {},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        example = (
            "# 格式A：签到地址+用户名+密码（支持换行维护多个站点，依次执行）\n"
            "https://example.com/login+myusername+mypassword\n\n"
            "# 方式2：在前面增加自定义备注（备注#签到地址+用户名+密码）\n"
            "我的论坛#https://bbs.example.com/login+user2+pass2\n\n"
            "# 方式3：支持 # 或 | 分隔\n"
            "https://site3.com/signin#user3#pass3\n\n"
            "# 方式4：针对某个站点独立指定代理（第4项可选）\n"
            "https://site4.com/login+user4+pass4+http://127.0.0.1:7890"
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
                        self._col_switch("use_proxy", "使用网络代理"),
                        self._col_switch("headless", "后台静默运行(无头)"),
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
                                "component": "VTextField",
                                "props": {
                                    "model": "proxy_server",
                                    "label": "固定代理服务器 (全局)",
                                    "placeholder": "例如: http://127.0.0.1:7890 或 socks5://127.0.0.1:1080",
                                },
                            }],
                        },
                        self._col_field("cron", "签到周期", component="VCronField", cols=12, sm=6),
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
                                "model": "sites_text",
                                "label": "批量站点列表（格式：签到地址+用户名+密码，多个站点回车换行）",
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
                        self._col_field("min_delay", "站点间最小随机延迟(秒)", field_type="number", cols=12, sm=4),
                        self._col_field("max_delay", "站点间最大随机延迟(秒)", field_type="number", cols=12, sm=4),
                        self._col_field("page_timeout", "单站点超时时间(秒)", field_type="number", cols=12, sm=4),
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        self._col_field("history_days", "历史保留天数", field_type="number", cols=12, sm=6),
                    ],
                },
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": (
                            "支持批量维护多个站点进行网页自动登录与签到：\n"
                            "1. 格式：签到地址+用户名+密码（多个站点每行一个，依次执行）。\n"
                            "2. 固定代理维护：在「固定代理服务器」填写（如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080），开启后直接走该固定代理。\n"
                            "3. 顺序执行与统一通知：维护多个站点时，将按设置依次顺序执行；全部站点执行完成后统一汇总发送通知。"
                        ),
                    },
                },
            ],
        }], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "use_proxy": False,
            "headless": True,
            "proxy_server": "",
            "sites_text": "",
            "cron": "30 8 * * *",
            "min_delay": 3,
            "max_delay": 8,
            "page_timeout": 35,
            "history_days": 30,
            "clear_history": False,
        }

    @staticmethod
    def _col_switch(model: str, label: str) -> Dict[str, Any]:
        return {
            "component": "VCol",
            "props": {"cols": 12, "sm": 6, "md": 4},
            "content": [{"component": "VSwitch", "props": {"model": model, "label": label}}],
        }

    @staticmethod
    def _col_field(
        model: str,
        label: str,
        component: str = "VTextField",
        field_type: Optional[str] = None,
        cols: int = 12,
        sm: int = 6,
        md: Optional[int] = None,
    ) -> Dict[str, Any]:
        props: Dict[str, Any] = {"model": model, "label": label}
        if field_type:
            props["type"] = field_type
        col_props: Dict[str, Any] = {"cols": cols, "sm": sm}
        if md:
            col_props["md"] = md
        return {
            "component": "VCol",
            "props": col_props,
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
                    {"component": "td", "text": item.get("url", "-")},
                    {"component": "td", "text": item.get("username", "-")},
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
                    {"component": "td", "text": item.get("message", "-")},
                    {"component": "td", "text": str(item.get("proxy", "直连"))},
                ],
            })
        return [{
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": [
                {"component": "VCardTitle", "text": "网页批量签到历史记录"},
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
                                        {"component": "th", "text": "站点备注"},
                                        {"component": "th", "text": "网址"},
                                        {"component": "th", "text": "用户名"},
                                        {"component": "th", "text": "状态"},
                                        {"component": "th", "text": "执行信息"},
                                        {"component": "th", "text": "代理"},
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
            logger.error(f"网页批量签到：停止调度失败：{exc}")

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []


openpage = OpenPage
