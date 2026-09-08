# AGENTS.md — QQ 群机器人项目速查

> 本文件供后续 AI 代理（Codex 等）快速了解本项目。目标、结构、环境事实如有变化，请同步更新本文。

## 1. 项目概述

- 项目：qqrobottext（本地目录 `D:\codex\qqrobot`），极简 QQ 群聊机器人，**Python 版**。
- 唯一功能：群聊中被 `@机器人本人` 时，回复纯文本 `对不起做不到。`
- 明确不处理：普通群消息、`@全体成员`、私聊、主动发言、引用/`@` 回。
- 架构：NapCat（QQ 协议端，登录真实 QQ 并提供 OneBot v11 服务）+ 本项目 Python 客户端（连接 NapCat 正向 WebSocket）。
- 依赖：仅第三方库 `websockets`；其余为 Python 标准库。要求 Python ≥ 3.10（本机 3.12.14）。

## 2. 目录结构

- `src/bot.py`：全部机器人逻辑与程序入口（asyncio + websockets）。
- `tests/test_bot.py`：行为测试（unittest，5 项，全部通过）。
- `requirements.txt`：`websockets>=12.0`。
- `.env.example` / `.gitignore`：环境变量示例；`.env` 不存在时走默认值。Python 的 `.venv/`、`__pycache__/` 已忽略。
- `README.md`：完整中文安装与使用文档（含 NapCat 配置）。
- `LICENSE`：MIT。
- `qq/`：NapCat/QQ 本地运行时目录（git 已忽略，不上传）。
  - `qq/napcat/launcher-fixed.bat`：本机修复版启动器（显式指定 QQ.exe 路径，绕过注册表探测）。
  - `qq/napcat/config/onebot11_<QQ号>.json`：OneBot 网络配置（QQ 号 3958801964）。
  - `qq/napcat/config/webui.json`：WebUI 配置与登录 token —— **敏感，勿写入文档或提交**。
- 说明：原 Node.js 实现（`src/index.js`、`package.json`）已在 Python 重构时删除。

## 3. 运行与配置

- 本机运行（已建好 `.venv`，勿删）：`.venv\Scripts\python.exe src\bot.py`
- 从零安装：`python -m venv .venv`，再 `.venv\Scripts\python.exe -m pip install -r requirements.txt`。
- 运行测试：`.venv\Scripts\python.exe -m unittest discover -s tests -v`
- 环境变量：`NAPCAT_WS_URL` 默认 `ws://127.0.0.1:3001`；`NAPCAT_WS_TOKEN` 默认空（通过 URL `access_token` 参数传递）。
- 机器人 QQ 号不写进代码：运行时用事件 `self_id` 动态识别。
- Windows 系统 `python` 仍指向商店占位符（不可用），**一律使用 `.venv\Scripts\python.exe` 完整路径**。

## 4. 实现要点（改动前必读）

- `QQBot.run()`：`websockets.connect` 循环；断开/异常后每 3 秒自动重连，不崩溃。
- `_read_loop(ws, pending=None)`：读取消息；动作响应按 `echo` 回填 `pending` future；群消息事件 `create_task` 并发处理；连接结束统一处理未决 future。
- 只处理 `post_type=message` 且 `message_type=group`；忽略 `user_id === self_id`。
- `is_at_self()`：消息分段数组中 `at.data.qq === self_id`；`qq=all`（@全体）不触发；字符串消息格式回退解析 `raw_message` 中 `[CQ:at,qq=...]`。
- 回复：`send_group_msg`，参数 `group_id` + `message: '对不起做不到。'`；`_send_action` 超时 10 秒。

## 5. 本机环境事实（排查关键）

- QQ 路径：`D:\Program Files\Tencent\QQNT\QQ.exe`，版本 `9.9.19.35184`（偏旧但 NapCat v4.18.19 曾实测可运行；NapCat `qqnt.json` 目标版本 9.9.22-40990）。
- 该 QQ 无注册表卸载项，NapCat 官方 `launcher*.bat` 会报 `provided QQ path is invalid`；必须使用 `qq/napcat/launcher-fixed.bat`（管理员运行、先彻底退出 QQ）。
- Python 安装方式：官方安装器在无桌面会话下失败，改用 uv 管理的独立 Python：`C:\Users\qwer1\AppData\Roaming\uv\python\cpython-3.12.14-windows-x86_64-none\python.exe`；uv 本体在 `%LOCALAPPDATA%\Programs\uv\uv.exe`。
- 当前 NapCat 状态（2026-09-09）：**未运行**，端口 3001/6099 均未监听；需要重新运行 `launcher-fixed.bat` 并完成 QQ 登录后再做端到端验收。
- 已核实的 OneBot WS 配置（`onebot11_3958801964.json`）：host `127.0.0.1`、port `3001`、`messagePostFormat=array`、token 空、heartbeat 30s。
- NapCat 日志仅在控制台（`fileLog=false`）；WebUI token 见启动日志或 `config/webui.json`。
- GitHub 网络需走本机 Clash 代理 `http://127.0.0.1:7890`（仓库已配置 `http.proxy`）。

## 6. 进度与待办

- 已完成：Node → Python 重构；5 项 unittest 全部通过；语法与集成行为验证通过。
- 待办：启动 NapCat 后做真实端到端验收（`@机器人` 回复、普通消息/`@全体`/自身消息不回复）。
- 长期注意：QQ 升级后 NapCat 需同步升级；用真实 QQ 登录存在风控/封号风险，建议小号。

## 7. 版本控制与开源状态

- 本机 git 仓库分支 `main`，远端 `origin`：<https://github.com/infinitymyheaven/qqrobottext>（公开）。
- 提交作者：`infinitymyheaven` + GitHub noreply 邮箱；凭据由 GitHub CLI（`C:\Program Files\GitHub CLI\gh.exe`）管理。
- 重构改动当前为工作区/本地状态，尚未推送时需先 `git add -A && git commit`。
- `.gitignore` 忽略 `qq/`、`.venv/` 等；NapCat 不随仓库分发；自研代码 MIT。

## 8. 参考

- `README.md`：面向用户的中文使用说明。
- NapCat 官方文档：<https://napneko.github.io/>；发布页：<https://github.com/NapNeko/NapCatQQ/releases>
- `websockets` 文档：<https://websockets.readthedocs.io/>
- 对接方式参考项目：Miaoge-Ge/qq-llm-bot、kuliantnt/qq-maid-bot、MoXueYao/QQBot。
