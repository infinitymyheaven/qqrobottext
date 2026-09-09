#!/usr/bin/env python3
"""有作息、群成员资料和未来事项记忆的 QQ 群 DeepSeek 机器人。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from html import unescape
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import websockets

if __package__:
    from .memory import MemoryStore
else:  # 支持 README 中的 `python src\bot.py` 直接启动方式。
    from memory import MemoryStore

RETRY_DELAY_SECONDS = 3.0
ACTION_TIMEOUT_SECONDS = 10.0
DEFAULT_WS_URL = "ws://127.0.0.1:3001"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_SYSTEM_PROMPT = "你是一个友好、自然、简洁的 QQ 群聊助手。请结合可靠的群资料和近期对话直接回答。"
DEFAULT_ERROR_REPLY = "抱歉，AI 服务暂时不可用，请稍后再试。"
DEFAULT_EMPTY_REPLY = "请在 @ 我后输入想聊的内容。"
DEFAULT_DB_PATH = "data/bot_memory.sqlite3"
ENV_FILE = Path(".env")

MORNING_MESSAGES = (
    "早上好，我睡醒啦，今天也来和大家一起聊天。",
    "早呀，我来上班了，今天也请多关照。",
    "大家早上好，我已经醒啦，有事可以叫我。",
)
NIGHT_MESSAGES = (
    "到休息时间啦，大家晚安，我明天再来。",
    "今天先聊到这里，我要睡觉啦，大家晚安。",
    "晚上好，也晚安啦，我先休息，明天见。",
)
ROLE_LABELS = {"owner": "群主", "admin": "管理员", "member": "群成员", "unknown": "身份未知"}

_CQ_AT_PATTERN = re.compile(r"\[CQ:at(?:,([^\]]*))?\]")
_CQ_CODE_PATTERN = re.compile(r"\[CQ:[^\]]+\]")
_DATE_CUE_PATTERN = re.compile(
    r"(?:\d{1,4}[年./-]\d{1,2}|\d{1,2}月\d{1,2}[日号]?|"
    r"今天|明天|后天|大后天|本周|这周|下周|星期|礼拜|周[一二三四五六日天]|"
    r"早上|上午|中午|下午|晚上|凌晨|\d{1,2}[:：点时]|截止|到期)"
)

logger = logging.getLogger("qqrobot")


def load_env_file(path: Path = ENV_FILE) -> None:
    """读取简单 KEY=VALUE 格式的 .env，不覆盖已有环境变量。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def with_access_token(url: str, token: str) -> str:
    if not token:
        return url
    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "access_token"
    ]
    query.append(("access_token", token))
    return urlunparse(parsed._replace(query=urlencode(query)))


def is_at_self(payload: dict, self_id: str) -> bool:
    message = payload.get("message")
    if isinstance(message, list):
        return any(
            isinstance(segment, dict)
            and segment.get("type") == "at"
            and str((segment.get("data") or {}).get("qq")) == self_id
            for segment in message
        )
    raw = str(payload.get("raw_message") or "")
    for match in _CQ_AT_PATTERN.finditer(raw):
        for part in (match.group(1) or "").split(","):
            key, separator, value = part.partition("=")
            if separator and key.strip() == "qq" and value.strip() == self_id:
                return True
    return False


def extract_mentioned_ids(payload: dict, self_id: str) -> list[str]:
    message = payload.get("message")
    ids: list[str] = []
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "at":
                continue
            qq = str((segment.get("data") or {}).get("qq") or "")
            if qq and qq not in {self_id, "all"} and qq not in ids:
                ids.append(qq)
    return ids


def extract_message_text(payload: dict) -> str:
    message = payload.get("message")
    if isinstance(message, list):
        parts = []
        for segment in message:
            if isinstance(segment, dict) and segment.get("type") == "text":
                value = (segment.get("data") or {}).get("text")
                if value is not None:
                    parts.append(str(value))
        return "".join(parts).strip()
    raw = str(payload.get("raw_message") or message or "")
    return unescape(_CQ_CODE_PATTERN.sub("", raw)).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("环境变量 %s 不是整数，使用默认值 %s", name, default)
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("环境变量 %s 不是数字，使用默认值 %s", name, default)
        return default


