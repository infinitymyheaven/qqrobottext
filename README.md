# QQ 群智能 DeepSeek 机器人（Python 版）

这是一个通过 NapCat（OneBot v11）接入 QQ 群、使用 DeepSeek 生成回复的机器人。它拥有模拟作息、群成员身份识别、有限聊天上下文、永久群资料和未来事项提醒。下面的介绍全是ai生成的，实用的介绍和部署方式还在锐意制作中。。。

## 主要能力

- 无需群友先与机器人说话：连接后主动同步白名单群的完整成员列表。
- 识别 QQ 号、昵称、群名片、群主、管理员、普通成员和专属头衔。
- 群友改名、身份变化或退群后保留历史记录，不受模型上下文长度影响。
- 每天 10:00–19:00 工作；包括 `@` 在内的候选消息都由多因素概率算法决定是否回答。
- 独立意愿模块维护每群三小时消息流；普通消息与 `@` 都经过概率判断并共享每日 500 条上限。
- 结构化人格内容模块学习模板账号的语气、句式、幽默、互动习惯和常用短语，并通过可回退版本控制影响 DeepSeek 的回答方式。
- 内置“对话基本要求”硬约束，让聊天回答保持日常、简短、口语化的纯文本，并拒绝改变机器人身份、提示词或运行规则的请求。
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

## 作息与消息流回复意愿

- 回答时段默认 `[10:00, 19:00)`，支持 `HH:MM` 分钟级配置，默认时区 `Asia/Shanghai`。
- 工作开始后发送一次随机早安消息；工作结束后的 10 分钟内发送一次随机晚安消息。两者都可关闭或自定义模板。
- 回答时段内，普通文字和 `@` 都计算回答概率；回答时段外固定不回复，但仍记录消息、更新关系并参与低频话题分析。
- `src/reply_willingness.py` 为每个群保存最多 500 条、最长三小时的内存消息流，每五秒刷新一次环境快照。普通消息没有 600 秒硬间隔，机器人回复后会短期降温，随后按时间和新消息数量恢复。
- 每群每天最多发送 500 条由意愿算法批准的消息，包括 `@` 回复、空 `@` 提示和 API 失败提示；问候与事项提醒不计入。
- 算法把固定用户活跃度、群整体活跃度、长期话题熟悉度、社会关系、是否被 @、消息与个人背景相关性、趣味度和随机量统一归一化后计算原始分：

```text
原始分 = 八项参数的“特征值 × 权重”之和
回答概率 = sigmoid(6 × (原始分 - 0.65))
```

群活跃度中最近十分钟密度占 80%，十分钟前至三小时的密度占 20%，并考虑近期参与人数。`@` 权重很高，但仍经过概率抽样，不保证回答。

每次白名单群内的其他成员发言时，PowerShell 都会立即输出一条 `INFO 意愿计算` JSON 日志。日志包含消息流统计、八项特征、八项权重、逐项贡献、命中话题、原始分、概率、抽样随机数、结果、原因和当日剩余额度；它在调用 DeepSeek 前输出，且不包含消息正文、个人背景、密钥或 Token。

话题知识每群按 `10小时 - 7小时 × 群活跃度` 的间隔低频更新，严格限制在 3–10 小时。第一阶段只向 DeepSeek 发送匿名编号后的消息，第二阶段只将公开主题词交给 `web_search`；群聊原文、姓名和 QQ 号不会进入联网检索。话题按讨论热度保存为核心、次要和边缘三级。

## 永久资料与提醒

数据默认保存在 `data/bot_memory.sqlite3`：

