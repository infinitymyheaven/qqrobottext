# QQ 群智能 DeepSeek 机器人（Python 版）

这是一个通过 NapCat（OneBot v11）接入 QQ 群、使用 DeepSeek 生成回复的机器人。它拥有模拟作息、群成员身份识别、有限聊天上下文、永久群资料和未来事项提醒。下面的介绍全是ai生成的，实用的介绍和部署方式还在锐意制作中。。。

## 主要能力

- 无需群友先与机器人说话：连接后主动同步白名单群的完整成员列表。
- 识别 QQ 号、昵称、群名片、群主、管理员、普通成员和专属头衔。
- 群友改名、身份变化或退群后保留历史记录，不受模型上下文长度影响。
- 每天 10:00–19:00 工作；包括 `@` 在内的候选消息都由多因素概率算法决定是否回答。
- 普通主动回复受每日随机上限和最小间隔保护；`@` 仍经过概率判断，但不受这两项限制。
- 自动识别未来事项、持久化保存并在到期前提醒。
- 普通群聊默认只保留最近五分钟的临时上下文，不永久保存全部聊天内容。

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

白名单只是允许机器人工作的范围。启动时程序还会读取机器人当前群列表：尚未加入或已经被移出的白名单群会被跳过，不会尝试发送问候或提醒，也不会因此反复断线重连。机器人加入群后重启程序即可识别。

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
| `active_window_all` | 配置的工作时段内全部白名单群文字消息 | 较高；日期关键词命中的消息会额外调用 DeepSeek |
| `participated` | 被 @ 或算法选中、机器人实际回复的消息 | 较低；可能漏掉机器人未参与的话题 |

程序先使用本地日期关键词过滤，只有疑似包含日期或时间的消息才调用 DeepSeek 提取，因此不会无条件分析每条消息。

## 作息与主动回复算法

- 回答时段默认 `[10:00, 19:00)`，支持 `HH:MM` 分钟级配置，默认时区 `Asia/Shanghai`。
- 工作开始后发送一次随机早安消息；工作结束后的 10 分钟内发送一次随机晚安消息。两者都可关闭或自定义模板。
- 回答时段内，普通文字和 `@` 都计算回答概率；回答时段外保持沉默，但继续学习成员活跃度和兴趣画像。
- 每个群每天从 `60–100` 中随机选取一个普通主动回复上限并持久化；普通消息还需满足最小发言间隔才计算概率，`@` 豁免这两项门槛。
- 算法把成员活跃度、群流量、成员与机器人话题熟悉度、互动关系、是否被 @、近期话题相关性、趣味度和随机量归一化后计算原始分：

```text
原始分 = 各基础因素加权和
       + @状态 × 话题熟悉度交互项
       + 成员活跃度 × 群活跃度交互项
回答概率 = sigmoid(6 × (原始分 - 0.65))
```

所有权重、Sigmoid 参数、画像半衰期和学习率都可在 `.env` 调整。算法使用固定 128 维字符特征，不需要额外模型或 NumPy。

每次白名单群内的其他成员发言时，PowerShell 都会输出一条 `发言判定` JSON 日志。日志包含各项配置权重、实时特征值、加权贡献、原始分、回答概率、是否发言及原因；工作时段外、达到上限和无文字内容也会记录。为避免常规日志复制聊天内容，日志不包含消息正文。

当天随机上限保存在 SQLite，重启不会改变。如果修改配置后旧上限不在新区间内，程序会为当天重新抽取。

## 永久资料与提醒

数据默认保存在 `data/bot_memory.sqlite3`：

- 启动、重连和每 6 小时通过 NapCat 全量同步群成员。
- 成员加入、退出、管理员或群名片变化后自动刷新对应群。
- 当前资料与变更历史均保留；退群成员标记为非活跃而不删除。
- 发言算法的成员活跃度、成员兴趣、机器人群话题偏好和互动关系使用规范化 SQLite 表长期保存；稀疏向量的每个非零维度均由 SQL 独立管理，不存为 JSON。
- AI 每次只读取当前发言者、群主/管理员、被 @ 或问题中明确提到的成员，匹配数量可配置，避免把大群名册塞进每次请求。
- 最近群聊默认保留 5 分钟、最多 20 条、每条最多 300 字；逐成员对话默认保留 10 条，闲置 30 分钟后清空。
- 未来事项默认提前 60 分钟发送普通文本提醒；若时间落在睡眠时段，会提前移动到最近的工作时段。
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