@dataclass(frozen=True)
class BotConfig:
    active_group_ids: frozenset[str]
    timezone: ZoneInfo
    answer_start_hour: int = 10
    answer_end_hour: int = 19
    spontaneous_daily_limit: int = 50
    spontaneous_min_interval_seconds: int = 600
    spontaneous_score_threshold: float = 0.65
    member_sync_interval_seconds: int = 21600
    future_memory_source: str = "active_window_all"
    reminder_lead_minutes: int = 60
    memory_db_path: str = DEFAULT_DB_PATH
    max_history_messages: int = 10
    max_reply_chars: int = 2000

    @classmethod
    def from_env(cls) -> "BotConfig":
        group_ids = frozenset(
            value.strip()
            for value in (os.getenv("ACTIVE_GROUP_IDS") or "").split(",")
            if value.strip()
        )
        timezone_name = (os.getenv("BOT_TIMEZONE") or "Asia/Shanghai").strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logger.warning("未知时区 %s，使用 Asia/Shanghai", timezone_name)
            timezone = ZoneInfo("Asia/Shanghai")
        source = (os.getenv("FUTURE_MEMORY_SOURCE") or "active_window_all").strip()
        if source not in {"active_window_all", "participated"}:
            logger.warning("未知 FUTURE_MEMORY_SOURCE=%s，使用 active_window_all", source)
            source = "active_window_all"
        start = min(23, max(0, _env_int("ANSWER_START_HOUR", 10)))
        end = min(24, max(start + 1, _env_int("ANSWER_END_HOUR", 19)))
        return cls(
            active_group_ids=group_ids,
            timezone=timezone,
            answer_start_hour=start,
            answer_end_hour=end,
            spontaneous_daily_limit=max(0, _env_int("SPONTANEOUS_DAILY_LIMIT", 50)),
            spontaneous_min_interval_seconds=max(
                0, _env_int("SPONTANEOUS_MIN_INTERVAL_SECONDS", 600)
            ),
            spontaneous_score_threshold=min(
                1.0, max(0.0, _env_float("SPONTANEOUS_SCORE_THRESHOLD", 0.65))
            ),
            member_sync_interval_seconds=max(
                60, _env_int("MEMBER_SYNC_INTERVAL_SECONDS", 21600)
            ),
            future_memory_source=source,
            reminder_lead_minutes=max(0, _env_int("REMINDER_LEAD_MINUTES", 60)),
            memory_db_path=(os.getenv("MEMORY_DB_PATH") or DEFAULT_DB_PATH).strip(),
            max_history_messages=max(0, _env_int("CHAT_HISTORY_MESSAGES", 10)),
            max_reply_chars=max(1, _env_int("MAX_REPLY_CHARS", 2000)),
        )


class DeepSeekAPIError(RuntimeError):
    pass


class OneBotActionError(RuntimeError):
    """NapCat 接受了动作请求，但 OneBot 返回了失败结果。"""

    def __init__(self, retcode, message: str):
        self.retcode = retcode
        self.message = message
        super().__init__(f"OneBot 动作失败（retcode={retcode}）：{message}")


def _onebot_failure_message(payload: dict) -> str:
    raw = str(payload.get("message") or payload.get("wording") or "未知错误")
    nested = re.search(r'"errMsg"\s*:\s*"([^"]+)"', raw)
    if nested:
        return nested.group(1)
    return raw.splitlines()[0][:300]


