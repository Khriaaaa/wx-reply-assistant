<div align="center">
  <img src="assets/hero.png" alt="微信回复助手：读你的微信，递上三句能发的话" width="900">
</div>

<br>

# 微信回复助手

**读你的微信，递上三句能发的话。**
只给建议，绝不替你发。

消息弹出来，脑子却一片空白 —— 明知道该回，就是想不出怎么开口。
这个工具只做一件小事：把对方最近说的话读进来，替你起三个头，
顺眼就复制走，不顺眼就当没看见。

它不接管你的微信，不碰你的键盘，更不会在你没看的时候把话发出去。

<br>

## 它长什么样

<div align="center">
  <img src="assets/panel.png" alt="面板：对方最近说、对话参考、三条候选回复" width="620">
</div>

面板只干一件事：把「对方最近说了什么」「这句话大概什么意思」「三条不同口吻的候选」
摆在同一屏。候选卡片上的百分比，是模型对自己那句话的把握，不是对你的打分 ——
看看就好，别当真。

每条候选都能一键复制。左下角的「填入」按钮默认是灰的：打开后能把话填进微信输入框，
但**不会按回车**，回车永远是你的事。这是故意的。

> 上图用的是合成演示数据（`tools/make_demo_store.py` 生成），不是谁的聊天记录。

<br>

## 它怎么工作

<div align="center">
  <img src="assets/flow.png" alt="一条消息走完的六步" width="900">
</div>

读屏走 Windows 无障碍接口（UIA），不是截图猜字。聊天窗口只给正文、不标发送人，
所以"谁说的"靠两条证据拼：气泡在左还是在右（像素），引用气泡里写着谁（文字，更稳）。
两条对不上，整轮直接丢弃 —— 宁可少记，不记错。

去重靠指纹，触发条件是「对方发了消息 + 静默 1.2 秒」，对方连发三条也只生成一次。
生成失败会重试，重试按会话记账：一个会话挂了，不会连累另一个会话的消息。

<br>

## 结构

<div align="center">
  <img src="assets/arch.png" alt="架构：微信窗口 → 采集器 → 编排 → 面板" width="900">
</div>

```
collector/     读屏与入库（PowerShell 跑在虚机里 + Python 解析）
orchestrator/  触发、拼上下文、调模型、校验
prompts/       系统提示词与返回结构
web/           面板（标准库 http.server，带密码）
tools/         虚机连接、演示数据、填入通道
docs/          工程笔记
store/         运行期数据（聊天记录、建议、截图）—— 不进仓库
```

采集器和编排层都能单独跑：虚机里是纯 PowerShell，编排层是纯标准库 Python，
只有调模型那一步需要联网。

<br>

## 快速开始

### 单机版（推荐：一台 Windows 就能跑）

微信在哪个 Windows 上，就装在哪台：

```
下载 https://github.com/Khriaaaa/wx-reply-assistant/releases/latest/download/wx-reply-assistant-setup.exe
双击装上 → 桌面出现「微信回复助手」→ 打开就是面板
```

自带运行环境（Python、依赖、读屏工具全在安装包里），不用先装别的东西，
不要管理员权限。数据放在 `%APPDATA%\wx-reply-assistant\`，卸载不丢。
第一次打开会让你接一个模型接口（填个 API Key 就行），然后就能用了。

想从源码打这个安装包：见 [desktop/README.md](desktop/README.md)。

### 两台机器（虚机隔离 / NAS 常驻那套）

前提：微信跑在一台 Windows 机器上（虚拟机、实体机都行），能通过 QEMU guest agent 操作，
并且装了能读 UIA 的命令行工具（这里用的是 winapp-cli 风格的 `ui inspect` /
`ui scroll` / `ui screenshot`）。

```bash
# 0. Windows 侧（微信跑在那台机器上）：双击 wxreply-setup.exe，缺什么它自己下
#    下载 https://github.com/Khriaaaa/wx-reply-assistant/releases/latest/download/wxreply-setup.exe
#    没带这个文件的话，源码在 scripts/setup-windows.ps1，打包法在 scripts/build-exe.ps1

# 1. 虚机连接参数（不进仓库）
cp config.example.yaml config.local.yaml   # 填 ssh / qga_uuid

# 2. 面板与生成用的模型：需要一个 OpenAI 兼容的 /chat/completions 端点
#    默认读 /opt/data/config.yaml，可用 WXREPLY_CONFIG 指向别处，
#    或在项目根放 config.local.yaml 覆盖（见 orchestrator/generate.py 顶部）

# 3. 先看看界面长什么样（合成数据）
python3 tools/make_demo_store.py --out store

# 4. 起面板
python3 web/server.py --port 8801 --lan   # 默认只监听本机；要手机/别的机器访问就加 --lan
#    首次启动会生成随机密码，写在 store/.panel_password

# 5. 真采集（虚机侧要能读到微信窗口）
python3 collector/wx_collector.py start
python3 collector/wx_collector.py status
```

采集器不依赖面板也能跑；面板自带检查线程，对方一来新消息就重新生成。

<br>

## 边界

- **只读**。不点击、不发送、不模拟键盘；唯一的写入是这个项目自己的 `store/`
- **填入默认关闭**。想开要显式设置 `store/.allow_fill` 或 `HERMES_PANEL_ALLOW_FILL=1`，
  开了也只是填进输入框 —— 回车永远由你按
- **面板有密码**。密码和会话密钥启动时随机生成，存在本地文件，不经过任何第三方
- **聊天记录不出本机**。发给模型服务商的只有最近若干条对话，拿回三条候选 ——
  没有别的数据外流

<br>

## 已知限制

- 一次只跟当前打开的那个会话，多会话还没做
- 发送人判定依赖界面布局，微信大改版可能要重新标定
- 虚机上微信窗口得一直开着，锁屏或最小化就读不到了
- 生成质量看你接的模型。提示词在 `prompts/reply_system.md` —— 改它比改代码管用

<br>

## 工程笔记

踩过的坑、UIA 到底给了什么、为什么填入必须绕一大圈走 QMP —— 都在
[docs/notes.md](docs/notes.md)。这份笔记比 README 长，也更接近这个项目的真相。

<br>

## 许可

本项目采用 [AGPL-3.0](LICENSE) 协议开源。