`deepseek-v4-flash` 通过 DeepSeek Responses API 使用服务端 `web_search`：普通问题由模型判断是否联网，明确要求搜索时强制联网核验。天气、新闻等实时信息和本地未来事项中没有记录的外部事件均可触发搜索。搜索来源只显示在运行机器的 PowerShell 日志中，不附加到 QQ 回复。

“现在几点”“当前时间”等本地时间问题直接使用机器人已经持有的带时区时钟回答，不调用 DeepSeek，也不依赖 `web_search`。这样既更快，也不会因为模型没有执行搜索工具而误报联网失败；询问其他地区时间等需要外部判断的问题仍交给模型处理。

关闭 `WEB_SEARCH_ENABLED` 后，群聊恢复使用 Chat Completions；未来事项的结构化提取始终使用 Chat Completions。兼容服务商若不支持 `/responses` 和 `web_search`，应关闭联网功能。真实密钥不得写入代码、文档或提交到 GitHub。

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
| `DEEPSEEK_TIMEOUT_SECONDS` | `60` | 单次 DeepSeek 请求超时 |
| `DEEPSEEK_MAX_TOKENS` | `1024` | 聊天回复最大生成 token 数 |
| `WEB_SEARCH_ENABLED` | `true` | 是否让群聊回答使用 Responses API 和服务端联网搜索 |
| `WEB_SEARCH_TIMEOUT_SECONDS` | `90` | 联网回答请求超时；必须大于 0 |
| `WEB_SEARCH_FAILURE_REPLY` | `我不知道，暂时没有查到可靠的联网信息。` | 联网失败、不完整或无法核验时的群内提示 |
| `WEB_SEARCH_LOG_SOURCES` | `true` | 是否在 PowerShell 中记录搜索来源，不影响群内回复 |
| `WEB_SEARCH_MAX_LOG_SOURCES` | `5` | 单次最多记录的来源数量；`0` 不打印来源 URL |
| `BOT_TIMEZONE` | `Asia/Shanghai` | 作息和提醒时区 |
| `ANSWER_START_TIME` / `ANSWER_END_TIME` | `10:00` / `19:00` | 分钟级工作时段；结束时间必须更晚，`24:00` 仅可用于结束 |
| `SPONTANEOUS_REPLIES_ENABLED` | `true` | 是否允许算法主动插话 |
| `SPONTANEOUS_DAILY_MIN` / `SPONTANEOUS_DAILY_MAX` | `60` / `100` | 每群每天随机主动回复上限区间 |
| `SPONTANEOUS_MIN_INTERVAL_SECONDS` | `600` | 主动回复最小间隔 |
| `SPONTANEOUS_TRAFFIC_WINDOW_SECONDS` | `60` | 流量评分统计窗口 |
| `SPONTANEOUS_TRAFFIC_FULL_SCORE_MESSAGES` | `10` | 流量项达到满分所需消息数 |
| `SPEAK_WEIGHT_USER_ACTIVITY` / `SPEAK_WEIGHT_GROUP_ACTIVITY` | `0.08` / `0.08` | 发言者长期活跃度和群内近期流量权重 |
| `SPEAK_WEIGHT_TOPIC_FAMILIARITY` / `SPEAK_WEIGHT_SOCIAL_BOND` | `0.12` / `0.12` | 长期话题熟悉度和成员互动关系权重 |
| `SPEAK_WEIGHT_IS_MENTIONED` / `SPEAK_WEIGHT_MESSAGE_RELEVANCE` | `1.0` / `0.12` | 被 @ 和当前消息与近期群话题相关度权重；默认让典型 @ 概率约为 90% |
| `SPEAK_WEIGHT_FUN_FACTOR` / `SPEAK_WEIGHT_RANDOM_NOISE` | `0.08` / `0.05` | 消息对话性和随机扰动权重 |
| `SPEAK_WEIGHT_INTERACTION_MENTIONED_TOPIC` | `0.15` | “被 @ × 话题熟悉度”交互项权重 |
| `SPEAK_WEIGHT_INTERACTION_USER_GROUP_ACTIVITY` | `0.05` | “成员活跃度 × 群流量”交互项权重 |
| `SPEAK_SIGMOID_K` / `SPEAK_SIGMOID_MIDPOINT` | `6.0` / `0.65` | 原始分转换为回答概率的曲线参数 |
| `SPEAK_USER_ACTIVITY_FULL_SCORE_MESSAGES` | `20` | 成员活跃度归一化尺度 |
| `SPEAK_ACTIVITY_HALF_LIFE_HOURS` / `SPEAK_BOND_HALF_LIFE_DAYS` | `24` / `30` | 活跃度和互动关系的时间衰减速度 |
| `SPEAK_BOND_LEARNING_RATE` / `SPEAK_INTEREST_LEARNING_RATE` | `0.10` / `0.20` | 关系及兴趣画像学习率，范围 0–1 |
| `SPEAK_RECENT_REPLY_WINDOW_SECONDS` / `SPEAK_RECENT_REPLY_FULL_COUNT` | `600` / `5` | 趣味度中机器人近期发言饱和度参数 |
| `MEMBER_SYNC_INTERVAL_SECONDS` | `21600` | 全量成员同步间隔 |
| `MEMBER_CONTEXT_MATCH_LIMIT` | `20` | 单次回答按名称等检索的成员上限 |
| `FUTURE_MEMORY_ENABLED` | `true` | 是否提取、注入和提醒未来事项 |
| `FUTURE_MEMORY_SOURCE` | `active_window_all` | 日期记忆来源模式 |
| `FUTURE_EXTRACTION_CONCURRENCY` | `2` | 日期候选消息调用 DeepSeek 的最大并发数 |
| `FUTURE_CONTEXT_MAX_EVENTS` | `20` | 单次回答注入的有效未来事项上限 |
| `REMINDER_LEAD_MINUTES` | `60` | 初次提醒提前分钟数 |
| `REMINDER_FOLLOWUP_MIN_MINUTES` / `REMINDER_FOLLOWUP_MAX_MINUTES` | `120` / `300` | 二次提醒随机等待区间 |
| `MEMORY_DB_PATH` | `data/bot_memory.sqlite3` | 本地数据库路径 |
| `GROUP_CONTEXT_WINDOW_SECONDS` | `300` | 最近群聊上下文时间窗口 |
| `GROUP_CONTEXT_MAX_MESSAGES` | `20` | 最近群聊注入条数上限，`0` 禁用 |
| `GROUP_CONTEXT_MESSAGE_MAX_CHARS` | `300` | 每条临时群消息保存字符数 |
| `CHAT_HISTORY_MESSAGES` | `10` | 每位成员对话历史条数，`0` 禁用 |
| `CHAT_HISTORY_TTL_MINUTES` | `30` | 对话历史闲置过期时间，`0` 表示永不过期到进程结束 |
| `MAX_REPLY_CHARS` | `2000` | QQ 单次回复最大字符数 |
| `MORNING_GREETING_ENABLED` / `NIGHT_GREETING_ENABLED` | `true` / `true` | 是否发送上下班问候 |
| `MORNING_GREETING_MESSAGES` / `NIGHT_GREETING_MESSAGES` | 内置三条模板 | 使用 `||` 分隔多条随机模板 |
| `EMPTY_MENTION_REPLY` / `AI_ERROR_REPLY` | 内置中文提示 | 空 @ 和 AI 故障时的回复文本 |
| `ERROR_LOG_ENABLED` | `true` | 是否启用只在报错时落盘的错误现场日志 |
| `ERROR_LOG_PATH` | `logs/error_context.txt` | 错误现场 txt 路径；目录会按需创建 |
| `ERROR_LOG_BEFORE_RECORDS` / `ERROR_LOG_AFTER_RECORDS` | `30` / `10` | 每次错误保存的前后状态条数 |
| `ERROR_LOG_MAX_BYTES` / `ERROR_LOG_BACKUP_COUNT` | `1048576` / `2` | 单文件空间上限与旧文件保留数量 |

