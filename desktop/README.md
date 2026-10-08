# 桌面版（Electron 壳）

把整套东西装进一个双击就能用的 Windows 应用：窗口一开就是面板，
采集 / 生成 / 面板三个后端进程由壳子自己起停，关窗口收进托盘，退出时一起收掉。

## 它做了什么

```
微信回复助手.exe（Electron）
├─ 渲染进程            → 直接 loadURL 面板地址（带进门票，没有地址栏、不用密码）
├─ 主进程（Node）      → 起停 assistant.py、托盘、开窗口、退出清理
│   └─ resources/py/   → 采集 + 编排 + 面板（仓库里的 Python 源码原样搬进来）
├─ resources/python/   → 自带 CPython embeddable（3.11）+ PIL/numpy/pyyaml
└─ resources/winapp/   → 自带 winapp.exe（读 UIA 用，MIT，来自 microsoft/winappCli）
```

装完不依赖系统里的任何东西：没有 Python 也能跑，不用跑安装脚本，不要管理员权限。
数据在 `%APPDATA%\wx-reply-assistant\store`（不动安装目录）。

## 从源码构建

在 Windows 构建机上（Linux 上没法交叉打 NSIS 包）：

```powershell
git clone https://github.com/Khriaaaa/wx-reply-assistant
cd wx-reply-assistant\desktop

# 1. 依赖（electron + electron-builder）
npm install
#   国内网络可先设：
#   npm config set registry https://registry.npmmirror.com
#   $env:ELECTRON_MIRROR = 'https://npmmirror.com/mirrors/electron/'

# 2. 备料：自带 Python + 三个轮子 + winapp.exe，落到 vendor/
C:\Py311\python.exe scripts\make-vendor.py
#   国内会走 npmmirror / 清华 PyPI；本机已经装了 winapp-cli 的话直接从那拷

# 3. 打包（输出 dist\wx-reply-assistant-<版本>-setup.exe）
$env:ELECTRON_BUILDER_BINARIES_MIRROR = 'https://npmmirror.com/mirrors/electron-builder-binaries/'
npx electron-builder --win nsis --x64
```

`vendor/`、`node_modules/`、`dist/` 都不进仓库（见根目录 .gitignore）。

## 调试

```powershell
npm start              # 开发模式直跑（用系统 Python，先设 WXREPLY_PYTHON 或装了 py -3）
```

主进程的日志在 `%APPDATA%\wx-reply-assistant\logs\desktop.log`，
后端各进程的日志在 `%APPDATA%\wx-reply-assistant\store\logs\`。

## 几个刻意的选择

- **不开系统浏览器**：壳子自己 loadURL，面板就长在应用窗口里（`assistant.py up --no-open`）
- **进门票只认本机**：`?k=` 那条 URL 仅 127.0.0.1 来源可用，开了 `--lan` 也换不到 cookie
- **退出要收干净**：先 `assistant.py down` 停采集/生成/面板，再退壳子
- **端口残留先清**：8801 被旧进程占着时请求会乱点头，boot 前会清掉自己的监听
