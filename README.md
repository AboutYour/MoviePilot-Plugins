# Katabump 自动续期 — MoviePilot 插件

定时登录 Katabump 免费面板，自动为服务器续期（登录过 Cloudflare Turnstile → 进入服务器详情 See → Renew → 解 ALTCHA 验证码 → 确认续期 → 截图），结果通过 MoviePilot 自带通知推送。

## 为什么用 MoviePilot 插件

- **不用单独部署**：装进 MoviePilot 就自己定时跑，复用 MP 的调度和通知（微信/Telegram/Bark 等 MP 已配的渠道）。
- **依赖已就绪**：MoviePilot 容器已内置 `playwright`；插件首次运行会自动补装 chromium 二进制（约 150MB，仅一次）。
- **住宅 IP 才能过验证**：Cloudflare Turnstile 会拒绝机房/云 IP。装在**家里的 NAS**（住宅宽带出口）上运行才能通过；这也是云端跑不通的根本原因。

## GitHub 仓库结构（第三方库）

仓库名建议用社区约定的 **`MoviePilot-Plugins`**。索引 `package.v2.json` 必须在**根目录**，插件代码放进 **`plugins.v2/`** 子目录：

```
MoviePilot-Plugins/              ← 仓库根（推送到 GitHub 的内容）
├── package.v2.json              仓库索引（必须在根，插件市场读它）
├── plugins.v2/                  V2 插件目录
│   └── katabumprenew/           目录名 = 插件类名小写
│       ├── __init__.py          插件主类（元数据 + 配置界面 + 调度）
│       ├── renew_engine.py      Playwright 自动化引擎（登录/续期/验证码）
│       └── requirements.txt     额外依赖（MP 一般已内置 playwright）
└── icons/                       图标（可选）
    └── katabumprenew.png
```

推送前需替换：`package.v2.json` 里的 `authorUrl`、`icon` 改成你自己的仓库地址（或删掉 `icon` 用默认）。

## 安装

1. 把 `package.v2.json`、`plugins.v2/` 推到你的 GitHub 仓库（分支 `main`）。
2. MoviePilot → 设置 → 插件 → 插件仓库，填入仓库地址 `https://github.com/<你的用户名>/MoviePilot-Plugins`。
3. 在插件市场找到「Katabump自动续期」，点安装。
4. 打开插件配置：
   - **启用插件**
   - **账号列表 (JSON)**：`[{"name":"主账号","purpose":"主服务器","username":"a@x.com","password":"pwd1"}, {"name":"备用账号","username":"b@x.com","password":"pwd2"}]`
   - **执行周期**：默认 `30 3 */3 * *`（每 3 天凌晨 3:30）；按你的续期周期调整
   - **发送通知**：开启后每轮结束把汇总推到 MP 通知
   - 其余（Turnstile 等待、续期重试、Chrome 路径）保持默认即可
5. 保存。可勾「立即运行一次」验证。

## 配置项说明

| 配置 | 说明 |
|---|---|
| 账号列表 (JSON) | 账号数组，字段 `name`/`purpose`（可选）及 `username`/`password`，与 Android/桌面版 `login.json` 兼容；也可粘贴 Android 组合导出的 `{accounts,servers}`；没有账号时从启用服务器的面板凭据生成目标；每条记录独立执行，不按用户名去重 |
| 执行周期 (cron) | 标准 5 段 cron，MP 本地时区 |
| 登录地址 | 默认 `https://dashboard.katabump.com/auth/login` |
| Turnstile 等待(秒) | 等令牌就绪的上限，默认 120；若已判定 CF 网络失败会提前结束 |
| 续期重试次数 | Renew/ALTCHA 失败重试，默认 3 |
| 无头模式 | 默认关闭。Turnstile/ALTCHA 需要“有头”环境，容器里配合 xvfb |
| Chrome 路径(可选) | 留空自动用容器内 chromium；也可指定宿主挂进来的 Chrome |
| 代理模式 | `auto`（默认）/ `direct` / `system` / `custom`，见下 |
| 代理服务器(可选) | `http://user:pass@host:port` 或 `socks5://host:port` |

### 代理模式

| 模式 | 行为 |
|---|---|
| **auto（推荐）** | 先 TCP 探测 `challenges.cloudflare.com`；直连可达则直连；不可达则回退环境 `HTTP(S)_PROXY`；若填写了代理字段则优先用配置代理 |
| **direct** | 强制直连，忽略环境代理与配置代理 |
| **system** | 使用容器环境变量里的 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` |
| **custom** | 只用「代理服务器」字段 |

`socks5://` 会自动改成 `socks5h://`（**DNS 走代理**），避免容器 DNS 解析不了 `*.challenges.cloudflare.com`。

## 注意

### 两类失败要分清

1. **网络/DNS 失败**（日志里有 `ERR_NAME_NOT_RESOLVED`、`challenges.cloudflare.com` 请求失败、iframe=0）  
   - 容器访问不了 Cloudflare 挑战服务。  
   - 处理：修 DNS/出站，或填一个能访问 Cloudflare 的代理（`proxy_mode=custom` + 代理地址）。

2. **IP 信誉失败**（iframe 能渲染，但 token 一直为空）  
   - 出口被 Cloudflare 判定为机房/云 IP。  
   - 处理：住宅网络直连，或**住宅代理**。

- 首次运行下载 chromium 稍慢，属正常。
- 截图保存在插件数据目录 `screenshots/` 下，历史记录可在插件详情页查看。

## 容器内自检（可选）

```bash
# DNS / 连通性
getent hosts challenges.cloudflare.com
# 或
nslookup challenges.cloudflare.com
curl -I --max-time 10 https://challenges.cloudflare.com
```

若这里就失败，插件里也过不了 Turnstile，必须先解决网络或挂代理。

## 版本

- **v1.5.1**：修复 Turnstile 空 iframe；默认关闭 A/B 预探针和指纹篡改；空白 30 秒后强制重建；修复 Python 转义警告
- **v1.5.0**：同步 Android 一键签到逻辑；账号独立会话；宽容定位 See/Renew；ALTCHA 稳定确认；严格校验本轮 See/Renew；记录 Expiry
- **v1.3.2**：修复主域通、随机子域（brunhild.*）不通时误关 DNS 映射；直连强制 `MAP *.challenges.cloudflare.com`
- **v1.3.0**：公共 DNS 预解析 + host-resolver-rules / DoH；读取 `PROXY_HOST`；启动打印引擎版本
- **v1.2.0**：CF 连通性预检、代理 auto 回退、socks5h、网络失败提前结束
- **v1.1.0**：反自动化检测、直连优先、真实 Chrome 渠道
- **v1.0.0**：初版续期流程

### 如何确认已更新到最新版

```text
[Katabump] 插件版本 1.5.1 ...
[Katabump] ======== Katabump 引擎 v1.5.1 启动 ========
[Katabump] 将 *.challenges.cloudflare.com 全部映射到 104.x.x.x
[Katabump] 已注入 host-resolver-rules ...
```

没有 `v1.5.1` / `host-resolver-rules` 说明未更新成功：卸载重装插件并重启 MoviePilot。
