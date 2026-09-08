# QQ 群聊机器人（Python 版）

一个极简的 QQ 群聊机器人：当群成员在群里 `@机器人` 时，机器人发送一条纯文本消息 `对不起做不到。`。

架构上由两部分组成：

- **NapCat**：QQ 协议端。负责登录一个普通 QQ 账号，并以 OneBot v11 正向 WebSocket 服务的形式提供消息收发能力。
- **本项目**：Python 客户端（`src/bot.py`）。连接 NapCat 的 WebSocket，监听群消息事件，检测到被 `@` 后调用 `send_group_msg` 发送回复。

本项目仅依赖第三方库 `websockets`（其余均为 Python 标准库），机器人 QQ 号不需要写进配置——运行时自动从事件中的 `self_id` 获取。

## 项目结构

```text
qqrobot/
├── src/bot.py         # 全部机器人逻辑（入口）
├── tests/test_bot.py  # 行为测试（unittest）
├── requirements.txt   # Python 依赖
├── .env.example       # 环境变量示例
├── .gitignore
└── README.md
```

## 环境要求

- Windows / macOS / Linux（与 NapCat 同机运行）
- Python ≥ 3.10（开发环境为 3.12）
- 一个用于登录 NapCat 的普通 QQ 账号
- 一个用于测试的 QQ 群

> ⚠️ 风险提示：NapCat 通过普通 QQ 登录，属于非官方协议实现，账号存在被腾讯风控、限制甚至封禁的风险。建议使用不重要的 QQ 小号，并自行评估是否接受该风险。

## 第一步：安装并配置 NapCat

NapCat 的安装方式与依赖版本会持续更新，最新步骤请以官方文档为准：<https://napneko.github.io/guide/boot/Shell>。Windows x64 从零开始推荐“一键包”路线：

1. 前往 NapCat 的 GitHub Releases 页面（<https://github.com/NapNeko/NapCatQQ/releases>），下载最新版本的 `NapCat.Shell.Windows.OneKey.zip`（内置 QQ 与 NapCat，无需先安装 QQ）。
2. 解压到不含中文与空格的路径（例如 `D:\NapCat`），双击其中的 `NapCatInstaller.exe`，等待自动化配置完成。
3. 进入自动生成的 `NapCat.XXXX.Shell` 目录，双击 `napcat.bat` 启动。启动后会出现一个控制台窗口，**不要关闭它**。
4. 从 NapCat 控制台日志中找到形如 `WebUi User Panel Url: http://127.0.0.1:6099/webui?token=xxxxx` 的地址，复制到浏览器打开。
5. 在 WebUI 内先进入「QQ 登录」并点击 `QRCode`，用**机器人 QQ 账号**的手机 QQ 扫码登录。登录成功后 WebUI Token 会刷新，请再从 NapCat 控制台（或手机 QQ 收到的消息）获取最新带 token 的地址并重新进入。
6. 重新进入 WebUI 后，按要求设置一个 WebUI 管理密码（不设置会禁用大部分功能）。
7. 进入「网络配置」，点击「新建」，创建一个 **WebSocket 服务器（正向 WS / OneBot v11）**：
   - 端口：`3001`
   - 监听主机：`127.0.0.1`（仅本机使用，更安全；公网部署请勿绑定 `0.0.0.0`）
   - 消息上报格式 `messagePostFormat`：`array`
   - Access Token：留空；若设置，则同步填写到本项目的 `.env`
   - 勾选「保存时启用」，然后保存。
8. 将机器人 QQ 拉入你的测试群。

> 备选：手动路线为先安装新版 PC 版 QQ（QQNT），再下载 `NapCat.Shell.Windows.Node.zip` 或 `NapCat.Shell.zip`，解压后运行 `launcher.bat`（Win10 用 `launcher-win10.bat`），WebUI 的后续配置步骤相同。

## 第二步：安装本项目依赖

