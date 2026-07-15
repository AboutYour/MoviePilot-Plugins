"""
Katabump 自动续期 MoviePilot 插件。

把桌面版「一键签到」/ Node 版 action_renew 的逻辑移植为 MoviePilot 插件：
登录（等 Cloudflare Turnstile 令牌就绪再提交）→ 进入服务器详情(See) → Renew →
解 ALTCHA 验证码 → 确认续期 → 截图 → 通过 MoviePilot 通知推送结果。

依赖 MoviePilot 容器内置的 playwright；chromium 二进制首次运行时自动补装。
"""
import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
except Exception:  # pragma: no cover
    BackgroundScheduler = None
    CronTrigger = None


class KatabumpRenew(_PluginBase):
    # ===== 插件元数据 =====
    plugin_name = "Katabump自动续期"
    plugin_desc = "定时登录 Katabump 免费面板，自动为服务器续期（See→Renew→过验证码→确认），结果推送到通知。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/refresh.png"
    plugin_version = "1.5.1"
    plugin_author = "kbmgr"
    author_url = "https://github.com/"
    plugin_config_prefix = "katabumprenew_"
    plugin_order = 30
    auth_level = 1

    # ===== 运行时配置 =====
    _enabled = False
    _onlyonce = False
    _notify = True
    _cron = "30 3 */3 * *"
    _login_url = "https://dashboard.katabump.com/auth/login"
    _accounts_json = ""
    _chrome_path = ""            # 可选：手动指定 Chrome/Chromium 路径或 CDP 地址
    _browser_mode = "auto"         # auto/playwright/system_chrome/system_edge/cdp
    _turnstile_wait = 120        # 登录等待 Turnstile 令牌秒数
    _renew_attempts = 3          # 续期重试次数
    _headless = False            # Turnstile/ALTCHA 需要“有头”，默认 False（配合 xvfb）
    _proxy_server = ""           # 可选：代理服务器（非住宅网络运行时，用住宅代理过 Turnstile）
    # auto=CF 直连探测后必要时回退环境代理；direct=强制直连；system=环境代理；custom=仅用下方代理
    _proxy_mode = "auto"

    _scheduler: Optional[BackgroundScheduler] = None
    _running = False

    def init_plugin(self, config: dict = None):
        # 先重置为默认
        self._enabled = False
        self._onlyonce = False
        self._notify = True
        self._cron = "30 3 */3 * *"
        self._login_url = "https://dashboard.katabump.com/auth/login"
        self._accounts_json = ""
        self._chrome_path = ""
        self._browser_mode = "auto"
        self._turnstile_wait = 120
        self._renew_attempts = 3
        self._headless = False
        self._proxy_server = ""
        self._proxy_mode = "auto"

        if config:
            self._enabled = bool(config.get("enabled"))
            self._onlyonce = bool(config.get("onlyonce"))
            self._notify = bool(config.get("notify", True))
            self._cron = (config.get("cron") or "30 3 */3 * *").strip()
            self._login_url = (config.get("login_url") or self._login_url).strip()
            self._accounts_json = config.get("accounts_json") or ""
            self._chrome_path = (config.get("chrome_path") or "").strip()
            bm = (config.get("browser_mode") or "auto").strip().lower()
            self._browser_mode = bm if bm in ("auto", "playwright", "system_chrome", "system_edge", "cdp") else "auto"
            self._turnstile_wait = int(config.get("turnstile_wait") or 120)
            self._renew_attempts = int(config.get("renew_attempts") or 3)
            self._headless = bool(config.get("headless"))
            self._proxy_server = (config.get("proxy_server") or "").strip()
            mode = (config.get("proxy_mode") or "auto").strip().lower()
            self._proxy_mode = mode if mode in ("auto", "direct", "system", "custom") else "auto"

        # 停掉旧调度
        self.stop_service()

        # onlyonce：立即跑一次
        if self._onlyonce:
            logger.info(f"Katabump 续期 v{self.plugin_version}：立即运行一次")
            self._onlyonce = False
            self.__update_config()
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.run_renew,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="Katabump续期(单次)",
            )
            self._scheduler.start()

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "notify": self._notify,
            "cron": self._cron,
            "login_url": self._login_url,
            "accounts_json": self._accounts_json,
            "chrome_path": self._chrome_path,
            "browser_mode": self._browser_mode,
            "turnstile_wait": self._turnstile_wait,
            "renew_attempts": self._renew_attempts,
            "headless": self._headless,
            "proxy_server": self._proxy_server,
            "proxy_mode": self._proxy_mode,
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "KatabumpRenew",
                "name": "Katabump自动续期",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.run_renew,
                "kwargs": {},
            }]
        return []

    # ============================================================
    # 配置界面
    # ============================================================
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify", "label": "发送通知"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即运行一次"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "cron",
                                        "label": "执行周期 (cron)",
                                        "placeholder": "30 3 */3 * *（每3天凌晨3:30）",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "login_url",
                                        "label": "登录地址",
                                        "placeholder": "https://dashboard.katabump.com/auth/login",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "accounts_json",
                                        "label": "账号列表 (JSON)",
                                        "rows": 6,
                                        "placeholder": '[{"name":"主账号","purpose":"主服务器","username":"a@x.com","password":"pwd1"},\n {"name":"备用账号","username":"b@x.com","password":"pwd2"}]',
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {"model": "turnstile_wait", "label": "Turnstile 等待(秒)", "type": "number"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {"model": "renew_attempts", "label": "续期重试次数", "type": "number"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "headless", "label": "无头模式(不推荐)"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "chrome_path",
                                        "label": "Chrome 路径 / CDP 地址(可选)",
                                        "placeholder": "留空自动；可填 /usr/bin/google-chrome 或 http://NAS_IP:9222",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "browser_mode",
                                        "label": "浏览器模式",
                                        "items": [
                                            {"title": "自动(推荐)", "value": "auto"},
                                            {"title": "仅 Playwright Chromium", "value": "playwright"},
                                            {"title": "优先系统 Chrome(channel=chrome)", "value": "system_chrome"},
                                            {"title": "优先系统 Edge(channel=msedge)", "value": "system_edge"},
                                            {"title": "外部 Chrome CDP", "value": "cdp"},
                                        ],
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "proxy_mode",
                                        "label": "代理模式",
                                        "items": [
                                            {"title": "自动(推荐：CF 不通则回退环境代理)", "value": "auto"},
                                            {"title": "强制直连(忽略环境代理)", "value": "direct"},
                                            {"title": "使用系统环境代理", "value": "system"},
                                            {"title": "仅用下方填写的代理", "value": "custom"},
                                        ],
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "proxy_server",
                                        "label": "代理服务器(可选)",
                                        "placeholder": "http://user:pass@host:port 或 socks5://host:port",
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
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "v1.5.1：修复 Turnstile 空 iframe。默认不预跑 A/B 挑战、不篡改浏览器指纹；"
                                            "持续空白 30 秒后自动强制重建一次。system_chrome 不存在时仍会回退内置 Chromium。",
                                },
                            }],
                        }],
                    },
                ],
            },
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": True,
            "cron": "30 3 */3 * *",
            "login_url": "https://dashboard.katabump.com/auth/login",
            "accounts_json": "",
            "chrome_path": "",
            "browser_mode": "auto",
            "turnstile_wait": 120,
            "renew_attempts": 3,
            "headless": False,
            "proxy_server": "",
            "proxy_mode": "auto",
        }

    def get_page(self) -> List[dict]:
        # 展示最近一次续期历史
        history = self.get_data("history") or []
        if not history:
            return [{
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": "暂无续期记录，运行一次后这里会显示结果。"},
            }]
        rows = []
        for item in sorted(history, key=lambda x: x.get("time", ""), reverse=True):
            ok = item.get("success")
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": item.get("time", "")},
                    {"component": "td", "text": item.get("name") or item.get("username", "")},
                    {"component": "td",
                     "props": {"style": f"color:{'#4caf50' if ok else '#f44336'}"},
                     "text": "成功" if ok else "失败"},
                    {"component": "td", "text": item.get("expiry", "") or "-"},
                    {"component": "td", "text": item.get("detail", "")},
                ],
            })
        return [{
            "component": "VCard",
            "content": [{
                "component": "VTable",
                "props": {"hover": True},
                "content": [
                    {"component": "thead", "content": [{
                        "component": "tr",
                        "content": [
                            {"component": "th", "text": "时间"},
                            {"component": "th", "text": "账号"},
                            {"component": "th", "text": "结果"},
                            {"component": "th", "text": "Expiry"},
                            {"component": "th", "text": "详情"},
                        ],
                    }]},
                    {"component": "tbody", "content": rows},
                ],
            }],
        }]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"Katabump 续期停止调度失败: {e}")

    # ============================================================
    # 账号解析
    # ============================================================
    def _parse_accounts(self) -> List[Dict[str, str]]:
        raw = (self._accounts_json or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except Exception as e:
            logger.error(f"账号 JSON 解析失败: {e}")
            return []
        if isinstance(parsed, list):
            items = parsed
        elif isinstance(parsed, dict) and (parsed.get("username") or parsed.get("email")):
            items = [parsed]
        elif isinstance(parsed, dict):
            items = parsed.get("accounts") or parsed.get("users") or []
        else:
            items = []
        result = []
        for it in items:
            if not isinstance(it, dict):
                continue
            u = str(it.get("username") or it.get("email") or "").strip()
            p = str(it.get("password") or "").strip()
            if not u or not p:
                continue
            result.append({
                "name": str(it.get("name") or it.get("remark") or "").strip(),
                "purpose": str(it.get("purpose") or "").strip(),
                "username": u,
                "password": p,
            })
        # Android one-click fallback: when no standalone account is available, use enabled
        # server records that contain panel credentials. This also accepts combined exports.
        if not result:
            if isinstance(parsed, dict):
                server_items = parsed.get("servers") or parsed.get("Servers") or []
            elif isinstance(parsed, list):
                server_items = parsed
            else:
                server_items = []
            for item in server_items:
                if not isinstance(item, dict):
                    continue
                enabled = item.get("enabled", item.get("Enabled", True))
                if enabled is False:
                    continue
                username = str(
                    item.get("panelUsername") or item.get("PanelUsername") or
                    item.get("panel_user") or item.get("PanelUser") or ""
                ).strip()
                password = str(
                    item.get("panelPassword") or item.get("PanelPassword") or
                    item.get("panelPasswordPlain") or item.get("PanelPasswordPlain") or ""
                ).strip()
                if not username or not password:
                    continue
                result.append({
                    "name": str(item.get("name") or item.get("Name") or "").strip(),
                    "purpose": str(item.get("purpose") or item.get("Purpose") or "").strip(),
                    "username": username,
                    "password": password,
                })
        return result

    @staticmethod
    def _mask(username: str) -> str:
        v = (username or "").strip()
        if "@" not in v:
            return (v[:2] + "***") if len(v) > 2 else "***"
        name, domain = v.split("@", 1)
        masked = (name[:2] + "***") if len(name) > 2 else (name[:1] + "*")
        return f"{masked}@{domain}"

    # ============================================================
    # 入口：调度/单次都走这里（在后台线程执行 asyncio）
    # ============================================================
    def run_renew(self):
        if self._running:
            logger.warning("Katabump 续期正在运行中，跳过本次触发")
            return
        accounts = self._parse_accounts()
        if not accounts:
            logger.warning("Katabump 续期：未配置有效账号")
            return
        self._running = True
        try:
            logger.info(f"[Katabump] 插件版本 {self.plugin_version}，账号数 {len(accounts)}，"
                        f"proxy_mode={self._proxy_mode}，proxy={'有' if self._proxy_server else '无'}")
            results = asyncio.run(self._run_all(accounts))
            self._save_history(results)
            self._notify_summary(results)
        except Exception as e:
            logger.error(f"Katabump 续期执行异常: {e}")
        finally:
            self._running = False

    def _save_history(self, results: List[Dict[str, Any]]):
        history = self.get_data("history") or []
        history.extend(results)
        # 只保留最近 100 条
        history = history[-100:]
        self.save_data("history", history)

    def _notify_summary(self, results: List[Dict[str, Any]]):
        if not self._notify:
            return
        ok = sum(1 for r in results if r.get("success"))
        fail = len(results) - ok
        lines = [f"✅ 成功 {ok} / ❌ 失败 {fail} / 共 {len(results)}", "━━━━━━━━"]
        for r in results:
            icon = "✅" if r.get("success") else "❌"
            username = r.get("username", "")
            display_name = r.get("name") or ""
            masked_user = self._mask(username)
            account_text = f"{display_name} ({masked_user})" if display_name and display_name != username else masked_user
            expiry = f"；Expiry {r.get('expiry')}" if r.get("expiry") else ""
            lines.append(f"{icon} {account_text}：{r.get('detail','')}{expiry}")
        lines.append("━━━━━━━━")
        lines.append(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title="【Katabump 自动续期】",
            text="\n".join(lines),
        )

    def _screenshot_dir(self) -> Path:
        d = Path(self.get_data_path()) / "screenshots"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _run_all(self, accounts: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        # 延迟导入引擎：只有真正运行时才加载 playwright，避免插件加载期报错
        from .renew_engine import run_all
        return await run_all(
            accounts=accounts,
            login_url=self._login_url,
            shot_dir=self._screenshot_dir(),
            logger=logger,
            chrome_path=self._chrome_path,
            browser_mode=self._browser_mode,
            turnstile_wait=self._turnstile_wait,
            renew_attempts=self._renew_attempts,
            headless=self._headless,
            proxy_server=self._proxy_server,
            proxy_mode=self._proxy_mode,
        )
