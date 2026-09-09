# AGENTS.md — QQ 群智能 DeepSeek 机器人速查

## 1. 项目与当前分支

- 项目：`qqrobottext`，Python QQ 群机器人。
- 开发分支：`智能ai分支`；基于 `main` 的提交 `1189ac6` 创建，不要自动合并回 `main`。
- 架构：NapCat OneBot v11 正向 WebSocket + Python 客户端 + DeepSeek Chat Completions + SQLite。
- 功能：白名单群成员同步、身份/头衔永久记忆、可配置分钟级作息、智能主动回复、未来事项提醒。
- 依赖：`websockets`、`tzdata`；其余使用 Python 标准库。Python 3.10+。

## 2. 关键文件与命令

- `src/bot.py`：配置、DeepSeek 客户端、OneBot 连接、回答策略、同步和提醒调度。
- `src/memory.py`：SQLite 表结构和所有持久化读写。
- `tests/test_bot.py` / `tests/test_memory.py`：模拟 API、策略和持久化测试。
- `.env.example`：全部配置与默认值；真实 `.env` 已忽略，本机版本也要同步新增变量且不得覆盖密钥、Token 和群号。
- 运行：`.venv\Scripts\python.exe src\bot.py`
- 测试：`.venv\Scripts\python.exe -m unittest discover -s tests -v`
- 本机 `.venv` 基础解释器：`D:\codex\python-runtimes\cpython-3.12.14-windows-x86_64-none\python.exe`。不要改回 Codex 应用隔离的 `AppData\Roaming\uv` 路径。

## 3. 安全边界与配置

- `ACTIVE_GROUP_IDS` 是英文逗号分隔的 QQ 群号白名单；为空时禁用回复、同步、问候和提醒。
- 白名单不等于已入群：每次连接先用 `get_group_list` 验证实际在群状态，只有交集群会同步和主动发送；群消息/通知也会实时更新已入群集合。
- 回答窗口用 `ANSWER_START_TIME` / `ANSWER_END_TIME` 配置，默认 `[10:00, 19:00)`，精确到分钟；窗口外被 @ 也不回答。旧小时变量已经废弃。
- 配置采用严格模式：布尔、数字、时区、时间、区间或权重非法时抛出 `ConfigError`，入口在连接 NapCat 前以状态码 2 退出。
- `FUTURE_MEMORY_SOURCE=active_window_all` 默认扫描回答时段内日期候选消息；改为 `participated` 只分析机器人参与的消息。两套逻辑在 `_handle_group_message()` 有注释，README 必须保持显著说明。
- `DEEPSEEK_API_KEY` 必填。任何真实 API Key、NapCat WebUI Token、群号白名单、数据库内容或 QQ 登录数据都不得写入文档或提交。
- `.env`、`data/`、`qq/`、`.venv/` 和 `.venv-virtualized-old/` 均应保持忽略。

## 4. 成员资料与上下文

- 连接后调用 `get_group_list` 和每个白名单群的 `get_group_member_list(no_cache=true)`；默认每 21600 秒重做全量同步，可由 `.env` 调整。
- 定时问候或提醒遇到 OneBot 动作失败时只暂停对应群，不允许异常结束调度器并触发 WebSocket 重连循环。
- `group_increase`、`group_decrease`、`group_admin`、`group_card` 通知会延迟 2 秒刷新该群并合并重复刷新。
- SQLite 默认 `data/bot_memory.sqlite3`：
  - `groups`：群名与最后同步时间。
  - `members`：当前昵称、群名片、`role`、专属头衔、首次/最后发现、是否仍在群。
  - `member_history`：仅在资料或活跃状态变化时追加快照，永不因上下文截断删除。
  - `future_events`：未来事项、提醒、确认和二次提醒状态。
  - `bot_activity`：每群每日主动回复数、持久化随机日上限、早晚问候和最后发言时间；旧库通过 `ALTER TABLE` 无损增加上限字段。
- AI 上下文始终包含发言者、当前群主/管理员、被 @ 的其他成员及按 QQ/昵称/群名片/头衔命中的成员；成员、未来事项、临时群聊的数量、时间和单条长度均由 `.env` 限制。
- 普通对话历史以 `(group_id, user_id)` 隔离，同时受 `CHAT_HISTORY_MESSAGES` 和 `CHAT_HISTORY_TTL_MINUTES` 限制；永久结构化资料不受影响。

## 5. 主动回复与作息算法

- 回答时段内 @ 必答且不占主动额度；普通文字消息才参与随机判断。
- 主动回复可独立关闭；每群每日从 `SPONTANEOUS_DAILY_MIN/MAX` 随机生成并持久化上限，默认 60–100。
- 评分三项权重、流量窗口/满分消息数、沉默满分秒数、阈值与最小间隔全部来自 `.env`；三项权重之和必须为 1。
- 所有成功发出的机器人消息都会更新 `last_bot_sent_at`；只有算法触发回复增加 `spontaneous_count`。
- 工作开始后发送一次随机早安，工作结束后的 10 分钟内发送一次随机晚安；开关和 `||` 分隔的模板来自 `.env`。
- 后台同步或调度任务异常会结束当前连接会话，由外层 3 秒重连恢复，避免功能静默失效。

## 6. 未来事项规则

- 本地 `_DATE_CUE_PATTERN` 先过滤，DeepSeek 再输出 JSON：`events[{summary,event_at}]`。
- 相对日期按当前 `BOT_TIMEZONE` 解析；仅日期默认 23:59；过去或格式无效的事项丢弃；相同群、来源、摘要和时间去重。
- 初次提醒默认提前 60 分钟且不 @；睡眠时段的提醒提前移动到最近的回答时段。
- 初次提醒后，来源成员在同群发任意消息即写入确认状态。
- 未确认时按 `REMINDER_FOLLOWUP_MIN_MINUTES` / `REMINDER_FOLLOWUP_MAX_MINUTES` 随机等待后准备二次 @；若事项届时已过期则不发送。
- 提醒状态持久化，重启后不会重复发送。

## 7. 本机与版本控制事实

- QQ：`D:\Program Files\Tencent\QQNT\QQ.exe`；NapCat 使用 `qq/napcat/launcher-fixed.bat`。
- OneBot：`127.0.0.1:3001`、数组消息格式、Token 默认空。NapCat 控制台必须保持运行。
- 2026-09-09 09:05 的最后一次启动检查中端口 3001 拒绝连接，说明当时 NapCat 未运行；端到端验收前需重新启动。
- `qq/napcat/config/webui.json` 含敏感 Token，不得读取后输出或提交。
- 远端：`https://github.com/infinitymyheaven/qqrobottext`。GitHub 网络使用仓库既有代理配置。
- 实现完成后先运行全部测试、语法检查、`git diff --check` 和疑似密钥扫描，再提交并仅推送 `智能ai分支`。

## 8. 验收重点

- 白名单为空完全静默；非白名单群不读资料、不回复。
- 名册无需群友发言即可识别群主、管理员、群名片和专属头衔；重启后资料仍在。
- 10:00/19:00 边界、@必答、普通消息权重、十分钟间隔和每日上限正确。
- 两种日期来源模式、提醒时段调整、任意消息确认、过期取消二次 @ 正确。
- 测试不得调用真实 DeepSeek 或向真实 QQ 群发消息。
