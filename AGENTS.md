# AGENTS.md — QQ 群 DeepSeek 机器人项目速查

## 1. 项目概述

- 项目：`qqrobottext`，Python QQ 群聊机器人。
- 功能：群聊中被 `@机器人本人` 时，提取文字、调用 DeepSeek Chat Completions API，并把回复发回群聊。
- 不处理：普通群消息、`@全体成员`、私聊、主动发言、图片和文件输入。
- 架构：NapCat（OneBot v11 正向 WebSocket）+ 本项目 Python 客户端 + DeepSeek OpenAI 兼容 API。
- 依赖：仅 `websockets`；HTTP 请求使用 Python 标准库。Python 要求 3.10 以上。

## 2. 目录与运行

- `src/bot.py`：全部机器人、DeepSeek HTTP 客户端和入口逻辑。
- `tests/test_bot.py`：消息解析、API 请求格式、上下文隔离和群聊行为测试。
- `.env.example`：NapCat 与 DeepSeek 配置示例；真实 `.env` 已忽略。
- `README.md`：完整中文安装与使用文档。
- 本机运行：`.venv\Scripts\python.exe src\bot.py`
- 测试：`.venv\Scripts\python.exe -m unittest discover -s tests -v`
- Windows 系统 `python` 是商店占位符，本机操作一律使用项目 `.venv` 中的 Python。
- `.venv` 的基础解释器固定在 `D:\codex\python-runtimes\cpython-3.12.14-windows-x86_64-none\python.exe`，普通 PowerShell 可直接访问。不要改回 Codex 应用隔离的 `AppData\Roaming\uv` 路径。
- 旧的应用隔离环境临时保留为 `.venv-virtualized-old/` 且已被 Git 忽略；确认无需回退后可删除。

## 3. 配置

- `NAPCAT_WS_URL`：默认 `ws://127.0.0.1:3001`。
- `NAPCAT_WS_TOKEN`：默认空，通过 URL 的 `access_token` 参数传递。
- `DEEPSEEK_API_KEY`：必填，缺失时程序以状态码 2 退出。
- `DEEPSEEK_BASE_URL`：默认 `https://api.deepseek.com`。
- `DEEPSEEK_MODEL`：默认 `deepseek-v4-flash`，可为兼容服务商的模型名。
- `DEEPSEEK_SYSTEM_PROMPT`、`DEEPSEEK_TIMEOUT_SECONDS`、`DEEPSEEK_MAX_TOKENS` 可调。
- `CHAT_HISTORY_MESSAGES`：默认 10 条，`0` 关闭上下文。
- `MAX_REPLY_CHARS`：默认 2000 字符。

任何真实 API Key、NapCat WebUI Token 或 QQ 登录数据都不得写入文档或提交。

## 4. 实现要点

- `DeepSeekClient.chat()` 通过 `asyncio.to_thread` 执行标准库 HTTP 请求，避免阻塞 WebSocket 事件循环。
- 接口使用 Bearer Token，向 `{base_url}/chat/completions` 发送 OpenAI 兼容消息；若 Base URL 已包含完整路径则不重复拼接。
- `extract_message_text()` 只提取 OneBot 文本段；字符串格式会移除 CQ 码并反转义 HTML 实体。
- `QQBot._handle_group_message()` 仅接受群聊中对 `self_id` 的 @，忽略自身消息。
- 对话上下文以 `(group_id, user_id)` 为键隔离，每个会话用异步锁保持连续对话顺序。
- API 失败时回复固定友好提示，失败请求不会写入历史；回复过长会截断。
- `QQBot.run()` 断开或异常后每 3 秒自动连接 NapCat。
- `_read_loop()` 按 `echo` 匹配 OneBot 动作响应，群事件使用独立任务处理。

## 5. 本机环境事实

- QQ：`D:\Program Files\Tencent\QQNT\QQ.exe`，版本 `9.9.19.35184`。
- NapCat：本地目录 `qq/` 已被 Git 忽略；官方启动器无法探测 QQ，需管理员运行 `qq/napcat/launcher-fixed.bat`。
- OneBot 配置：`127.0.0.1:3001`、数组消息格式、Token 空、心跳 30 秒。
- 2026-09-09 重新验证时 NapCat 已能在 `127.0.0.1:3001` 接受连接；真实端到端测试时仍需保持其控制台进程运行。
- `qq/napcat/config/webui.json` 含敏感 Token，不得读取后输出或提交。
- GitHub 远端：`https://github.com/infinitymyheaven/qqrobottext`，公开仓库，分支 `main`。
- GitHub 网络使用仓库既有代理配置。

## 6. 当前进度与待办

- 已完成：Node.js 到 Python 重构；DeepSeek API 聊天接入；按群成员隔离的有限上下文；8 项模拟 API/行为测试；配置与 README；本地 `.env` 已由用户填写且保持忽略状态；新 `.venv` 已成功启动并连接 NapCat。
- 待办：在真实 QQ 群完成端到端聊天验收。
- 端到端检查：@机器人能回答；连续追问能读取上下文；普通消息、@全体、私聊和自身消息不回复；错误密钥能返回友好提示。

## 7. 安全与版本控制

- `.env`、`qq/`、`.venv/` 均已忽略；提交前仍需检查密钥未进入 diff。
- 使用普通 QQ 登录存在风控和封号风险，建议小号。
- DeepSeek API 会产生用量和费用，测试时注意账户余额与请求频率。
- 新改动验证后再按用户明确要求提交和推送，不自动扩大远端写入范围。
