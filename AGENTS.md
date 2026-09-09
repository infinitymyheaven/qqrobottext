# AGENTS.md — QQ 群智能 DeepSeek 机器人接手指南

## 1. 项目目标与当前状态

- 项目名：`qqrobottext`，Python 3.10+ 的 QQ 群聊机器人。
- 当前开发分支：`智能ai分支`，跟踪 `origin/智能ai分支`；不要自动合并或直接改写 `main`。
- 分支基线：`main` 固定在 `1189ac6`；智能功能和后续修复只在当前分支演进。
- 运行架构：NapCat OneBot v11 正向 WebSocket → 本项目 Python 客户端 → DeepSeek OpenAI 兼容 Chat Completions API。
- 当前能力：白名单群控制、完整群成员同步、角色/头衔长期记忆、分钟级作息、@回答、算法主动插话、近期群聊上下文、逐成员对话上下文、未来事项提取与提醒。
- 最近功能版本：`8afeadc`，已完成行为参数 `.env` 化、严格配置校验和每日随机主动回复上限。
- 自动化测试不连接真实 QQ，也不调用 DeepSeek；配置升级后的真实群聊端到端验证仍需人工执行。

## 2. 代码地图与启动命令

- `src/bot.py`：`.env` 读取与校验、DeepSeek HTTP 客户端、OneBot WebSocket、消息处理、上下文构造、主动回复算法、成员同步和提醒调度。
- `src/memory.py`：SQLite 建表/迁移以及成员资料、资料历史、未来事项和每日活动读写。
- `tests/test_bot.py`：消息解析、配置、时间边界、上下文、同步、问候、主动回复和提醒测试。
- `tests/test_memory.py`：成员历史、退群状态、事项去重、每日随机上限和旧库迁移测试。
- `.env.example`：所有公开配置及默认值的唯一权威模板；README 里的配置表必须与它同步。
- `README.md`：用户安装、NapCat 配置、行为说明和完整配置参考。

Windows 本机命令：