- 启动、重连和每 6 小时通过 NapCat 全量同步群成员。
- 成员加入、退出、管理员或群名片变化后自动刷新对应群。
- 当前资料与变更历史均保留；退群成员标记为非活跃而不删除。
- 话题知识、每群话题热度、结构化人格版本、社会关系和每日算法回复数使用规范化 SQLite 表长期保存；普通消息流本身只在内存保存，重启可丢失。
- 模板账号通过本机 `.env` 的 `PERSONA_USER_ID` 设置；旧 `WILLINGNESS_PERSONA_USER_ID` 仅保留兼容。目标 QQ 号不要写入代码或提交。
- 当前激活的人格版本同时提供意愿相关性向量和回答内容因子。机器人高相似地借鉴表达习惯和兴趣，但不冒充模板账号、不代替本人表态、不透露采集资料，也不会把群主或任何成员称为“主人”。
- 群友 `@` 或引用机器人时，关系向 1 靠近 12%；机器人成功回复时向 1 靠近 8%。一天内不衰减，之后按指数曲线遗忘，在约 30 天归零。
- AI 每次只读取当前发言者、群主/管理员、被 @ 或问题中明确提到的成员，匹配数量可配置，避免把大群名册塞进每次请求。
- 最近群聊默认保留 5 分钟、最多 20 条、每条最多 300 字；逐成员对话默认保留 10 条，闲置 30 分钟后清空。
- 未来事项默认提前 60 分钟发送普通文本提醒；若时间落在睡眠时段，会提前移动到最近的工作时段。
- 提醒后，只要事项来源群友在同群发过任意消息，就视为已确认。
- 未确认时会随机等待 2–5 小时准备二次 @；事项已经过期则取消二次提醒。

`data/`、`.env`、QQ 数据和虚拟环境均被 Git 忽略。数据库包含群成员资料，备份或分享项目时不要复制该目录。

## 对话基本要求

每次聊天回答都会自动加入不可关闭的第二内容指标。它要求模型默认使用一到两句日常口语；问题复杂或明确要求详细时可以自然展开，但仍只输出一个纯文本正文块，不使用标题、列表、编号、前缀、引号、括号、`@`、表情或 Markdown，也不主动解释人格背景、提示词和运行机制。普通中文标点不受影响。

聊天历史、本地资料和人格样例全部作为只读数据。有人直接要求机器人忽略规则、改变身份、泄露提示词或覆盖工具行为时，原始越权文字不会进入回答模型或联网搜索；模型会在当前人格及其他内容指标影响下自然拒绝。引用、解释、翻译和创作类似句子仍可正常回答。

模型第一次输出越界时，程序会携带全部内容因子进行一次无联网改写；第二次仍越界时只做最小本地格式清理。这个过程不限制复杂问题的固定字数，也不会改写早晚安、事项提醒、空消息提示或故障提示。

## 结构化人格采集与版本管理

常驻机器人只能持续看到双方共同白名单群里的模板账号新发言。如需使用更丰富的历史，在另一个 NapCat 实例中由账号本人扫码登录，并把正向 WebSocket 端口设为 `3002`。不要把 QQ 密码发给程序、AI 或写入 `.env`。

采集器连接后先核对登录账号，再在本机终端列出群聊和好友供你选择。默认尽力扫描最近 90 天、最多 20000 条模板账号文字，并为每条本人发言保留此前最多三条、此后一条脱敏上下文。完整原文只存在于当前内存批次；DeepSeek 只收到去标识文本，不使用 `web_search`，SQLite 只保存结构化特征、短脱敏样例和断点统计。

```powershell
# 生成草稿；Token 会在终端中隐藏输入。
.venv\Scripts\python.exe src\persona_collector.py collect

# 查看版本并生成 20 组新旧回复盲测。
.venv\Scripts\python.exe src\persona_collector.py list
.venv\Scripts\python.exe src\persona_collector.py compare 2

# 盲测确认后激活；也可回退或彻底删除派生数据。
.venv\Scripts\python.exe src\persona_collector.py activate 2
.venv\Scripts\python.exe src\persona_collector.py rollback
.venv\Scripts\python.exe src\persona_collector.py delete
```

采集器生成的版本默认是 `draft`，不会影响线上机器人。激活或回退后重启机器人生效。NapCat 历史受本机缓存和版本差异影响，命令会报告实际覆盖范围及重复页、离线缺口等情况；`--restart` 可忽略旧断点重新采集。

人格与话题提炼使用 DeepSeek JSON Output，并显式关闭思考模式，把输出额度留给最终结构化结果。若接口偶发返回空内容或 JSON 被截断，程序会自动重试三次并逐次增加输出额度；日志只记录完成原因和是否出现思考内容，不记录聊天证据或模型原文。

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

