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

# 3. 打包（输出 dist\wx-reply-assistant-setup.exe）
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

## 启动前的环境体检（缺什么补什么）

`preflight.ps1` 由主进程在启动时跑，排在 `findPython()` **前面** —— 补下来的那份 Python 也得能被找到。

体检七项：自带 Python 运行时、自带 winapp、程序文件、微信客户端（装没装 / 开没开）、数据目录、面板端口、VC++ 运行库。
能当场补的当场补：Python 运行时或依赖缺失，从安装包自带的补包（`python-pack.zip`）本地还原，不联网；
微信装了没开就拉起来；端口被自己旧进程占着就清掉；数据目录没有就建。

winapp 那个 94MB **不挡启动**，丢后台子进程去下（`-DownloadOnly winapp`），补完下次启动就位。
下载源按实测速度排：ghfast.top → gh-proxy.com → 直连（这台机器上 313 / 240 / 39 KB/s）。解压后剔掉 `.pdb` ——
压缩包里 331MB 是调试符号，安装包本来就没带（不带是 58MB，带上是 389MB）。

结果写进 `desktop.log`；只有需要人工处理的项（比如这台机器没装微信）才弹窗，其余静默。
源码方式跑（`npm start`）时脚本在 `desktop/preflight.ps1`，打包后落在 `resources/preflight.ps1`。

## 安装包完整性：为什么不做「自校验哈希」

曾经想给安装包加一段「安装前算出自己的 SHA256，和脚本里嵌的常量比对」。**这个做法无解**：
常量写在安装包自己里面，写进去哈希就变，改完得重算 —— `H(含 V 的 exe) == V` 没有解。
实测三遍构建得到三个不同哈希，收敛判据永远失败；真交付出去的话，好包会被自己拦下。

现在依赖 **NSIS 内建的完整性校验**（`CRCCheck`，默认开）。实测：

| 包的状态 | `/S` 静默运行结果 |
| --- | --- |
| 完好 | `exit=0` |
| 中段翻一个字节 | `exit=2`，0.37s 退出 |
| 截掉尾部 64KB | `exit=2`，0.05s 退出 |

零点几秒就退，说明是启动阶段直接拒掉，不是解压到一半才失败。
局限要说清：CRC 覆盖打包数据段，exe 前部的引导代码不在覆盖范围内。

用户想自己核对，发布时附上 `SHA256SUMS.txt` 和一个批处理（把安装包和它放同一目录，双击即校验）；
生成器是 `scripts/make-verify-bat.py`，它把当前 exe 的哈希写进批处理再输出。