```powershell
cd D:\codex\qqrobot
.\.venv\Scripts\python.exe src\bot.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

- 不要调用系统商店的 `python`；项目 `.venv` 的基础解释器位于 `D:\codex\python-runtimes\cpython-3.12.14-windows-x86_64-none\python.exe`。
- 启动顺序是先以管理员身份运行 `qq\napcat\launcher-fixed.bat` 并登录 QQ，再启动 Python 机器人。
- `.env` 修改后必须重启机器人；停止进程使用 `Ctrl+C`。

## 3. 配置约定

- `main()` 先调用 `load_env_file()`，再由 `BotConfig.from_env()` 严格解析；配置错误必须在连接 NapCat 前以状态码 2 退出。
- 布尔值只接受 `true/false`、`1/0`、`yes/no`、`on/off`；数值、时区、时间、区间和权重不允许静默回退或自动截断。
- `ANSWER_START_TIME` / `ANSWER_END_TIME` 使用 `HH:MM`，开始包含、结束不包含；允许 `00:00–24:00`，但不支持跨午夜倒置区间。
- 旧变量 `ANSWER_START_HOUR`、`ANSWER_END_HOUR`、`SPONTANEOUS_DAILY_LIMIT` 已废弃，不要恢复兼容逻辑。
- `ACTIVE_GROUP_IDS` 使用英文逗号分隔；空值表示完全禁用同步、回复、问候和提醒。
- 白名单只是授权范围。实际发送还要求群号存在于 NapCat `get_group_list` 结果或已由实时群事件确认；未入群/已退群不能触发重连循环。
- 三个主动回复权重必须非负且总和为 1。当前默认公式：

```text
0.40 × random.random()
+ 0.25 × min(流量窗口消息数 / 10, 1)
+ 0.35 × min(沉默秒数 / 1200, 1)
```

- 默认主动回复日上限不是固定值：每群每天从 `SPONTANEOUS_DAILY_MIN=60` 到 `SPONTANEOUS_DAILY_MAX=100` 随机抽取并持久化；@回复、问候和提醒不计入。
- `FUTURE_MEMORY_SOURCE=active_window_all` 会分析工作时段内全部日期候选消息；`participated` 只分析机器人实际参与的消息。`FUTURE_MEMORY_ENABLED=false` 同时关闭提取、上下文注入和提醒。
- 早晚问候模板使用 `||` 分隔；模板、开关、上下文窗口、提醒区间和所有用户行为参数均以 `.env.example` 为准。

## 4. 消息与并发流程

1. `_read_loop()` 按 `echo` 完成 OneBot 动作 Future；群消息和通知分别创建后台任务。
2. `_handle_group_message()` 先验证白名单、排除机器人自身消息，再记录最近群聊和提醒确认；工作时段外立即静默。
3. 工作时段内，@消息必答；普通文字消息在群级锁内执行每日上限、最小间隔和评分判断。
4. `_answer_message()` 使用 `(group_id, user_id)` 对话锁隔离历史；历史同时受条数和闲置 TTL 限制。
5. `_build_group_context()` 按需加入发言者、群主/管理员、明确 @ 或名称命中的成员、有效未来事项及近期群聊，不允许把完整大群名册塞进每次 API 请求。
6. DeepSeek 成功后才更新对话历史；所有成功发送的机器人消息更新沉默时间，只有算法主动插话增加主动回复计数。
7. 日期关键词先经本地正则过滤，再在独立 Semaphore 内调用 DeepSeek 提取结构化事项。

并发不变量：

- 同群“评分 → 回复 → 计数”必须持有 `_group_reply_locks[group_id]`，避免并发消息绕过间隔或日上限。
- 同一成员的对话必须持有 `_conversation_locks[(group_id, user_id)]`，不同成员可并行。
- OneBot 动作失败使用 `OneBotActionError`；定时发送失败只暂停对应群，不得使整个调度器退出。
- `_run_connection()` 中读取循环、定时同步和调度任务任一意外结束时应取消其余任务并重连，避免“连接在线但后台功能死亡”。

## 5. SQLite 长期记忆

默认数据库为 `data/bot_memory.sqlite3`，`data/` 必须保持忽略：

- `groups`：群名、最后同步时间。
- `members`：昵称、群名片、角色、专属头衔、首次/最后发现时间、当前是否在群。
- `member_history`：成员资料或活跃状态变化时追加快照；退群成员标记为非活跃，不能删除历史。
- `future_events`：事项摘要、来源、事件时间、初次提醒、确认及二次提醒状态。
- `bot_activity`：每群每日主动回复数、随机日上限、问候状态和最后发言时间。

兼容要求：

- `_create_schema()` 使用 `CREATE TABLE IF NOT EXISTS`，并通过 `PRAGMA table_info` + `ALTER TABLE` 为旧库补充 `daily_spontaneous_limit`，不得重建或清空用户数据库。
- 每日随机上限按 `(group_id, local_date)` 隔离；重启保持不变。配置区间改变且旧值越界时，当天重新抽取。
- 成员每次全量同步后，本次缺失的旧成员只标记 `is_active=0`；资料历史永久保存。
- 普通群聊和逐成员聊天历史只存在内存中，不写入 SQLite；重启后允许丢失。

## 6. 作息、问候和提醒边界

- 工作窗口按配置分钟数判断，开始时刻包含、结束时刻排除；窗口外即使被 @ 也不回答。
- 早安在当天首次进入工作窗口时发送一次；晚安只在结束后的 10 分钟内发送一次。SQLite 防止重连重复发送。
- 最近群聊默认 300 秒、最多 20 条、每条 300 字；流量评分使用独立窗口，不能因修改群聊上下文时间而改变统计语义。
- 逐成员对话默认最多 10 条消息、闲置 30 分钟过期；TTL 为 0 时只按条数限制。
- 未来事项默认提前 60 分钟提醒；落在休息时段时移动到此前最近的工作窗口。
- 初次提醒只发普通文本；来源成员之后在同群发送任意消息即确认。
- 未确认事项按配置的分钟区间等待二次 @；若事项已过期则取消。

## 7. 安全、测试与发布清单

- 永远不要读取后输出或提交真实 `DEEPSEEK_API_KEY`、`NAPCAT_WS_TOKEN`、群号白名单、SQLite 内容或 `qq/napcat/config/webui.json`。
- `.env`、`data/`、`qq/`、`.venv/`、`.venv-virtualized-old/` 必须保持在 `.gitignore` 中。
- 测试只使用临时 SQLite、模拟 WebSocket 和模拟 DeepSeek；不得向真实群发消息或消耗真实 API 额度。
- 修改后至少执行：完整单元测试、`py_compile`、`git diff --check`、Git 状态检查和暂存区敏感值扫描。
- 配置新增/改名时必须同步修改 `.env.example`、README 配置表、`BotConfig` 严格校验和配置测试。
- 数据库字段变化必须提供针对旧 schema 的无损迁移测试。
- 提交前确认 `main` 与 `origin/main` 仍为 `1189ac6`，只推送 `智能ai分支`。
- 远端仓库：`https://github.com/infinitymyheaven/qqrobottext`。

## 8. 已知运行环境与排障

- QQ 客户端：`D:\Program Files\Tencent\QQNT\QQ.exe`；NapCat 本地目录为被忽略的 `qq/`。
- OneBot 默认地址：`ws://127.0.0.1:3001`，消息格式必须为数组；Access Token 两端配置必须一致。
- 日志显示“已同步群 … 的 0 名成员”时，先确认机器人账号确实在该群；当前版本会跳过 `get_group_list` 中不存在的白名单群。
- @无回复时依次检查：群是否在白名单、机器人是否仍在群、当前是否处于工作窗口、NapCat 是否持续运行、DeepSeek Key/余额及模型名是否有效。
- 启动后没有持续日志通常表示正在等待消息，不代表程序卡死。
