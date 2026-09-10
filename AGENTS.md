# AGENTS.md — QQ 群智能 DeepSeek 机器人接手指南

## 1. 项目定位与当前架构

- 项目名 `qqrobottext`，Python 3.10+。NapCat OneBot v11 正向 WebSocket 接收 QQ 群事件，本项目负责状态、意愿决策和调度，DeepSeek 负责聊天、低频知识提炼及联网搜索。
- 当前主分支已经把回复意愿拆到 `src/reply_willingness.py`；`src/bot.py` 不应再次出现一套平行概率公式。
- 群聊回答在 `WEB_SEARCH_ENABLED=true` 时使用 Responses API；普通请求 `tool_choice=auto`，只有明确联网请求才强制 `web_search`。本地时间问题直接用传入的带时区时钟回答。
- 群聊 Chat Completions/Responses 的默认输出上限由 `DEEPSEEK_MAX_TOKENS=4096` 控制；话题联网丰富当前也使用 `max_output_tokens=4096`，避免搜索和思考 token 挤占最终 JSON。未来事项、人格和话题本地提取仍使用各自的结构化 JSON 额度与安全重试，不要误绑到聊天额度。
- 自动测试使用临时 SQLite、模拟 WebSocket 和模拟 DeepSeek，绝不连接真实 QQ 或消耗 API 额度。
- 开始工作前先执行 `git fetch origin` 并检查当前分支、远端头和工作区，不要假定提交状态。

## 2. 代码地图与本机命令

- `src/bot.py`：严格读取 `.env`、DeepSeek HTTP 协议、OneBot 消息标准化、群权限/工作时间、上下文、消息发送、历史冷启动、问候和事项提醒。
- `src/reply_willingness.py`：每群消息流、五秒环境快照、八参数概率、实时决策日志、关系变化、话题/个人背景分析调度。
- `src/persona.py`：统一的 `ContentFactor` 接口、结构化人格 schema、隐私清洗、批次合并和在线人格指导块。
- `src/persona_collector.py`：第二 NapCat 本机交互采集、断点、版本列表/盲测/激活/回退/删除；绝不处理 QQ 密码。
- `src/memory.py`：SQLite 建表/无损迁移、成员资料、未来事项、活动、话题知识、个人背景、关系和每日算法回复数。
- `src/error_logging.py`：内存环形状态缓冲；只有 ERROR 才把有限前后文写入 `logs/error_context.txt`。
- `tests/test_reply_willingness.py`：消息流、八参数、日志安全、额度、冷却、遗忘曲线、话题层级和分析间隔。
- `tests/test_bot.py` / `tests/test_memory.py`：OneBot/DeepSeek 集成、作息、提醒、配置、SQLite 兼容测试。
- `tests/test_persona.py`：人格脱敏、结构化合并、版本迁移、采集断点和会话选择测试。
- `.env.example` 是配置名与默认值的权威模板；新增或删除配置必须同步 README 表格与配置测试。

Windows 命令：

