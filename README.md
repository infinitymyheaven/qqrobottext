# QQ 群聊 DeepSeek 机器人（Python 版）

当群成员在 QQ 群里 `@机器人` 并输入问题时，机器人会把纯文本问题交给 DeepSeek，并将模型回复发回群聊。

## 工作方式

- NapCat 登录机器人 QQ，并提供 OneBot v11 正向 WebSocket 服务。
- 本项目连接 NapCat，只监听群聊中对机器人本人的 `@`。
- 程序调用 DeepSeek 的 OpenAI 兼容 `chat/completions` API，再通过 `send_group_msg` 回复。
- 对话历史按“群号 + 用户 QQ”隔离，默认保存最近 10 条消息；程序重启后清空。
- 普通群消息、`@全体成员`、私聊和机器人自身消息均不响应。
- 目前只把文字交给模型，图片、文件、表情等非文本消息段会被忽略。

项目仅依赖 `websockets`，DeepSeek 请求使用 Python 标准库发送，无需安装 OpenAI SDK。

## 环境要求

- Python 3.10 或更高版本
- 已安装并登录的 NapCat
- 一个 DeepSeek API Key
- 用于测试的 QQ 小号和 QQ 群

> NapCat 属于非官方 QQ 协议实现，账号可能遭遇风控或封禁。建议只使用不重要的小号。

## 1. 配置 NapCat

参照 [NapCat 官方文档](https://napneko.github.io/guide/boot/Shell)完成安装和 QQ 登录，然后在 WebUI 的“网络配置”中新建并启用 OneBot v11 WebSocket 服务器：

- 主机：`127.0.0.1`
- 端口：`3001`
- 消息上报格式：`array`
- Access Token：可留空；如果填写，必须与 `.env` 中的值一致

将机器人 QQ 拉入测试群。Windows 本机已配置过 NapCat 时，可按 `AGENTS.md` 中记录的本机启动说明运行。

## 2. 安装项目

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

macOS / Linux 使用 `.venv/bin/python`。

## 3. 填写 DeepSeek API Key

打开项目根目录中新建的 `.env`，至少填写：

```dotenv
DEEPSEEK_API_KEY=你的真实API密钥
```

`.env` 已被 Git 忽略，不会正常提交到仓库。不要把真实密钥写入代码、聊天截图或公开日志。

默认使用 DeepSeek 官方接口和当前快速模型：

```dotenv
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

如果你的现成 API 来自兼容服务商，请按服务商说明修改这两个值。`DEEPSEEK_BASE_URL` 可以填写 API 根地址，也可以直接填写以 `/chat/completions` 结尾的完整地址。

## 4. 启动并聊天

先启动 NapCat，再在项目根目录运行：

```powershell
.venv\Scripts\python.exe src\bot.py
```

看到“已连接 NapCat OneBot WebSocket”后，在群里发送：

```text
@机器人 你好，请介绍一下你自己
```

机器人会把 DeepSeek 的回答发回群聊。只发送 `@机器人` 而没有文字时，它会提示你输入内容。

## 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NAPCAT_WS_URL` | `ws://127.0.0.1:3001` | NapCat 正向 WebSocket 地址 |
| `NAPCAT_WS_TOKEN` | 空 | NapCat Access Token |
| `DEEPSEEK_API_KEY` | 无 | DeepSeek API Key，必填 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 根地址或完整聊天接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 模型名称 |
| `DEEPSEEK_SYSTEM_PROMPT` | 中文群聊助手提示词 | 机器人角色设定 |
| `DEEPSEEK_TIMEOUT_SECONDS` | `60` | 单次模型请求超时秒数 |
| `DEEPSEEK_MAX_TOKENS` | `1024` | 模型单次最大输出 token 数 |
| `CHAT_HISTORY_MESSAGES` | `10` | 每位群成员保留的历史消息条数；`0` 表示关闭上下文 |
| `MAX_REPLY_CHARS` | `2000` | 发回 QQ 的最大字符数，超出时截断 |

## 测试

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用模拟 DeepSeek 响应，不会消耗 API 额度，也不要求 NapCat 正在运行。

## 常见问题

- **启动时提示缺少 `DEEPSEEK_API_KEY`**：确认已经把 `.env.example` 复制为 `.env`，并填写了真实密钥。
- **机器人回复“AI 服务暂时不可用”**：查看程序控制台中的 HTTP 状态码；重点检查密钥、余额、模型名和接口地址。
- **连接不上 NapCat**：确认 NapCat 已运行、WebSocket 配置已启用，端口和 Token 与 `.env` 一致。
- **@ 后不回复**：确认 @ 的是机器人本人而不是 `@全体成员`，并确认 NapCat 的消息格式为 `array`。
- **上下文没有保留**：历史仅保存在内存中，按群和用户隔离，程序重启后会清空。

## 参考

- [DeepSeek API 首次调用](https://api-docs.deepseek.com/)
- [DeepSeek 多轮对话](https://api-docs.deepseek.com/guides/multi_round_chat)
- [NapNeko/NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- [minecraft-dzy/napcat-ai-tools](https://github.com/minecraft-dzy/napcat-ai-tools)
- [WvvDongmo/QQbot-for-personal-use](https://github.com/WvvDongmo/QQbot-for-personal-use)

## 许可证

自研代码采用 [MIT License](LICENSE)。NapCat 不随本仓库分发，并适用其自身许可证与使用风险。
