# QQ 群智能 DeepSeek 机器人（Python 版）

这是一个通过 NapCat（OneBot v11）接入 QQ 群、使用 DeepSeek 生成回复的机器人。它拥有模拟作息、群成员身份识别、有限聊天上下文、永久群资料和未来事项提醒。

## 主要能力

- 无需群友先与机器人说话：连接后主动同步白名单群的完整成员列表。
- 识别 QQ 号、昵称、群名片、群主、管理员、普通成员和专属头衔。
- 群友改名、身份变化或退群后保留历史记录，不受模型上下文长度影响。
- 每天 10:00–19:00 工作；被 `@` 必答，普通消息按智能权重决定是否加入聊天。
- 每个白名单群每天最多约 50 条主动回复，消息不足时允许少于 50 条。
- 自动识别未来事项、持久化保存并在到期前提醒。
- 普通群聊只保留最近一分钟的临时上下文，不永久保存全部聊天内容。

## 安全默认值：必须填写群白名单

复制环境变量示例：

```powershell
Copy-Item .env.example .env
```

在 `.env` 中填写机器人可以活动的 QQ 群号，多个群使用英文逗号：

```dotenv
ACTIVE_GROUP_IDS=123456789,987654321
```

`ACTIVE_GROUP_IDS` 留空时，机器人仍可连接 NapCat，但**不会同步群资料、回复、问候或提醒**，避免意外在其他群发言。

## 未来事项记忆模式（重要）

`.env` 中的 `FUTURE_MEMORY_SOURCE` 可以在两套已实现逻辑间切换，无需修改源码：

```dotenv
# 默认：分析回答时段内白名单群的全部文字消息
FUTURE_MEMORY_SOURCE=active_window_all

# 低成本模式：只分析机器人实际参与回复的消息
# FUTURE_MEMORY_SOURCE=participated
```

两种模式的区别：

| 模式 | 覆盖范围 | API 成本和隐私影响 |
| --- | --- | --- |
| `active_window_all` | 10:00–19:00 的全部白名单群文字消息 | 较高；日期关键词命中的消息会额外调用 DeepSeek |
| `participated` | 被 @ 或算法选中、机器人实际回复的消息 | 较低；可能漏掉机器人未参与的话题 |

程序先使用本地日期关键词过滤，只有疑似包含日期或时间的消息才调用 DeepSeek 提取，因此不会无条件分析每条消息。

## 作息与主动回复算法

- 回答时段：`[10:00, 19:00)`，默认时区 `Asia/Shanghai`。
- 10:00 发送一次随机早安消息；19:00 发送一次随机晚安消息。
- 回答时段内被 @ 必定回答；回答时段外即使被 @ 也保持沉默。
- 普通消息至少距离机器人上次发言 600 秒，且当天未达到 50 条上限，才计算：

```text
得分 = 0.40 × 随机数
     + 0.25 × min(过去60秒消息数 / 10, 1)
     + 0.35 × min(距机器人上次发言秒数 / 1200, 1)
```

得分达到 `0.65` 才主动回复。任何机器人消息，包括 @ 回复、问候和提醒，都会重新计算沉默时间。

## 永久资料与提醒

数据默认保存在 `data/bot_memory.sqlite3`：

- 启动、重连和每 6 小时通过 NapCat 全量同步群成员。
- 成员加入、退出、管理员或群名片变化后自动刷新对应群。
- 当前资料与变更历史均保留；退群成员标记为非活跃而不删除。
- AI 每次只读取当前发言者、群主/管理员、被 @ 或问题中明确提到的成员，避免把大群名册塞进每次请求。
- 未来事项提前 60 分钟发送普通文本提醒；若时间落在睡眠时段，会提前移动到最近的工作时段。
- 提醒后，只要事项来源群友在同群发过任意消息，就视为已确认。
- 未确认时会随机等待 2–5 小时准备二次 @；事项已经过期则取消二次提醒。

`data/`、`.env`、QQ 数据和虚拟环境均被 Git 忽略。数据库包含群成员资料，备份或分享项目时不要复制该目录。

## 安装

环境要求：Python 3.10+、NapCat、DeepSeek API Key。项目依赖 `websockets` 和 Windows 所需的 IANA 时区数据 `tzdata`。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

本机已配置固定 Python 运行时时，直接使用现有 `.venv`，不要重新创建。

## 配置 NapCat

按照 [NapCat 官方文档](https://napneko.github.io/guide/boot/Shell)登录机器人 QQ，并在 WebUI 新建 OneBot v11 正向 WebSocket 服务：

- 主机：`127.0.0.1`
- 端口：`3001`
- 消息格式：`array`
- Access Token：可留空；填写时需同步到 `.env`

机器人通过以下 OneBot 动作读取资料：`get_group_list`、`get_group_member_list`；通过 `send_group_msg` 发言。

## DeepSeek 配置

在 `.env` 中至少填写：

```dotenv
DEEPSEEK_API_KEY=你的真实API密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

兼容服务商可以替换接口地址和模型名。真实密钥不得写入代码、文档或提交到 GitHub。

## 完整配置表

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ACTIVE_GROUP_IDS` | 空 | 必填群号白名单；空值禁用全部机器人行为 |
| `NAPCAT_WS_URL` | `ws://127.0.0.1:3001` | NapCat WebSocket 地址 |
| `NAPCAT_WS_TOKEN` | 空 | NapCat Access Token |
| `DEEPSEEK_API_KEY` | 无 | DeepSeek API Key，必填 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 根地址或完整聊天接口 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 模型名称 |
| `DEEPSEEK_SYSTEM_PROMPT` | 中文群聊助手提示词 | 机器人角色设定 |
| `BOT_TIMEZONE` | `Asia/Shanghai` | 作息和提醒时区 |
| `ANSWER_START_HOUR` / `ANSWER_END_HOUR` | `10` / `19` | 回答时间小时边界 |
| `SPONTANEOUS_DAILY_LIMIT` | `50` | 每个群每日主动回复上限 |
| `SPONTANEOUS_MIN_INTERVAL_SECONDS` | `600` | 主动回复最小间隔 |
| `SPONTANEOUS_SCORE_THRESHOLD` | `0.65` | 主动回复分数阈值 |
| `MEMBER_SYNC_INTERVAL_SECONDS` | `21600` | 全量成员同步间隔 |
| `FUTURE_MEMORY_SOURCE` | `active_window_all` | 日期记忆来源模式 |
| `REMINDER_LEAD_MINUTES` | `60` | 初次提醒提前分钟数 |
| `MEMORY_DB_PATH` | `data/bot_memory.sqlite3` | 本地数据库路径 |
| `CHAT_HISTORY_MESSAGES` | `10` | 每位成员的有限对话上下文 |
| `MAX_REPLY_CHARS` | `2000` | QQ 单次回复最大字符数 |

## 启动与测试

先启动 NapCat，再运行：

```powershell
.venv\Scripts\python.exe src\bot.py
```

运行自动测试（使用临时数据库和模拟 API，不消耗 DeepSeek 额度）：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 许可与风险

自研代码采用 [MIT License](LICENSE)。NapCat 是非官方 QQ 协议实现，存在账号风控或封禁风险，建议使用不重要的小号。群资料和消息分析也涉及成员隐私，请只在得到群成员知情同意的群中启用。
