"""NodeSeek / DeepFlood multi-account daily sign-in plugin for MoviePilot."""

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


class NodeSeekSignBatch(_PluginBase):
    plugin_name = "NodeSeek / DeepFlood 多账号签到"
    plugin_desc = "支持 NodeSeek、DeepFlood 多账号每日签到，每个账号独立配置备注、站点和 Cookie。"
    plugin_icon = "https://raw.githubusercontent.com/madrays/MoviePilot-Plugins/main/icons/nodeseeksign.png"
    plugin_version = "3.1.1"
    plugin_author = "madrays / kbmgr"
    author_url = "https://github.com/madrays"
    plugin_config_prefix = "nodeseeksignbatch_"
    plugin_order = 1
    auth_level = 2

    SITE_URLS = {
        "nodeseek": "https://www.nodeseek.com",
        "deepflood": "https://www.deepflood.com",
    }
    SITE_NAMES = {
        "nodeseek": "NodeSeek",
        "deepflood": "DeepFlood",
    }
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    )

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "0 8 * * *"
    _accounts_json = ""
    _random_choice = True
    _use_proxy = True
    _verify_ssl = False
    _max_retries = 3
    _min_delay = 5
    _max_delay = 12
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
        self._cron = "0 8 * * *"
        self._accounts_json = ""
        self._random_choice = True
        self._use_proxy = True
        self._verify_ssl = False
        self._max_retries = 3
        self._min_delay = 5
        self._max_delay = 12
        self._history_days = 30
        self._clear_history = False

        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._onlyonce = bool(config.get("onlyonce"))
            self._cron = str(config.get("cron") or self._cron).strip()
            self._accounts_json = config.get("accounts_json") or config.get("accounts") or ""
            self._random_choice = bool(config.get("random_choice", True))
            self._use_proxy = bool(config.get("use_proxy", True))
            self._verify_ssl = bool(config.get("verify_ssl", False))
            self._max_retries = self._as_int(config.get("max_retries"), 3, 0, 10)
            self._min_delay = self._as_int(config.get("min_delay"), 5, 0, 300)
            self._max_delay = self._as_int(config.get("max_delay"), 12, 0, 300)
            self._history_days = self._as_int(config.get("history_days"), 30, 1, 365)
            self._clear_history = bool(config.get("clear_history"))

            # Seamless migration from the original single-account configuration.
            if not self._accounts_json and config.get("cookie"):
                legacy = {
                    "remark": "NodeSeek 账号",
                    "site": "nodeseek",
                    "cookie": str(config.get("cookie") or "").strip(),
                    "member_id": str(config.get("member_id") or "").strip(),
                    "enabled": True,
                }
                self._accounts_json = json.dumps([legacy], ensure_ascii=False, indent=2)
                logger.info("NodeSeek签到：已将旧单账号配置迁移为账号列表")

        if self._max_delay < self._min_delay:
            self._min_delay, self._max_delay = self._max_delay, self._min_delay

        if self._clear_history:
            self.clear_sign_history()
            self._clear_history = False
            self._update_config()

        accounts = self._parse_accounts()
        logger.info(
            f"NodeSeek签到 v{self.plugin_version}：有效账号 {len(accounts)} 个，"
            f"站点={','.join(sorted({a['site'] for a in accounts})) or '无'}"
        )

        if self._onlyonce:
            self._onlyonce = False
            self._update_config()
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sign,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="NodeSeek/DeepFlood多账号签到(单次)",
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
            "accounts_json": self._accounts_json,
            "random_choice": self._random_choice,
            "use_proxy": self._use_proxy,
            "verify_ssl": self._verify_ssl,
            "max_retries": self._max_retries,
            "min_delay": self._min_delay,
            "max_delay": self._max_delay,
            "history_days": self._history_days,
            "clear_history": self._clear_history,
        })

    def _parse_accounts(self) -> List[Dict[str, Any]]:
        raw = self._accounts_json
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = [raw] if (raw.get("cookie") or raw.get("site")) else raw.get("accounts") or raw.get("users") or []
        else:
            text = str(raw or "").strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    items = parsed
                elif isinstance(parsed, dict) and (parsed.get("cookie") or parsed.get("site")):
                    items = [parsed]
                elif isinstance(parsed, dict):
                    items = parsed.get("accounts") or parsed.get("users") or []
                else:
                    items = []
            except (json.JSONDecodeError, AttributeError):
                items = []
                for line_number, line in enumerate(text.splitlines(), 1):
                    line = line.strip().rstrip(",")
                    if not line or line.startswith("#"):
                        continue
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        logger.error(f"NodeSeek签到：账号列表第 {line_number} 行格式错误：{exc}")

        accounts = []
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict) or item.get("enabled", True) is False:
                continue
            cookie = str(item.get("cookie") or "").strip()
            if not cookie:
                logger.warning(f"NodeSeek签到：第 {index} 个账号未填写 Cookie，已跳过")
                continue
            site = self._normalize_site(item.get("site") or item.get("url"))
            if not site:
                logger.error(f"NodeSeek签到：第 {index} 个账号站点无效，仅支持 nodeseek/deepflood")
                continue
            remark = str(item.get("remark") or item.get("name") or "").strip()
            if not remark:
                remark = f"{self.SITE_NAMES[site]}账号{index}"
            accounts.append({
                "key": f"{site}:{index}",
                "remark": remark,
                "site": site,
                "site_name": self.SITE_NAMES[site],
                "base_url": self.SITE_URLS[site],
                "cookie": cookie,
                "member_id": str(item.get("member_id") or item.get("memberId") or "").strip(),
            })
        return accounts

    @classmethod
    def _normalize_site(cls, value: Any) -> Optional[str]:
        site = str(value or "nodeseek").strip().lower()
        if site in cls.SITE_URLS:
            return site
        if "://" not in site:
            site = "https://" + site
        hostname = (urlparse(site).hostname or "").lower()
        if hostname in ("nodeseek.com", "www.nodeseek.com"):
            return "nodeseek"
        if hostname in ("deepflood.com", "www.deepflood.com"):
            return "deepflood"
        return None

    def sign(self) -> List[Dict[str, Any]]:
        if not self._run_lock.acquire(blocking=False):
            logger.warning("NodeSeek签到：已有任务正在运行，跳过本次触发")
            return []
        self._running = True
        try:
            accounts = self._parse_accounts()
            if not accounts:
                logger.warning("NodeSeek签到：未配置有效账号")
                return []
            logger.info(f"NodeSeek签到：开始处理 {len(accounts)} 个账号")
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
        result = None
        for attempt in range(self._max_retries + 1):
            if attempt:
                delay = random.uniform(max(1, self._min_delay), max(2, self._max_delay))
                logger.info(
                    f"NodeSeek签到：{account['remark']} 第 {attempt}/{self._max_retries} 次重试，"
                    f"等待 {delay:.1f} 秒"
                )
                time.sleep(delay)
            try:
                result = self._sign_account(account)
            except Exception as exc:
                logger.error(f"NodeSeek签到：{account['remark']} 请求异常：{exc}", exc_info=True)
                result = self._result(account, False, f"请求异常：{exc}")
            if result.get("success"):
                return result
        return result or self._result(account, False, "未知错误")

    def _sign_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"NodeSeek签到：正在签到 {account['remark']} ({account['site_name']})")
        base_url = account["base_url"]
        headers = self._headers(account, referer="/board")
        random_param = "true" if self._random_choice else "false"
        response = self._request(
            "post",
            f"{base_url}/api/attendance?random={random_param}",
            headers=headers,
            data=b"",
        )
        api_result = self._parse_sign_response(response)

        attendance = self._fetch_attendance(account)
        if not api_result["success"] and self._attendance_is_today(attendance):
            api_result.update({
                "success": True,
                "already_signed": True,
                "message": "今日已签到（签到记录确认）",
            })

        user_info = self._fetch_user_info(account) if account.get("member_id") else {}
        gain = attendance.get("gain") or api_result.get("gain")
        result = self._result(
            account,
            api_result["success"],
            api_result.get("message") or ("签到成功" if api_result["success"] else "签到失败"),
            already_signed=api_result.get("already_signed", False),
            gain=gain,
            rank=attendance.get("rank"),
            total_signers=attendance.get("total_signers"),
            user_name=user_info.get("member_name"),
        )
        logger.info(
            f"NodeSeek签到：{account['remark']} "
            f"{'成功' if result['success'] else '失败'}：{result['message']}"
        )
        return result

    def _headers(self, account: Dict[str, Any], referer: str = "/board") -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": account["base_url"],
            "Referer": account["base_url"] + referer,
            "User-Agent": self.USER_AGENT,
            "Cookie": account["cookie"],
        }

    @staticmethod
    def _parse_sign_response(response: Any) -> Dict[str, Any]:
        result = {"success": False, "already_signed": False, "message": ""}
        try:
            data = response.json()
            message = str(data.get("message") or "")
            result["message"] = message or f"HTTP {response.status_code}"
            if data.get("success") is True:
                result.update(success=True, gain=data.get("gain"))
            elif "已完成签到" in message or "已经签到" in message or "已签到" in message:
                result.update(success=True, already_signed=True)
            elif "鸡腿" in message or ("签到" in message and ("成功" in message or "完成" in message)):
                result["success"] = True
            elif message == "USER NOT FOUND" or data.get("status") == 404:
                result["message"] = "Cookie 已失效，请更新"
            return result
        except Exception:
            text = str(getattr(response, "text", "") or "")
            if "已完成签到" in text or "已经签到" in text or "已签到" in text:
                result.update(success=True, already_signed=True, message="今日已签到")
            elif any(keyword in text for keyword in ("签到成功", "签到完成", "鸡腿")):
                result.update(success=True, message="签到成功")
            elif any(keyword in text for keyword in ("登录", "注册", "陌生人")):
                result["message"] = "未登录或 Cookie 已失效"
            else:
                result["message"] = f"非 JSON 响应 (HTTP {getattr(response, 'status_code', '-')})"
            return result

    def _fetch_attendance(self, account: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = self._request(
                "get",
                f"{account['base_url']}/api/attendance/board?page=1",
                headers=self._headers(account),
            )
            data = response.json()
            direct_record = data.get("record") if isinstance(data, dict) else None
            if isinstance(direct_record, dict) and direct_record:
                record = dict(direct_record)
                record["rank"] = data.get("order") or record.get("rank")
                record["total_signers"] = data.get("total") or record.get("total_signers")
                return record
            rows = data.get("data") or data.get("records") or []
            if isinstance(rows, dict):
                rows = rows.get("data") or rows.get("records") or rows.get("list") or []
            if not isinstance(rows, list):
                return {}
            member_id = account.get("member_id")
            matched = None
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                row_member_id = str(row.get("member_id") or row.get("memberId") or row.get("id") or "")
                if member_id and row_member_id == member_id:
                    matched = (index, row)
                    break
            if matched is None and rows:
                matched = (0, rows[0])
            if matched is None:
                return {}
            index, row = matched
            record = {
                "gain": row.get("gain") or row.get("reward") or row.get("amount"),
                "rank": row.get("rank") or index + 1,
                "total_signers": data.get("total") or data.get("count") or len(rows),
                "created_at": row.get("created_at") or row.get("createdAt") or row.get("time"),
            }
            return record
        except Exception as exc:
            logger.warning(f"NodeSeek签到：{account['remark']} 获取签到记录失败：{exc}")
            return {}

    def _fetch_user_info(self, account: Dict[str, Any]) -> Dict[str, Any]:
        member_id = account["member_id"]
        try:
            response = self._request(
                "get",
                f"{account['base_url']}/api/account/getInfo/{member_id}?readme=1",
                headers=self._headers(account, referer=f"/space/{member_id}"),
            )
            data = response.json()
            info = (data.get("detail") or data.get("data")) if isinstance(data, dict) else {}
            return info if isinstance(info, dict) else {}
        except Exception as exc:
            logger.warning(f"NodeSeek签到：{account['remark']} 获取用户信息失败：{exc}")
            return {}

    @staticmethod
    def _attendance_is_today(record: Dict[str, Any]) -> bool:
        value = record.get("created_at") if record else None
        if not value:
            return False
        try:
            created = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
            return created.date() == now.date()
        except (TypeError, ValueError):
            return False

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
            "remark": account["remark"],
            "site": account["site"],
            "site_name": account["site_name"],
            "success": bool(success),
            "status": "已签到" if already_signed else ("签到成功" if success else "签到失败"),
            "message": str(message or ""),
        }
        result.update({key: value for key, value in extra.items() if value not in (None, "")})
        return result

    def _request(self, method: str, url: str, headers: Dict[str, str], data: Any = None):
        proxies = self._get_proxies()
        verify = self._verify_ssl
        errors = []

        if HAS_CLOUDSCRAPER:
            try:
                scraper = cloudscraper.create_scraper(browser="chrome")
                response = scraper.request(
                    method, url, headers=headers, data=data, proxies=proxies,
                    timeout=30, verify=verify,
                )
                if self._usable_response(response):
                    return response
                errors.append(f"cloudscraper HTTP {response.status_code}")
            except Exception as exc:
                errors.append(f"cloudscraper: {exc}")

        if HAS_CURL_CFFI:
            try:
                response = curl_requests.request(
                    method, url, headers=headers, data=data, proxies=proxies,
                    timeout=30, verify=verify, impersonate="chrome110",
                )
                if self._usable_response(response):
                    return response
                errors.append(f"curl_cffi HTTP {response.status_code}")
            except Exception as exc:
                errors.append(f"curl_cffi: {exc}")

        try:
            return requests.request(
                method, url, headers=headers, data=data, proxies=proxies,
                timeout=30, verify=verify,
            )
        except Exception as exc:
            errors.append(f"requests: {exc}")
            raise RuntimeError("；".join(errors)) from exc

    @staticmethod
    def _usable_response(response: Any) -> bool:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        return response.status_code not in (403, 429) and "text/html" not in content_type

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
        logger.info(f"NodeSeek签到：账号间随机等待 {delay:.1f} 秒")
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
            reward = f"，奖励 {item['gain']}" if item.get("gain") is not None else ""
            lines.append(
                f"[{item['site_name']}] {item['remark']}：{item['status']}{reward}"
                f"；{item.get('message') or '-'}"
            )
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title="【NodeSeek / DeepFlood 多账号签到】",
            text="\n".join(lines),
        )

    def clear_sign_history(self):
        self.save_data("sign_history", [])
        self.save_data("last_results", [])
        logger.info("NodeSeek签到：历史记录已清空")

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "nodeseeksignbatch",
                "name": "NodeSeek/DeepFlood多账号签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sign,
                "kwargs": {},
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        example = (
            '{"remark":"NodeSeek主号","site":"nodeseek","cookie":"session=...",'
            '"member_id":"123","enabled":true}\n'
            '{"remark":"DeepFlood主号","site":"deepflood","cookie":"session=...",'
            '"member_id":"456","enabled":true}'
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
                        self._col_switch("random_choice", "随机奖励"),
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
                                "model": "accounts_json",
                                "label": "账号列表（每行一个账号，账号资料互相独立）",
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
                            "账号列表支持两种格式：每行一个 JSON 对象，或完整 JSON 数组。每个账号单独填写 "
                            "remark（备注）、site（nodeseek/deepflood 或完整网址）、cookie、member_id（可选）和 "
                            "enabled（可选）。Cookie 仅用于对应账号和对应站点。"
                        ),
                    },
                },
            ],
        }], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "0 8 * * *",
            "accounts_json": "",
            "random_choice": True,
            "use_proxy": True,
            "verify_ssl": False,
            "max_retries": 3,
            "min_delay": 5,
            "max_delay": 12,
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
                    {"component": "td", "text": item.get("remark", "旧账号")},
                    {"component": "td", "text": item.get("site_name") or item.get("site", "NodeSeek")},
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
                    {"component": "td", "text": str(item.get("gain", "-"))},
                    {"component": "td", "text": item.get("message", "-")},
                ],
            })
        return [{
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": [
                {"component": "VCardTitle", "text": "NodeSeek / DeepFlood 多账号签到历史"},
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
                                        {"component": "th", "text": "账号备注"},
                                        {"component": "th", "text": "站点"},
                                        {"component": "th", "text": "状态"},
                                        {"component": "th", "text": "奖励"},
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
            logger.error(f"NodeSeek签到：停止调度失败：{exc}")

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []
