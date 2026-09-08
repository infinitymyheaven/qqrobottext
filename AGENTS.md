# AGENTS.md — QQ 群机器人项目速查

> 本文件供后续 AI 代理（Codex 等）快速了解本项目。目标、结构、环境事实如有变化，请同步更新本文。

## 1. 项目概述

- 项目：qqrobot-napcat-mini，极简 QQ 群聊机器人。
- 唯一功能：群聊中被 `@机器人本人` 时，回复纯文本 `对不起做不到。`
- 明确不处理：普通群消息、`@全体成员`、私聊、主动发言、引用/`@` 回。
- 架构：NapCat（QQ 协议端，登录真实 QQ 并提供 OneBot v11 服务）+ 本项目 Node.js 客户端（连接 NapCat 正向 WebSocket）。
- 依赖：零 npm 第三方依赖；要求 Node ≥ 22（本机 v24.20.0）。

## 2. 目录结构

- `src/index.js`：全部机器人逻辑与程序入口。
- `package.json`：脚本 `start`（`node --env-file-if-exists=.env src/index.js`）与 `check`（`node --check src/index.js`）。
- `.env.example` / `.gitignore`：环境变量示例；`.env` 不存在时走默认值。
- `README.md`：完整中文安装与使用文档。
- `qq/napcat/`：NapCat v4.18.19（Windows Shell）本体，解压于此，非手写代码；内含 node_modules、运行数据库等。
  - `qq/napcat/launcher-fixed.bat`：本机修复版启动器（显式指定 QQ.exe 路径，绕过注册表探测）。
  - `qq/napcat/config/onebot11_<QQ号>.json`：OneBot 网络配置（当前 QQ 号 3958801964）。
  - `qq/napcat/config/webui.json`：WebUI 配置与登录 token —— **敏感，勿写入文档或提交**。
- 项目尚未 `git init`；`qq/napcat` 体积大，若日后纳入版本控制需另行决策。

## 3. 运行与配置

- 启动机器人：仓库根目录 PowerShell 中执行 `npm.cmd start`（不能用 `npm start`：本机 ExecutionPolicy 禁止运行 `npm.ps1`）。
- 环境变量：`NAPCAT_WS_URL` 默认 `ws://127.0.0.1:3001`；`NAPCAT_WS_TOKEN` 默认空。自定义时复制 `.env.example` 为 `.env`。
- 机器人 QQ 号不写进代码：运行时用事件 `self_id` 动态识别。

## 4. 实现要点（改动前必读）

- `connect()`：使用 Node 内置 WebSocket；断开后每 3 秒自动重连，连接失败不崩溃。
- 动作调用：走同一 WS 发送 `{action, params, echo}`，按 `echo` 匹配响应，超时 10 秒。
- 只处理 `post_type=message` 且 `message_type=group`；忽略 `user_id === self_id` 的自身消息。
- `@` 判定：消息分段数组中存在 `at.data.qq === self_id`；`qq=all`（@全体）不触发；字符串消息格式则回退解析 `raw_message` 中 `[CQ:at,qq=...]`。
- 回复：调用 `send_group_msg`，参数 `group_id` + `message: '对不起做不到。'`。

## 5. 本机环境事实（排查关键）

- QQ 路径：`D:\Program Files\Tencent\QQNT\QQ.exe`，版本 `9.9.19.35184`（偏旧，但 NapCat v4.18.19 实测可运行；NapCat 的 `qqnt.json` 目标版本为 9.9.22-40990）。
- 该 QQ 无注册表卸载项，NapCat 官方 `launcher*.bat` 读注册表失败并报 `provided QQ path is invalid`；必须使用 `qq/napcat/launcher-fixed.bat`，且需管理员权限、启动前先彻底退出 QQ。
- NapCat 启动后 WebUI：`http://127.0.0.1:6099/webui?token=<随机值>`；token 见 NapCat 控制台日志或 `config/webui.json`。
- 当前 OneBot WS 服务器配置（`onebot11_3958801964.json`）：已启用、host `127.0.0.1`、port `3001`、`messagePostFormat=array`、token 空、heartbeat 30s、名称「qq机器人捏」。
- NapCat 登录 QQ 号：`3958801964`（配置文件后缀即该号）。
- 已核实（2026-09）：端口 3001/6099 均在监听；3001 已有机器人进程建立的连接；NapCat 与机器人进程均运行中。NapCat `fileLog=false`，日志仅在控制台。
- Windows PowerShell 默认禁止运行 `.ps1`，故涉及 npm 时统一用 `npm.cmd`。

## 6. 进度与待办

- 已完成：代码编写与静态检查；模拟 WebSocket 事件 8 项行为测试全部通过；NapCat 修复版启动器成功运行；机器人已与 NapCat 连通。
- 待办：在真实测试群中做端到端验收（`@机器人` 回复、普通消息/`@全体`/自身消息不回复）；确认 QQ 账号登录、群邀请等均就绪。
- 长期注意：QQ 客户端升级后 NapCat 可能需同步升级；用真实 QQ 登录存在风控/封号风险，建议机器人使用小号。

## 7. 参考

- `README.md`：面向用户的中文使用说明。
- NapCat 官方文档：<https://napneko.github.io/>；发布页：<https://github.com/NapNeko/NapCatQQ/releases>
- 对接方式参考项目：Miaoge-Ge/qq-llm-bot、kuliantnt/qq-maid-bot、MoXueYao/QQBot。
