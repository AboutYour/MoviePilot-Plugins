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
3. 打开插件配置：
   - **启用插件**
   - **账号列表 (JSON)**：`[{"username":"a@x.com","password":"pwd1"}, {"username":"b@x.com","password":"pwd2"}]`
   - **执行周期**：默认 `30 3 */3 * *`（每 3 天凌晨 3:30）；按你的续期周期调整
   - **发送通知**：开启后每轮结束把汇总推到 MP 通知
   - 其余（Turnstile 等待、续期重试、Chrome 路径）保持默认即可
4. 保存。可勾「立即运行一次」验证。

## 配置项说明

| 配置 | 说明 |
|---|---|
| 账号列表 (JSON) | 账号数组，字段 `username`/`password`，与桌面版 `login.json` 兼容 |
| 执行周期 (cron) | 标准 5 段 cron，MP 本地时区 |
| 登录地址 | 默认 `https://dashboard.katabump.com/auth/login` |
| Turnstile 等待(秒) | 等令牌就绪的上限，默认 120 |
| 续期重试次数 | Renew/ALTCHA 失败重试，默认 3 |
| 无头模式 | 默认关闭。Turnstile/ALTCHA 需要“有头”环境，容器里配合 xvfb |
| Chrome 路径(可选) | 留空自动用容器内 chromium；也可指定宿主挂进来的 Chrome |

## 注意

- 必须住宅网络运行，否则登录会卡在 Turnstile。
- 首次运行下载 chromium 稍慢，属正常。
- 截图保存在插件数据目录 `screenshots/` 下，历史记录可在插件详情页查看。