布尔值可写 `true/false`、`1/0`、`yes/no` 或 `on/off`。配置使用严格校验：格式错误、范围倒置、无效时区或权重之和不为 1 时，程序会在连接 NapCat 前退出并指出变量名。`FUTURE_MEMORY_ENABLED=false` 会同时停止新事项提取、事项上下文注入和主动提醒。旧变量 `ANSWER_START_HOUR`、`ANSWER_END_HOUR` 和 `SPONTANEOUS_DAILY_LIMIT` 已废弃。

联网请求只带当前问题、有限对话历史和按需检索的本地资料，并会隐藏其中的 QQ 数字标识；不会发送完整群成员名单。DeepSeek 若没有返回 URL 注解，日志只说明执行过哪些搜索动作。联网功能会增加响应时间和 API 用量。

## 错误现场日志

控制台仍实时显示运行状态，但普通日志不会持续写入硬盘。程序只在出现 `ERROR` 或异常堆栈时创建 `logs/error_context.txt`，保存错误前后的有限日志以及进程、线程、Python 版本、运行时长和工作目录。文件达到配置的空间上限后自动轮换，最多保留指定数量的旧文件；`logs/` 已被 Git 忽略。

如果机器人正常运行且从未出现错误，看不到该 txt 文件属于正常现象。修改日志配置后需要重启机器人。

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