在项目根目录打开终端，创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS / Linux 将第二行换成 `.venv/bin/python -m pip install -r requirements.txt`。

> Windows PowerShell 可能禁止运行 `.ps1` 激活脚本，因此无需执行 activate，直接用 `.venv\Scripts\python.exe` 即可。

## 第三步：启动机器人

```powershell
.venv\Scripts\python.exe src\bot.py
```

看到类似 `已连接 NapCat OneBot WebSocket。` 的日志即表示连接成功。然后：

1. 在测试群里 `@机器人`，机器人会回复：`对不起做不到。`
2. 如修改了端口或设置了 Access Token，先复制环境变量示例再修改：

   ```powershell
   Copy-Item .env.example .env
   ```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NAPCAT_WS_URL` | `ws://127.0.0.1:3001` | NapCat 正向 WebSocket 服务地址 |
| `NAPCAT_WS_TOKEN` | 空 | NapCat WebSocket 服务器设置的 Access Token；未设置则留空 |

## 运行测试

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

覆盖：`@本人` 回复一次且内容正确、普通消息/私聊/自身消息/`@全体成员` 均不回复、字符串 CQ 码兜底解析。

## 行为边界

- 只响应群聊中 `@机器人本人` 的消息。
- `@全体成员`（`qq=all`）不会触发回复。
- 不 `@` 回提问者、不引用原消息，回复为纯文本。
- 机器人不会主动发言，也不处理私聊、加群申请等其他事件。
- 断线后每 3 秒自动重连，不崩溃。

## 常见问题

- **一直提示“正在连接”/连接失败**：NapCat 未运行、WebSocket 服务器未开启、端口被改过，或 `.env` 中的 `NAPCAT_WS_URL` 与 NapCat 配置不一致。请检查 WebUI 网络配置与监听地址。
- **提示 `python` 不是有效命令**：Windows 若只装有 Microsoft Store 的 Python 占位符，请改用项目内 `.venv\Scripts\python.exe` 的完整路径运行。
- **连接后收不到消息**：确认 WebSocket 服务器的消息格式为 `array`；确认机器人确实在该群内。
- **设置了 Access Token 后连不上**：确认 `.env` 中 `NAPCAT_WS_TOKEN` 与 NapCat 中设置完全一致，或先取消 Access Token 排障。
- **@ 了不回复**：请确认 @ 的是机器人本人，而不是别人或 @全体。

## 开源许可与免责声明

- 本仓库仅包含本项目自研代码与文档，采用 [MIT License](LICENSE)。
- NapCat 是独立的第三方项目，采用其自有许可证（Limited Redistribution License：非商业用途、再分发需附完整许可证与来源说明、修改版不得公开发布）。因此**本仓库不附带、不重新分发 NapCat**，请按上文指引从 NapCat 官方 Releases 自行下载。
- 本项目通过非官方协议接入 QQ（登录普通 QQ 账号），违反腾讯《QQ 软件许可及服务协议》中关于禁止使用非官方客户端的条款，账号存在被风控、限制甚至封禁的风险。本项目仅供学习与测试，请使用不重要的 QQ 小号，风险自负。

## 参考项目与文档

本项目参考了以下开源项目验证过的对接方式（OneBot v11 事件/动作格式、默认 WS 端口与断线重连模式），并按“仅 @ 回复”的最小需求自建：

- [NapNeko/NapCatQQ](https://github.com/NapNeko/NapCatQQ)：QQ 协议端本体
- [Miaoge-Ge/qq-llm-bot](https://github.com/Miaoge-Ge/qq-llm-bot)：基于 NapCat（OneBot）的客户端实现参考
- [kuliantnt/qq-maid-bot](https://github.com/kuliantnt/qq-maid-bot)：NapCat OneBot v11 接入文档参考
- [MoXueYao/QQBot](https://github.com/MoXueYao/QQBot)：NapCat 安装与配置步骤参考