```powershell
cd D:\codex\qqrobot
.\.venv\Scripts\python.exe src\bot.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

- 不要调用系统商店的 `python`。项目 `.venv` 的基础解释器位于 `D:\codex\python-runtimes\cpython-3.12.14-windows-x86_64-none\python.exe`。
- 先以管理员身份运行 `qq\napcat\launcher-fixed.bat` 并登录 QQ，再运行机器人；修改 `.env` 后必须重启。

## 3. 独立回复意愿模块不变量

每个白名单群维护最多 `WILLINGNESS_MESSAGE_LIMIT=500` 条、最长 `WILLINGNESS_MESSAGE_MAX_AGE_SECONDS=10800` 秒的内存消息流，包含群友和机器人发言。环境每五秒刷新；没有新决策时不得重复刷日志。

八项特征都必须在 0–1：

1. `user_activity`：`.env` 固定值，默认 0.5。
2. `group_activity`：最近十分钟密度占 80%，十分钟前至三小时占 20%，再考虑近期发言人数。
3. `topic_familiarity`：消息流与 SQLite 长期知识中最相关的三个话题的加权熟悉度。
4. `social_bond`：当前发言者占 60%，近期参与者时间窗口关系占 40%。
5. `is_mentioned`：仅 0/1；默认权重 1.0，很强但不保证回复。
6. `message_relevance`：当前文字与配置目标账号个人背景的匹配度。
7. `fun_factor`：`0.65 × 情绪强度 + 0.35 × log` 归一化话题回复数。
8. `random_noise`：可注入 RNG，测试必须固定。

```text
raw_score = Σ(feature × weight)
probability = sigmoid(k × (raw_score - midpoint))
will_reply = random_draw < probability
```

- 不得恢复两个旧交互项、600 秒硬间隔、随机 60–100 上限、动态成员活跃度或旧流量公式。
- 每群每日最多 `WILLINGNESS_DAILY_REPLY_LIMIT=500` 条算法回复；普通消息、`@`、空 `@` 提示和 API 错误提示都计数。问候和事项提醒不计数。
- 回复后群级意愿降温，按时间和机器人回复后的新消息数恢复，防止连续抢话。
- 工作时段外仍记录消息、更新互动关系、参与话题分析，但 `decide()` 固定返回不回复。
- 同群“记录 → 评分 → 发送 → 成功计数”必须持有 `_group_reply_locks[group_id]`。同一成员对话使用 `_conversation_locks[(group_id,user_id)]`。

## 4. 实时日志与隐私

每次白名单群内其他人发言都必须恰好输出一条 `INFO 意愿计算` 单行 JSON，并在 DeepSeek 请求之前完成。日志至少包含：群号、发言者 ID、缓冲统计、八项值、八项权重、逐项贡献、命中话题及级别、冷却系数、原始分、概率、最终随机数、结果、原因、当天计数/上限/剩余。

- 无文字、工作时段外、达到额度或概率未通过也必须输出完整结构。
- 常规日志禁止包含消息正文、个人背景原文、API Key、NapCat Token 或 SQLite 内容。
- 群号和发言者 ID 是明确要求的诊断字段；个人背景目标 QQ 号不得硬编码或写入仓库，只能配置在被忽略的 `.env`。

## 5. 话题知识、个人背景与关系

- 话题分析间隔为 `10小时 - 7小时 × group_activity`，并严格夹在每群 3–10 小时。失败至少三小时后再试，保留旧知识。
- 第一阶段把群成员替换为批次匿名编号，再用 DeepSeek 提取话题、摘要、消息数、参与人数、情绪和公开检索词。
- 第二阶段只把公开检索词交给 `web_search`；聊天原文、姓名、QQ 号和成员编号不得进入搜索请求。
- 每批最高热度为 `core`，达到最高消息数 50% 为 `secondary`，其余为 `peripheral`。
- `PERSONA_USER_ID` 是通用配置；旧 `WILLINGNESS_PERSONA_USER_ID` 只作为兼容别名，两者不一致必须报错。常驻冷启动仍尽力读取共同群历史；独立采集器默认 90 天/20000 条并要求用户选择会话。
- 独立采集器先验证 `get_login_info`，原文只存在于内存页；参与者、账号、手机号、邮箱和 URL 在调用 DeepSeek 前脱敏。SQLite 只能保存结构化特征、短脱敏样例、覆盖统计和派生断点。
- 人格、话题等 JSON 提炼必须在 Chat Completions 中显式关闭思考模式；空 `content` 或截断 JSON 最多安全重试三次并逐次提高输出额度，诊断不得记录提示或模型原文。
- 采集器产生 `draft`，只有显式激活的版本可进入线上回答。群聊风格权重默认 0.70，私聊为 0.30；普通群实时增量达到 50 条立即更新，或满 24 小时且至少 10 条时更新。
- 每次 AI 回答通过独立 `ContentFactor` 注入人格，不能重新塞回 `_build_group_context()` 的事实资料。两条 DeepSeek 聊天路径必须共用因子，联网路径不得把因子内容写入搜索词。
- 机器人平时不主动声明身份，但不得冒充模板账号、代替本人表态、泄露资料或恢复“主人/服从”设定。
- 群友 `@` 或引用机器人时关系向 1 靠近 12%；机器人成功回复时向 1 靠近 8%。一天内不衰减，之后按指数遗忘，在约第 30 天或低于 0.01 时归零。
- 发送成功后保存 `send_group_msg` 返回的 `message_id`。收到引用段先查三小时消息流，未命中再调用 `get_msg` 验证引用发送者。

## 6. SQLite 与消息上下文

默认数据库 `data/bot_memory.sqlite3`。除原有长期表外，人格使用 `persona_profile_versions`、`persona_style_dimensions`、`persona_phrases`、`persona_exemplars` 和 `persona_collection_state`；原始聊天不得写入这些表。

- `_create_schema()` 只能用 `CREATE TABLE IF NOT EXISTS` 和 `PRAGMA table_info + ALTER TABLE` 无损升级，绝不重建、清空或删除用户数据库。
- `bot_activity.algorithm_reply_count` 按 `(group_id, local_date)` 持久化并跨日隔离。
- 旧画像表保留兼容，但不再作为新意愿算法输入；不得为了“清理”删除旧数据。
- 三小时群消息流和逐成员聊天历史只在内存中；重启允许丢失。回答上下文仍按 `GROUP_CONTEXT_*` 从消息流裁剪，默认五分钟/20 条/每条 300 字。
- AI 回答上下文只加入发言者、群主/管理员、明确 @ 或名称命中的成员、有效事项和近期群聊，不得塞入完整大群名册；联网路径继续隐藏 QQ 数字标识。

## 7. OneBot、作息与故障边界

- `ACTIVE_GROUP_IDS` 是授权范围。实际发送还要求群号由 `get_group_list` 或实时事件确认；未入群/已退群不能造成重连循环。
- 工作窗口 `ANSWER_START_TIME` 包含、`ANSWER_END_TIME` 排除，允许 `00:00–24:00`，不支持倒置跨午夜。
- 早安在当天首次进入窗口时一次；晚安只在结束后十分钟内一次。SQLite 防重发。
- `get_group_msg_history` 用于背景冷启动；`get_msg` 用于引用兜底；任何失败都应降级使用已有数据，不阻断连接。
- `_run_connection()` 中读取、同步、调度、意愿刷新或意愿分析任务任一意外退出时取消其余任务并重连。
- OneBot 动作失败使用 `OneBotActionError`；定时发送失败只暂停对应群。
- 本地时间问题不得强制联网。联网失败或强制搜索未执行时发送 `WEB_SEARCH_FAILURE_REPLY`，不得编造实时答案。

## 8. 安全、测试和交付清单

- 永远不要读取后输出或提交真实 `DEEPSEEK_API_KEY`、`NAPCAT_WS_TOKEN`、个人背景目标 QQ、群号白名单、SQLite 内容或 `qq/napcat/config/webui.json`。
- `.env`、`data/`、`logs/`、`qq/`、`.venv/`、`.venv-virtualized-old/` 必须保持在 `.gitignore`。
- 配置新增/改名同步 `.env.example`、README、`BotConfig.from_env()` 严格校验和测试。
- 数据库变化必须有旧 schema 无损迁移测试。
- 修改后至少运行：完整单元测试、`py_compile`、`git diff --check`、Git 状态、跟踪/暂存文件敏感值扫描。
- 提交或推送前再次 `git fetch origin`，确认目标分支和远端没有未知提交。只有用户明确要求时才提交、推送或跨分支同步。
- 远端仓库：`https://github.com/infinitymyheaven/qqrobottext`。