class DeepSeekClient:
    """DeepSeek OpenAI 兼容客户端，支持聊天和未来日期结构化提取。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_DEEPSEEK_BASE_URL,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        timeout_seconds: float = 60.0,
        max_tokens: int = 1024,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.system_prompt = system_prompt
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens

    @property
    def endpoint(self) -> str:
        return self.base_url if self.base_url.endswith("/chat/completions") else f"{self.base_url}/chat/completions"

    async def chat(self, history: list[dict], user_text: str, *, context: str = "") -> str:
        messages = [{"role": "system", "content": self.system_prompt}]
        if context:
            messages.append(
                {
                    "role": "system",
                    "content": "以下资料来自本地数据库，身份信息以此为准；不要编造未提供的信息。\n" + context,
                }
            )
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})
        payload = await asyncio.to_thread(
            self._post,
            {"model": self.model, "messages": messages, "stream": False, "max_tokens": self.max_tokens},
        )
        return self._content(payload)

    async def extract_future_events(self, text: str, now: datetime) -> list[dict]:
        prompt = (
            "从消息中提取尚未发生且时间明确的未来事项。相对日期以给定当前时间解析；"
            "只有日期没有具体时间时使用当天23:59。不要提取过去时间或含糊的‘以后’。"
            "仅返回JSON对象，格式为 {\"events\":[{\"summary\":\"事项摘要\","
            "\"event_at\":\"带时区的ISO 8601时间\"}]}；没有则返回 {\"events\":[]}。\n"
            f"当前时间：{now.isoformat()}\n消息：{text}"
        )
        payload = await asyncio.to_thread(
            self._post,
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "max_tokens": 512,
                "response_format": {"type": "json_object"},
            },
        )
        content = self._content(payload).strip()
        if content.startswith("```"):
            lines = content.splitlines()
            content = "\n".join(lines[1:-1]) if len(lines) >= 3 else content
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DeepSeekAPIError("未来事项提取结果不是有效 JSON") from exc
        events = decoded.get("events", []) if isinstance(decoded, dict) else []
        return [event for event in events if isinstance(event, dict)]

    def _post(self, body: dict) -> dict:
        request = Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise DeepSeekAPIError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise DeepSeekAPIError(f"DeepSeek 请求失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise DeepSeekAPIError("DeepSeek 返回了无法识别的数据")
        return payload

    @staticmethod
    def _content(payload: dict) -> str:
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekAPIError("DeepSeek 返回了无法识别的数据") from exc
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekAPIError("DeepSeek 返回了空回复")
        return content.strip()


class QQBot:
    def __init__(
        self,
        ws_url: str,
        token: str = "",
        *,
        llm_client: DeepSeekClient | None = None,
        config: BotConfig | None = None,
        memory: MemoryStore | None = None,
        now_provider: Callable[[], datetime] | None = None,
        rng=None,
    ) -> None:
        self.log_url = ws_url
        self.ws_url = with_access_token(ws_url, token)
        self.llm_client = llm_client
        self.config = config or BotConfig.from_env()
        self.memory = memory or MemoryStore(self.config.memory_db_path)
        self._owns_memory = memory is None
        self._now_provider = now_provider or (lambda: datetime.now(self.config.timezone))
        self.rng = rng or random.SystemRandom()
        self._histories: dict[tuple[str, str], list[dict]] = {}
        self._conversation_locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._group_reply_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._extraction_semaphore = asyncio.Semaphore(2)
        self._recent_messages: defaultdict[str, deque] = defaultdict(deque)
        self._refresh_tasks: dict[str, asyncio.Task] = {}
        # 白名单只是用户授权范围；实际发送前还必须由 get_group_list 或群事件
        # 确认机器人当前确实在群内，防止对尚未加入/已被移出的群反复发送。
        self._joined_group_ids: set[str] = set()

    def now(self) -> datetime:
        value = self._now_provider()
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.config.timezone)
        return value.astimezone(self.config.timezone)

    def is_answer_time(self, now: datetime | None = None) -> bool:
        current = (now or self.now()).astimezone(self.config.timezone)
        return self.config.answer_start_hour <= current.hour < self.config.answer_end_hour

    async def run(self) -> None:
        logger.info("正在连接 NapCat（%s）...", self.log_url)
        if not self.config.active_group_ids:
            logger.warning("ACTIVE_GROUP_IDS 为空：所有回复、同步、问候和提醒均已禁用。")
        try:
            while True:
                try:
                    async with websockets.connect(self.ws_url) as ws:
                        logger.info("已连接 NapCat OneBot WebSocket。")
                        await self._run_connection(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("连接异常：%s", exc)
                logger.info("与 NapCat 的连接已断开，稍后自动重连...")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                logger.info("正在连接 NapCat（%s）...", self.log_url)
        finally:
            if self._owns_memory:
                self.memory.close()

    async def _run_connection(self, ws) -> None:
        pending: dict = {}
        reader = asyncio.create_task(self._read_loop(ws, pending))
        workers: list[asyncio.Task] = []
        try:
            if self.config.active_group_ids:
                self._joined_group_ids.clear()
                await self._sync_all_groups(ws, pending)
                workers = [
                    asyncio.create_task(self._periodic_sync_loop(ws, pending)),
                    asyncio.create_task(self._scheduler_loop(ws, pending)),
                ]
            done, _ = await asyncio.wait(
                [reader, *workers], return_when=asyncio.FIRST_COMPLETED
            )
            # 读取循环结束代表连接断开；后台同步或调度异常则向上抛出并重连，
            # 避免机器人看似在线但早晚问候、提醒已经悄悄停止。
            for task in done:
                await task
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    async def _read_loop(self, ws, pending: dict | None = None) -> None:
        pending = {} if pending is None else pending
        event_tasks: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                echo = payload.get("echo")
                if echo is not None and echo in pending:
                    future = pending.pop(echo)
                    if not future.done():
                        if payload.get("status") == "ok" and payload.get("retcode") == 0:
                            future.set_result(payload)
                        else:
                            future.set_exception(
                                OneBotActionError(
                                    payload.get("retcode"), _onebot_failure_message(payload)
                                )
                            )
                    continue

                task = None
                if payload.get("post_type") == "message" and payload.get("message_type") == "group":
                    task = asyncio.create_task(self._handle_group_message(ws, payload, pending))
                elif payload.get("post_type") == "notice":
                    task = asyncio.create_task(self._handle_notice(ws, payload, pending))
                if task:
                    event_tasks.add(task)
                    task.add_done_callback(event_tasks.discard)
        finally:
            error = RuntimeError("WebSocket 连接已断开")
            for future in pending.values():
                if not future.done():
                    future.set_exception(error)
            if event_tasks:
                await asyncio.gather(*event_tasks, return_exceptions=True)

    async def _handle_group_message(self, ws, payload: dict, pending: dict) -> None:
        try:
            group_id = str(payload.get("group_id") or "")
            user_id = str(payload.get("user_id") or "")
            self_id = str(payload.get("self_id") or "")
            if group_id not in self.config.active_group_ids or not user_id or user_id == self_id:
                return
            # 能收到这个群的消息，本身就是机器人仍在群内的实时证明。
            self._joined_group_ids.add(group_id)

            now = self.now()
            self.memory.acknowledge_user(group_id, user_id, now.timestamp())
            text_value = extract_message_text(payload)
            if text_value:
                self._remember_recent(group_id, user_id, text_value, payload, now)
            if not self.is_answer_time(now):
                return

            mentioned = is_at_self(payload, self_id)
            spontaneous = False
            should_reply = False
            # 同一群的“评分 -> 回复 -> 计数”必须串行，否则繁忙群可能多条消息
            # 同时看到旧的最后发言时间并一起越过十分钟间隔。
            async with self._group_reply_locks[group_id]:
                if mentioned:
                    should_reply = True
                elif text_value:
                    should_reply = self._should_reply_spontaneously(group_id, now)
                    spontaneous = should_reply

                if mentioned and not text_value:
                    await self._send_group_message(
                        ws, group_id, DEFAULT_EMPTY_REPLY, pending, now=now
                    )
                elif should_reply and text_value:
                    await self._answer_message(
                        ws,
                        payload,
                        pending,
                        text_value,
                        group_id,
                        user_id,
                        self_id,
                        now,
                        spontaneous,
                    )

            # active_window_all：回答时段内所有候选消息都提取未来事项。
            # participated：仅从机器人实际参与回复的消息中提取。通过 .env 切换，无需改源码。
            should_extract = text_value and (
                self.config.future_memory_source == "active_window_all"
                or (self.config.future_memory_source == "participated" and should_reply)
            )
            if should_extract and _DATE_CUE_PATTERN.search(text_value):
                await self._extract_and_store_future_events(payload, text_value, now)
        except Exception:  # noqa: BLE001
            logger.exception("处理群消息失败")

    async def _answer_message(
        self,
        ws,
        payload: dict,
        pending: dict,
        question: str,
        group_id: str,
        user_id: str,
        self_id: str,
        now: datetime,
        spontaneous: bool,
    ) -> None:
        if self.llm_client is None:
            await self._send_group_message(
                ws, group_id, DEFAULT_ERROR_REPLY, pending, now=now, spontaneous=spontaneous
            )
            return
        conversation_id = (group_id, user_id)
        async with self._conversation_locks[conversation_id]:
            history = self._histories.get(conversation_id, [])
            context = self._build_group_context(
                group_id, user_id, extract_mentioned_ids(payload, self_id), question, now
            )
            try:
                answer = await self.llm_client.chat(history, question, context=context)
            except Exception:  # noqa: BLE001
                logger.exception("调用 DeepSeek 失败")
                await self._send_group_message(
                    ws, group_id, DEFAULT_ERROR_REPLY, pending, now=now, spontaneous=spontaneous
                )
                return
            updated = history + [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ]
            self._histories[conversation_id] = (
                updated[-self.config.max_history_messages :]
                if self.config.max_history_messages
                else []
            )
            await self._send_group_message(
                ws, group_id, answer, pending, now=now, spontaneous=spontaneous
            )

    def _remember_recent(
        self, group_id: str, user_id: str, text_value: str, payload: dict, now: datetime
    ) -> None:
        sender = payload.get("sender") or {}
        display_name = str(sender.get("card") or sender.get("nickname") or user_id)
        queue = self._recent_messages[group_id]
        queue.append((now.timestamp(), user_id, display_name, text_value[:300]))
        cutoff = now.timestamp() - 60
        while queue and queue[0][0] < cutoff:
            queue.popleft()

    def _should_reply_spontaneously(self, group_id: str, now: datetime) -> bool:
        activity = self.memory.get_activity(group_id, now.date().isoformat())
        if activity["spontaneous_count"] >= self.config.spontaneous_daily_limit:
            return False
        last_sent = activity["last_bot_sent_at"]
        silence = float("inf") if last_sent is None else max(0.0, now.timestamp() - last_sent)
        if silence < self.config.spontaneous_min_interval_seconds:
            return False
        message_count = len(self._recent_messages[group_id])
        traffic_score = min(message_count / 10.0, 1.0)
        silence_score = min(silence / 1200.0, 1.0)
        score = 0.40 * self.rng.random() + 0.25 * traffic_score + 0.35 * silence_score
        logger.debug("群 %s 主动回复评分 %.3f（近一分钟 %s 条）", group_id, score, message_count)
        return score >= self.config.spontaneous_score_threshold

    def _build_group_context(
        self,
        group_id: str,
        user_id: str,
        mentioned_ids: list[str],
        question: str,
        now: datetime,
    ) -> str:
        selected: dict[str, dict] = {}
        speaker = self.memory.get_member(group_id, user_id)
        if speaker:
            selected[user_id] = speaker
        for member in self.memory.get_members_by_role(group_id, ("owner", "admin")):
            selected[member["user_id"]] = member
        for uid in mentioned_ids:
            member = self.memory.get_member(group_id, uid)
            if member:
                selected[uid] = member
        include_inactive = any(word in question for word in ("以前", "曾经", "退群", "过去"))
        for member in self.memory.search_members(
            group_id, question, include_inactive=include_inactive, limit=20
        ):
            selected[member["user_id"]] = member

        lines = ["群成员资料："]
        for member in selected.values():
            display = member["card"] or member["nickname"] or member["user_id"]
            role = ROLE_LABELS.get(member["role"], member["role"])
            title = f"，专属头衔：{member['title']}" if member["title"] else ""
            state = "当前在群" if member["is_active"] else "已退群"
            lines.append(
                f"- {display}（QQ {member['user_id']}，{role}{title}，{state}）"
            )

        events = self.memory.get_active_events(group_id, now.timestamp(), limit=20)
        if events:
            lines.append("仍有效的未来事项：")
            for event in events:
                event_time = datetime.fromtimestamp(event["event_at"], self.config.timezone)
                lines.append(f"- {event_time:%Y-%m-%d %H:%M}：{event['summary']}")

        recent = list(self._recent_messages[group_id])[-20:]
        if recent:
            lines.append("最近一分钟群聊：")
            lines.extend(f"- {name}：{text_value}" for _, _, name, text_value in recent)
        return "\n".join(lines)

    async def _extract_and_store_future_events(
        self, payload: dict, text_value: str, now: datetime
    ) -> None:
        if self.llm_client is None:
            return
        try:
            # 回答时段全量模式可能同时命中很多日期消息，限制并发避免瞬时打满 API。
            async with self._extraction_semaphore:
                extracted = await self.llm_client.extract_future_events(text_value, now)
        except Exception:  # noqa: BLE001
            logger.exception("提取未来事项失败")
            return
        for event in extracted:
            summary = str(event.get("summary") or "").strip()
            event_at = self._parse_event_time(event.get("event_at"), now)
            if not summary or event_at is None or event_at <= now:
                continue
            ideal = event_at - timedelta(minutes=self.config.reminder_lead_minutes)
            remind_at = self._adjust_reminder_before(ideal)
            if remind_at < now:
                remind_at = now
            added = self.memory.add_future_event(
                group_id=payload.get("group_id"),
                source_user_id=payload.get("user_id"),
                source_message_id=payload.get("message_id"),
                summary=summary,
                event_at=event_at.timestamp(),
                remind_at=remind_at.timestamp(),
                created_at=now.timestamp(),
            )
            if added:
                logger.info("已记住群 %s 的未来事项：%s", payload.get("group_id"), summary)

    def _parse_event_time(self, raw_value, now: datetime) -> datetime | None:
        if not isinstance(raw_value, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.config.timezone)
        return parsed.astimezone(self.config.timezone)

    def _adjust_reminder_before(self, moment: datetime) -> datetime:
        local = moment.astimezone(self.config.timezone)
        start = datetime.combine(local.date(), time(self.config.answer_start_hour), self.config.timezone)
        end = datetime.combine(local.date(), time(self.config.answer_end_hour % 24), self.config.timezone)
        if self.config.answer_end_hour == 24:
            end = start.replace(hour=0) + timedelta(days=1)
        if local < start:
            previous = start - timedelta(days=1)
            return previous.replace(hour=self.config.answer_end_hour - 1, minute=59, second=59)
        if local >= end:
            return end - timedelta(seconds=1)
        return local

    def _adjust_to_next_answer_time(self, moment: datetime) -> datetime:
        local = moment.astimezone(self.config.timezone)
        start = datetime.combine(local.date(), time(self.config.answer_start_hour), self.config.timezone)
        end_hour = self.config.answer_end_hour
        end = (
            datetime.combine(local.date(), time(end_hour), self.config.timezone)
            if end_hour < 24
            else datetime.combine(local.date() + timedelta(days=1), time(0), self.config.timezone)
        )
        if local < start:
            return start
        if local >= end:
            return start + timedelta(days=1)
        return local

    async def _handle_notice(self, ws, payload: dict, pending: dict) -> None:
        group_id = str(payload.get("group_id") or "")
        if group_id not in self.config.active_group_ids:
            return
        if (
            payload.get("notice_type") == "group_decrease"
            and str(payload.get("user_id") or "") == str(payload.get("self_id") or "")
        ):
            self._joined_group_ids.discard(group_id)
            logger.warning("机器人已离开白名单群 %s，暂停该群的主动发送", group_id)
            return
        self._joined_group_ids.add(group_id)
        if payload.get("notice_type") not in {
            "group_increase",
            "group_decrease",
            "group_admin",
            "group_card",
        }:
            return
        existing = self._refresh_tasks.get(group_id)
        if existing and not existing.done():
            return

        async def refresh() -> None:
            await asyncio.sleep(2)
            await self._sync_group(ws, pending, group_id)

        task = asyncio.create_task(refresh())
        self._refresh_tasks[group_id] = task
        try:
            await task
        finally:
            self._refresh_tasks.pop(group_id, None)

    async def _sync_all_groups(self, ws, pending: dict) -> None:
        names: dict[str, str] = {}
        try:
            groups = await self._send_action(ws, "get_group_list", {"no_cache": True}, pending)
            if isinstance(groups, list):
                names = {
                    str(group.get("group_id")): str(group.get("group_name") or "")
                    for group in groups
                    if isinstance(group, dict)
                }
                joined = self.config.active_group_ids.intersection(names)
                self._joined_group_ids = set(joined)
                missing = self.config.active_group_ids.difference(joined)
                for group_id in sorted(missing):
                    logger.warning(
                        "白名单群 %s 不在机器人当前群列表中，跳过同步和主动发送",
                        group_id,
                    )
            else:
                raise RuntimeError("群列表响应不是数组")
        except Exception:  # noqa: BLE001
            logger.exception("获取群列表失败，仅保留已经确认在群内的群")
        for group_id in sorted(self._joined_group_ids):
            await self._sync_group(ws, pending, group_id, names.get(group_id, ""))

    async def _sync_group(
        self, ws, pending: dict, group_id: str, group_name: str = ""
    ) -> None:
        try:
            members = await self._send_action(
                ws,
                "get_group_member_list",
                {"group_id": int(group_id), "no_cache": True},
                pending,
            )
            if not isinstance(members, list):
                raise RuntimeError("群成员列表响应不是数组")
            self.memory.sync_members(group_id, members, self.now().timestamp(), group_name)
            logger.info("已同步群 %s 的 %s 名成员", group_id, len(members))
        except Exception:  # noqa: BLE001
            logger.exception("同步群 %s 成员失败，继续使用本地旧资料", group_id)

    async def _periodic_sync_loop(self, ws, pending: dict) -> None:
        while True:
            await asyncio.sleep(self.config.member_sync_interval_seconds)
            await self._sync_all_groups(ws, pending)

    async def _scheduler_loop(self, ws, pending: dict) -> None:
        while True:
            await self._run_scheduled_once(ws, pending)
            await asyncio.sleep(15)

    async def _run_scheduled_once(self, ws, pending: dict) -> None:
        now = self.now()
        local_date = now.date().isoformat()
        active = self.is_answer_time(now)
        for group_id in sorted(self._joined_group_ids):
            async with self._group_reply_locks[group_id]:
                activity = self.memory.get_activity(group_id, local_date)
                if active and not activity["morning_sent"]:
                    if not await self._send_scheduled_message(
                        ws,
                        group_id,
                        self.rng.choice(MORNING_MESSAGES),
                        pending,
                        now=now,
                        morning=True,
                    ):
                        continue
                if (
                    not active
                    and now.hour == self.config.answer_end_hour
                    and now.minute < 10
                    and not activity["night_sent"]
                ):
                    await self._send_scheduled_message(
                        ws,
                        group_id,
                        self.rng.choice(NIGHT_MESSAGES),
                        pending,
                        now=now,
                        night=True,
                    )

        if not active:
            return
        for event in self.memory.due_initial_reminders(now.timestamp()):
            if event["group_id"] not in self._joined_group_ids:
                continue
            event_time = datetime.fromtimestamp(event["event_at"], self.config.timezone)
            message = f"提醒一下：{event['summary']}（时间：{event_time:%Y-%m-%d %H:%M}）"
            async with self._group_reply_locks[event["group_id"]]:
                sent = await self._send_scheduled_message(
                    ws, event["group_id"], message, pending, now=now
                )
                if not sent:
                    continue
                followup = now + timedelta(hours=self.rng.uniform(2, 5))
                followup = self._adjust_to_next_answer_time(followup)
                self.memory.mark_reminded(
                    event["id"], now.timestamp(), followup.timestamp()
                )

        for event in self.memory.due_followups(now.timestamp()):
            if event["group_id"] not in self._joined_group_ids:
                continue
            message = [
                {"type": "at", "data": {"qq": event["source_user_id"]}},
                {"type": "text", "data": {"text": f" 之前提醒的事项还没有看到回复：{event['summary']}"}},
            ]
            async with self._group_reply_locks[event["group_id"]]:
                sent = await self._send_scheduled_message(
                    ws, event["group_id"], message, pending, now=now
                )
                if sent:
                    self.memory.mark_followup_sent(event["id"], now.timestamp())

    async def _send_scheduled_message(
        self, ws, group_id: str, message, pending: dict, **kwargs
    ) -> bool:
        """定时任务发送失败时隔离单个群，不让调度器触发整条连接重建。"""
        try:
            await self._send_group_message(ws, group_id, message, pending, **kwargs)
            return True
        except OneBotActionError as exc:
            self._joined_group_ids.discard(str(group_id))
            logger.warning("群 %s 主动发送失败，已暂停该群：%s", group_id, exc)
            return False
        except Exception:  # noqa: BLE001
            logger.exception("群 %s 主动发送失败，本轮已跳过", group_id)
            return False

    async def _send_group_message(
        self,
        ws,
        group_id: str | int,
        message,
        pending: dict,
        *,
        now: datetime | None = None,
        spontaneous: bool = False,
        morning: bool = False,
        night: bool = False,
    ) -> None:
        output = message
        if isinstance(output, str) and len(output) > self.config.max_reply_chars:
            output = output[: self.config.max_reply_chars].rstrip() + "…"
        await self._send_action(
            ws,
            "send_group_msg",
            {"group_id": int(group_id), "message": output},
            pending,
        )
        sent_at = now or self.now()
        self.memory.record_bot_message(
            group_id,
            sent_at.date().isoformat(),
            sent_at.timestamp(),
            spontaneous=spontaneous,
            morning=morning,
            night=night,
        )

    async def _send_action(self, ws, action: str, params: dict, pending: dict):
        echo = f"{random.randrange(1 << 48):x}-{id(ws)}"
        future = asyncio.get_running_loop().create_future()
        pending[echo] = future
        try:
            await ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=ACTION_TIMEOUT_SECONDS)
            return response.get("data")
        finally:
            pending.pop(echo, None)


def main() -> int:
    load_env_file()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        logger.error("缺少 DEEPSEEK_API_KEY，请在 .env 中填写 DeepSeek API Key。")
        return 2
    llm_client = DeepSeekClient(
        api_key,
        base_url=(os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL).strip(),
        model=(os.getenv("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL).strip(),
        system_prompt=os.getenv("DEEPSEEK_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT).strip(),
        timeout_seconds=_env_int("DEEPSEEK_TIMEOUT_SECONDS", 60),
        max_tokens=_env_int("DEEPSEEK_MAX_TOKENS", 1024),
    )
    config = BotConfig.from_env()
    bot = QQBot(
        (os.getenv("NAPCAT_WS_URL") or DEFAULT_WS_URL).strip(),
        (os.getenv("NAPCAT_WS_TOKEN") or "").strip(),
        llm_client=llm_client,
        config=config,
    )
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("已手动退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