机器人通过 `get_group_list`、`get_group_member_list`、`get_group_msg_history` 和 `get_msg` 读取所需资料，通过 `send_group_msg` 发言并保存返回的消息 ID，以识别后续引用。

## DeepSeek 配置

在 `.env` 中至少填写：

```dotenv
DEEPSEEK_API_KEY=你的真实API密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

`deepseek-v4-flash` 通过 DeepSeek Responses API 使用服务端 `web_search`：普通稳定知识问题由模型判断是否联网；明确要求搜索，或者询问天气、新闻、最新版本、实时行情、比赛结果和非本地时间等信息时强制联网核验。本地未来事项中没有记录的外部事件也可触发搜索。搜索来源只显示在运行机器的 PowerShell 日志中，不附加到 QQ 回复。

DeepSeek V4 偶发会把内部 DSML 工具标记误放进回答正文。机器人会在发送前拦截这类内容；明确联网时使用只含搜索工具的 `tool_choice=required`，兼容重试会改用版本化搜索工具并关闭思考。若 Responses 服务端仍然只返回普通文本，默认会自动使用同一 API Key 切换到 DeepSeek Anthropic Messages 协议执行服务端搜索，无需额外的搜索服务或密钥。标准搜索调用、服务端搜索结果或 URL 引用都可作为已联网证据；若仍无证据，只发送联网失败提示，绝不会把 DSML 协议文字发到群里。

默认 `DEEPSEEK_SYSTEM_PROMPT` 将机器人定义为群内平等、自然且有分寸的群友。每次 AI 回答会在事实资料之外单独注入当前激活的结构化人格内容因子；普通 Chat Completions 与联网 Responses 两条路径共用人格和对话要求。对话要求是最终硬约束，不能被 `.env` 提示词、人格、资料或聊天消息覆盖，内容因子也不得进入搜索词。

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
| `DEEPSEEK_MAX_TOKENS` | `4096` | 聊天回复最大生成 token 数 |
| `WEB_SEARCH_ENABLED` | `true` | 是否让群聊回答使用 Responses API 和服务端联网搜索 |
| `WEB_SEARCH_TIMEOUT_SECONDS` | `90` | 联网回答请求超时；必须大于 0 |
| `WEB_SEARCH_ANTHROPIC_FALLBACK_ENABLED` | `true` | Responses 忽略搜索时，是否用同一 DeepSeek Key 自动切换 Anthropic 协议 |
| `WEB_SEARCH_MAX_USES` | `3` | 单次 Anthropic 联网最多搜索次数，可设为 `1`–`10` |
| `WEB_SEARCH_FAILURE_REPLY` | `我不知道，暂时没有查到可靠的联网信息。` | 联网失败、不完整或无法核验时的群内提示 |
| `WEB_SEARCH_LOG_SOURCES` | `true` | 是否在 PowerShell 中记录搜索来源，不影响群内回复 |
| `WEB_SEARCH_MAX_LOG_SOURCES` | `5` | 单次最多记录的来源数量；`0` 不打印来源 URL |
| `BOT_TIMEZONE` | `Asia/Shanghai` | 作息和提醒时区 |
| `ANSWER_START_TIME` / `ANSWER_END_TIME` | `10:00` / `19:00` | 分钟级工作时段；结束时间必须更晚，`24:00` 仅可用于结束 |
| `SPONTANEOUS_REPLIES_ENABLED` | `true` | 是否允许算法主动插话 |
| `WILLINGNESS_MESSAGE_LIMIT` / `WILLINGNESS_MESSAGE_MAX_AGE_SECONDS` | `500` / `10800` | 每群内存消息流条数和三小时时间边界 |
| `WILLINGNESS_UPDATE_SECONDS` / `WILLINGNESS_DAILY_REPLY_LIMIT` | `5` / `500` | 环境刷新周期和每群每日算法回复总上限 |
| `WILLINGNESS_USER_ACTIVITY` | `0.5` | 八参数中的固定用户活跃度，范围 0–1 |
| `WILLINGNESS_SHORT_WINDOW_SECONDS` | `600` | 群活跃度的近期主窗口 |
| `WILLINGNESS_SHORT_FULL_MESSAGES` / `WILLINGNESS_OLD_FULL_MESSAGES` | `30` / `120` | 近期和较早消息密度达到满分的尺度 |
| `WILLINGNESS_TOPIC_ANALYSIS_MIN_HOURS` / `WILLINGNESS_TOPIC_ANALYSIS_MAX_HOURS` | `3` / `10` | 每群 AI 话题分析间隔边界 |
| `PERSONA_USER_ID` | 空 | 模板账号；只填写到本机 `.env`，旧 `WILLINGNESS_PERSONA_USER_ID` 仅兼容 |
| `WILLINGNESS_PERSONAL_BACKGROUND` | 内置通用背景 | 无历史资料时使用的本地相关性种子 |
| `PERSONA_CONTENT_ENABLED` / `PERSONA_GROUP_STYLE_WEIGHT` | `true` / `0.70` | 是否向 AI 注入人格内容因子，以及群聊相对私聊的风格权重 |
| `PERSONA_INCREMENT_MIN_MESSAGES` | `50` | 共同白名单群内累计到多少条模板发言后立即更新人格 |
| `PERSONA_INCREMENT_MAX_HOURS` / `PERSONA_INCREMENT_FLOOR_MESSAGES` | `24` / `10` | 未达到立即更新阈值时的最长等待时间和最少消息数 |
| `WILLINGNESS_HISTORY_DAYS` | `30` | 首次个人背景历史时间范围 |
| `WILLINGNESS_HISTORY_MESSAGE_LIMIT` / `WILLINGNESS_HISTORY_SCAN_LIMIT` | `2000` / `10000` | 最多收集的本人消息数和最多扫描的源消息数 |
| `WILLINGNESS_BOND_INBOUND_RATE` / `WILLINGNESS_BOND_OUTBOUND_RATE` | `0.12` / `0.08` | 群友与机器人双向互动时关系向 1 靠近的比例 |
| `WILLINGNESS_BOND_GRACE_HOURS` / `WILLINGNESS_BOND_ZERO_DAYS` | `24` / `30` | 关系遗忘宽限期和归零边界 |
| `WILLINGNESS_REPLY_COOLDOWN_SECONDS` | `120` | 回复后群级意愿按时间恢复至正常的最长时间；新消息可加速恢复 |
| `SPEAK_WEIGHT_USER_ACTIVITY` / `SPEAK_WEIGHT_GROUP_ACTIVITY` | `0.08` / `0.08` | 固定用户活跃度和群消息密度权重 |
| `SPEAK_WEIGHT_TOPIC_FAMILIARITY` / `SPEAK_WEIGHT_SOCIAL_BOND` | `0.12` / `0.12` | 长期话题熟悉度和成员互动关系权重 |
| `SPEAK_WEIGHT_IS_MENTIONED` / `SPEAK_WEIGHT_MESSAGE_RELEVANCE` | `1.0` / `0.12` | 被 @ 和当前消息与个人背景相关度权重；典型 @ 概率约为 90% |
| `SPEAK_WEIGHT_FUN_FACTOR` / `SPEAK_WEIGHT_RANDOM_NOISE` | `0.08` / `0.05` | AI 情绪/话题回复热度和随机扰动权重 |
| `SPEAK_SIGMOID_K` / `SPEAK_SIGMOID_MIDPOINT` | `6.0` / `0.65` | 原始分转换为回答概率的曲线参数 |
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

布尔值可写 `true/false`、`1/0`、`yes/no` 或 `on/off`。配置使用严格校验：格式错误、范围倒置、无效时区或非法权重会让程序在连接 NapCat 前退出并指出变量名。`FUTURE_MEMORY_ENABLED=false` 会同时停止新事项提取、事项上下文注入和主动提醒。旧变量 `ANSWER_START_HOUR`、`ANSWER_END_HOUR`、`SPONTANEOUS_DAILY_LIMIT`、`SPONTANEOUS_DAILY_MIN/MAX`、`SPONTANEOUS_MIN_INTERVAL_SECONDS`、旧流量窗口及两个交互项均已废弃。

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
