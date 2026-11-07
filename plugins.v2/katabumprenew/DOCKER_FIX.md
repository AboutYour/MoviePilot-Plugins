# Docker/NAS 环境 Turnstile 失败修复方案

## 问题症状
- 手机 APK 和 Windows 电脑能正常签到
- Docker/NAS 环境下 Turnstile iframe 持续空白,120秒超时
- 日志显示 `channel=chrome` 失败回退到内置 chromium
- 出现 `Error: 300010` 和 `crashed_retry`

## 根本原因
Docker 容器缺少系统 Chrome,回退使用的 Playwright 内置 Chromium 在容器环境下缺少完整的 GPU/WebGL 支持,导致 Cloudflare Turnstile 小组件崩溃。

## 解决方法(任选其一)

### 方案 1: 在容器内安装真实的 Google Chrome (推荐)

进入 MoviePilot 容器:
```bash
docker exec -it moviepilot /bin/bash
```

安装 Chrome:
```bash
# Debian/Ubuntu 容器
wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt-get update
apt-get install -y ./google-chrome-stable_current_amd64.deb
rm google-chrome-stable_current_amd64.deb

# 或者用 apt 源
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add -
echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
apt-get update
apt-get install -y google-chrome-stable
```

重启插件即可,代码会自动使用系统 Chrome。

### 方案 2: 使用宿主机 Chrome via CDP

在宿主机(NAS 本机)启动 Chrome 并暴露 CDP:
```bash
# 安装 Chrome(如果没有)
# 群晖可以用 Container Manager 的终端或 SSH

# 启动 Chrome 开启远程调试
google-chrome-stable \
  --remote-debugging-port=9222 \
  --remote-debugging-address=0.0.0.0 \
  --headless=new \
  --no-sandbox \
  --disable-dev-shm-usage \
  --user-data-dir=/tmp/chrome-katabump
```

在插件配置中填写:
- **浏览器模式**: `外部 Chrome CDP`
- **Chrome 路径或 CDP**: `http://宿主机IP:9222`

### 方案 3: 修改 Dockerfile 持久化安装 Chrome

如果你能重建 MoviePilot 镜像,在 Dockerfile 添加:
```dockerfile
RUN wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && \
    apt-get update && \
    apt-get install -y ./google-chrome-stable_current_amd64.deb && \
    rm google-chrome-stable_current_amd64.deb
```

### 方案 4: 强制使用内置 Chromium + 降低 Turnstile 期望(备选)

如果无法安装 Chrome,可尝试改用 `browser_mode=playwright` 并降低检测强度(不保证成功):

插件配置:
- **浏览器模式**: `仅 Playwright Chromium`
- **Turnstile 等待时间**: 改为 180 秒

这只是延长等待,Turnstile 依然可能因 WebGL 问题崩溃。

## 验证方法

修复后查看日志,应该看到:
```
INFO: [katabumprenew] 使用系统 Chrome 渠道(channel=chrome)
```

而不是:
```
INFO: [katabumprenew] 使用 Playwright 内置 chromium
```

## 为什么 Android/Windows 不需要这个修复?

- **Android**: WebView 使用系统原生 Chrome 内核,自带完整 GPU 加速
- **Windows**: 系统已安装 Chrome/Edge,代码优先使用系统浏览器
- **Docker**: 精简镜像通常不含浏览器,只有 Playwright 下载的裸 Chromium

## 技术细节

renew_engine.py 的启动逻辑(1776-1802行):
1. 先尝试 `channel="chrome"` (系统 Chrome)
2. 失败则用指定路径 `executable_path`
3. 最后回退到 Playwright 内置 chromium

容器环境第1步失败 → 直接跳到第3步 → Chromium 缺少 GPU → Turnstile 崩溃。

安装真实 Chrome 后,第1步成功 → 使用系统 Chrome → Turnstile 正常工作。
