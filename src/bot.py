#!/usr/bin/env python3
"""有作息、群成员资料和未来事项记忆的 QQ 群 DeepSeek 机器人。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
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
    from .error_logging import install_error_context_handler
    from .memory import MemoryStore
else:  # 支持 README 中的 `python src\bot.py` 直接启动方式。
    from error_logging import install_error_context_handler
    from memory import MemoryStore

RETRY_DELAY_SECONDS = 3.0
ACTION_TIMEOUT_SECONDS = 10.0
DEFAULT_WS_URL = "ws://127.0.0.1:3001"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_SYSTEM_PROMPT = "你是一个友好、自然、简洁的 QQ 群聊助手。请结合可靠的群资料和近期对话直接回答。"
DEFAULT_ERROR_REPLY = "抱歉，AI 服务暂时不可用，请稍后再试。"
DEFAULT_WEB_SEARCH_FAILURE_REPLY = "我不知道，暂时没有查到可靠的联网信息。"
DEFAULT_EMPTY_REPLY = "请在 @ 我后输入想聊的内容。"
DEFAULT_DB_PATH = "data/bot_memory.sqlite3"
DEFAULT_ERROR_LOG_PATH = "logs/error_context.txt"
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
_FORCED_WEB_SEARCH_PATTERN = re.compile(
    r"(?:联网|上网|网络)(?:搜索|查找|查询|查一下|搜一下)|"
    r"(?:搜索|查找|查询|查一下|搜一下)(?:网络|网上|一下)?|"
    r"(?:现在|当前|此刻|当地|北京).{0,8}(?:几点|时间)|"
    r"(?:几点了|现在几点|当前时间|北京时间|当地时间)"
)
_QQ_NUMBER_PATTERN = re.compile(r"QQ\s*\d+", re.IGNORECASE)
_LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)\d{5,}(?!\d)")
_MARKDOWN_URL_PATTERN = re.compile(r"\[([^\]]+)\]\(https?://[^)]+\)")
_PLAIN_URL_PATTERN = re.compile(r"https?://\S+")

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


class ConfigError(ValueError):
    """用户可修复的环境变量配置错误。"""


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None or not raw.strip() else int(raw)
    except ValueError:
        raise ConfigError(f"{name} 必须是整数，当前值为 {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} 不能小于 {minimum}，当前值为 {value}")
    return value


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None or not raw.strip() else float(raw)
    except ValueError:
        raise ConfigError(f"{name} 必须是数字，当前值为 {raw!r}") from None
    if not math.isfinite(value):
        raise ConfigError(f"{name} 必须是有限数字，当前值为 {raw!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} 不能小于 {minimum}，当前值为 {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} 不能大于 {maximum}，当前值为 {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ConfigError(
        f"{name} 必须是 true/false、1/0、yes/no 或 on/off，当前值为 {raw!r}"
    )


def _env_time_minutes(name: str, default: str, *, allow_24: bool = False) -> int:
    raw = (os.getenv(name) or default).strip()
    match = re.fullmatch(r"(\d{2}):(\d{2})", raw)
    if not match:
        raise ConfigError(f"{name} 必须使用 HH:MM 格式，当前值为 {raw!r}")
    hour, minute = map(int, match.groups())
    if allow_24 and hour == 24 and minute == 0:
        return 24 * 60
    if hour > 23 or minute > 59:
        raise ConfigError(f"{name} 不是有效时间，当前值为 {raw!r}")
    return hour * 60 + minute


def _env_messages(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return defaults
    messages = tuple(item.strip() for item in raw.split("||") if item.strip())
    if not messages:
        raise ConfigError(f"{name} 至少需要一条非空消息")
    return messages


def _env_text(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise ConfigError(f"{name} 不能为空")
    return value


def _format_time(minutes: int) -> str:
    return "24:00" if minutes == 1440 else f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class BotConfig:
    active_group_ids: frozenset[str]
    timezone: ZoneInfo
    answer_start_minutes: int = 600
    answer_end_minutes: int = 1140
    spontaneous_replies_enabled: bool = True
    spontaneous_daily_min: int = 60
    spontaneous_daily_max: int = 100
    spontaneous_min_interval_seconds: int = 600
    spontaneous_score_threshold: float = 0.65
    spontaneous_random_weight: float = 0.40
    spontaneous_traffic_weight: float = 0.25
    spontaneous_silence_weight: float = 0.35
    spontaneous_traffic_window_seconds: int = 60
    spontaneous_traffic_full_score_messages: int = 10
    spontaneous_silence_full_score_seconds: int = 1200
    member_sync_interval_seconds: int = 21600
    member_context_match_limit: int = 20
    future_memory_enabled: bool = True
    future_memory_source: str = "active_window_all"
    future_extraction_concurrency: int = 2
    future_context_max_events: int = 20
    reminder_lead_minutes: int = 60
    reminder_followup_min_minutes: int = 120
    reminder_followup_max_minutes: int = 300
    memory_db_path: str = DEFAULT_DB_PATH
    group_context_window_seconds: int = 300
    group_context_max_messages: int = 20
    group_context_message_max_chars: int = 300
    max_history_messages: int = 10
    chat_history_ttl_minutes: int = 30
    max_reply_chars: int = 2000
    morning_greeting_enabled: bool = True
    night_greeting_enabled: bool = True
    morning_messages: tuple[str, ...] = MORNING_MESSAGES
    night_messages: tuple[str, ...] = NIGHT_MESSAGES
    error_reply: str = DEFAULT_ERROR_REPLY
    web_search_failure_reply: str = DEFAULT_WEB_SEARCH_FAILURE_REPLY
    empty_reply: str = DEFAULT_EMPTY_REPLY
    deepseek_timeout_seconds: float = 60.0
    deepseek_max_tokens: int = 1024
    web_search_enabled: bool = True
    web_search_timeout_seconds: float = 90.0
    web_search_log_sources: bool = True
    web_search_max_log_sources: int = 5
    error_log_enabled: bool = True
    error_log_path: str = DEFAULT_ERROR_LOG_PATH
    error_log_before_records: int = 30
    error_log_after_records: int = 10
    error_log_max_bytes: int = 1_048_576
    error_log_backup_count: int = 2

    @classmethod
    def from_env(cls) -> "BotConfig":
        group_ids = frozenset(
            value.strip()
            for value in (os.getenv("ACTIVE_GROUP_IDS") or "").split(",")
            if value.strip()
        )
        invalid_group_ids = sorted(value for value in group_ids if not value.isdigit())
        if invalid_group_ids:
            raise ConfigError("ACTIVE_GROUP_IDS 只能包含数字群号，并使用英文逗号分隔")
        timezone_name = (os.getenv("BOT_TIMEZONE") or "Asia/Shanghai").strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            raise ConfigError(f"BOT_TIMEZONE 不是有效 IANA 时区：{timezone_name!r}") from None
        source = (os.getenv("FUTURE_MEMORY_SOURCE") or "active_window_all").strip()
        if source not in {"active_window_all", "participated"}:
            raise ConfigError(
                "FUTURE_MEMORY_SOURCE 必须是 active_window_all 或 participated"
            )
        start = _env_time_minutes("ANSWER_START_TIME", "10:00")
        end = _env_time_minutes("ANSWER_END_TIME", "19:00", allow_24=True)
        if end <= start:
            raise ConfigError("ANSWER_END_TIME 必须晚于 ANSWER_START_TIME")
        daily_min = _env_int("SPONTANEOUS_DAILY_MIN", 60, minimum=0)
        daily_max = _env_int("SPONTANEOUS_DAILY_MAX", 100, minimum=0)
        if daily_max < daily_min:
            raise ConfigError("SPONTANEOUS_DAILY_MAX 不能小于 SPONTANEOUS_DAILY_MIN")
        random_weight = _env_float("SPONTANEOUS_RANDOM_WEIGHT", 0.40, minimum=0)
        traffic_weight = _env_float("SPONTANEOUS_TRAFFIC_WEIGHT", 0.25, minimum=0)
        silence_weight = _env_float("SPONTANEOUS_SILENCE_WEIGHT", 0.35, minimum=0)
        if abs(random_weight + traffic_weight + silence_weight - 1.0) > 1e-6:
            raise ConfigError("三个 SPONTANEOUS_*_WEIGHT 之和必须等于 1")
        followup_min = _env_int("REMINDER_FOLLOWUP_MIN_MINUTES", 120, minimum=0)
        followup_max = _env_int("REMINDER_FOLLOWUP_MAX_MINUTES", 300, minimum=0)
        if followup_max < followup_min:
            raise ConfigError(
                "REMINDER_FOLLOWUP_MAX_MINUTES 不能小于 REMINDER_FOLLOWUP_MIN_MINUTES"
            )
        memory_path = (os.getenv("MEMORY_DB_PATH") or DEFAULT_DB_PATH).strip()
        if not memory_path:
            raise ConfigError("MEMORY_DB_PATH 不能为空")
        error_log_path = (os.getenv("ERROR_LOG_PATH") or DEFAULT_ERROR_LOG_PATH).strip()
        if not error_log_path:
            raise ConfigError("ERROR_LOG_PATH 不能为空")
        return cls(
            active_group_ids=group_ids,
            timezone=timezone,
            answer_start_minutes=start,
            answer_end_minutes=end,
            spontaneous_replies_enabled=_env_bool("SPONTANEOUS_REPLIES_ENABLED", True),
            spontaneous_daily_min=daily_min,
            spontaneous_daily_max=daily_max,
            spontaneous_min_interval_seconds=_env_int(
                "SPONTANEOUS_MIN_INTERVAL_SECONDS", 600, minimum=0
            ),
            spontaneous_score_threshold=_env_float(
                "SPONTANEOUS_SCORE_THRESHOLD", 0.65, minimum=0, maximum=1
            ),
            spontaneous_random_weight=random_weight,
            spontaneous_traffic_weight=traffic_weight,
            spontaneous_silence_weight=silence_weight,
            spontaneous_traffic_window_seconds=_env_int(
                "SPONTANEOUS_TRAFFIC_WINDOW_SECONDS", 60, minimum=1
            ),
            spontaneous_traffic_full_score_messages=_env_int(
                "SPONTANEOUS_TRAFFIC_FULL_SCORE_MESSAGES", 10, minimum=1
            ),
            spontaneous_silence_full_score_seconds=_env_int(
                "SPONTANEOUS_SILENCE_FULL_SCORE_SECONDS", 1200, minimum=1
            ),
            member_sync_interval_seconds=_env_int(
                "MEMBER_SYNC_INTERVAL_SECONDS", 21600, minimum=60
            ),
            member_context_match_limit=_env_int(
                "MEMBER_CONTEXT_MATCH_LIMIT", 20, minimum=0
            ),
            future_memory_enabled=_env_bool("FUTURE_MEMORY_ENABLED", True),
            future_memory_source=source,
            future_extraction_concurrency=_env_int(
                "FUTURE_EXTRACTION_CONCURRENCY", 2, minimum=1
            ),
            future_context_max_events=_env_int(
                "FUTURE_CONTEXT_MAX_EVENTS", 20, minimum=0
            ),
            reminder_lead_minutes=_env_int("REMINDER_LEAD_MINUTES", 60, minimum=0),
            reminder_followup_min_minutes=followup_min,
            reminder_followup_max_minutes=followup_max,
            memory_db_path=memory_path,
            group_context_window_seconds=_env_int(
                "GROUP_CONTEXT_WINDOW_SECONDS", 300, minimum=1
            ),
            group_context_max_messages=_env_int(
                "GROUP_CONTEXT_MAX_MESSAGES", 20, minimum=0
            ),
            group_context_message_max_chars=_env_int(
                "GROUP_CONTEXT_MESSAGE_MAX_CHARS", 300, minimum=1
            ),
            max_history_messages=_env_int("CHAT_HISTORY_MESSAGES", 10, minimum=0),
            chat_history_ttl_minutes=_env_int(
                "CHAT_HISTORY_TTL_MINUTES", 30, minimum=0
            ),
            max_reply_chars=_env_int("MAX_REPLY_CHARS", 2000, minimum=1),
            morning_greeting_enabled=_env_bool("MORNING_GREETING_ENABLED", True),
            night_greeting_enabled=_env_bool("NIGHT_GREETING_ENABLED", True),
            morning_messages=_env_messages("MORNING_GREETING_MESSAGES", MORNING_MESSAGES),
            night_messages=_env_messages("NIGHT_GREETING_MESSAGES", NIGHT_MESSAGES),
            error_reply=_env_text("AI_ERROR_REPLY", DEFAULT_ERROR_REPLY),
            web_search_failure_reply=_env_text(
                "WEB_SEARCH_FAILURE_REPLY", DEFAULT_WEB_SEARCH_FAILURE_REPLY
            ),
            empty_reply=_env_text("EMPTY_MENTION_REPLY", DEFAULT_EMPTY_REPLY),
            deepseek_timeout_seconds=_env_float(
                "DEEPSEEK_TIMEOUT_SECONDS", 60.0, minimum=0.1
            ),
            deepseek_max_tokens=_env_int("DEEPSEEK_MAX_TOKENS", 1024, minimum=1),
            web_search_enabled=_env_bool("WEB_SEARCH_ENABLED", True),
            web_search_timeout_seconds=_env_float(
                "WEB_SEARCH_TIMEOUT_SECONDS", 90.0, minimum=0.1
            ),
            web_search_log_sources=_env_bool("WEB_SEARCH_LOG_SOURCES", True),
            web_search_max_log_sources=_env_int(
                "WEB_SEARCH_MAX_LOG_SOURCES", 5, minimum=0
            ),
            error_log_enabled=_env_bool("ERROR_LOG_ENABLED", True),
            error_log_path=error_log_path,
            error_log_before_records=_env_int(
                "ERROR_LOG_BEFORE_RECORDS", 30, minimum=0
            ),
            error_log_after_records=_env_int(
                "ERROR_LOG_AFTER_RECORDS", 10, minimum=0
            ),
            error_log_max_bytes=_env_int(
                "ERROR_LOG_MAX_BYTES", 1_048_576, minimum=1024
            ),
            error_log_backup_count=_env_int(
                "ERROR_LOG_BACKUP_COUNT", 2, minimum=0
            ),
        )


class DeepSeekAPIError(RuntimeError):
    pass


class DeepSeekWebSearchError(DeepSeekAPIError):
    """联网聊天失败，调用方应使用不确定性提示而不是编造实时答案。"""


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
    """DeepSeek 客户端：Responses API 聊天，Chat Completions 提取日期。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_DEEPSEEK_BASE_URL,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        timeout_seconds: float = 60.0,
        max_tokens: int = 1024,
        web_search_enabled: bool = True,
        web_search_timeout_seconds: float = 90.0,
        web_search_log_sources: bool = True,
        web_search_max_log_sources: int = 5,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.system_prompt = system_prompt
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.web_search_enabled = web_search_enabled
        self.web_search_timeout_seconds = web_search_timeout_seconds
        self.web_search_log_sources = web_search_log_sources
        self.web_search_max_log_sources = web_search_max_log_sources

    @property
    def api_root(self) -> str:
        for suffix in ("/chat/completions", "/responses"):
            if self.base_url.endswith(suffix):
                return self.base_url[: -len(suffix)]
        return self.base_url

    @property
    def endpoint(self) -> str:
        return f"{self.api_root}/chat/completions"

    @property
    def responses_endpoint(self) -> str:
        return f"{self.api_root}/responses"

    async def chat(
        self,
        history: list[dict],
        user_text: str,
        *,
        context: str = "",
        now: datetime | None = None,
    ) -> str:
        if self.web_search_enabled:
            try:
                return await self._chat_with_web(history, user_text, context=context, now=now)
            except DeepSeekWebSearchError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise DeepSeekWebSearchError(str(exc)) from exc
        return await self._chat_completion(history, user_text, context=context)

    async def _chat_completion(
        self, history: list[dict], user_text: str, *, context: str = ""
    ) -> str:
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
            self.endpoint,
            self.timeout_seconds,
        )
        return self._content(payload)

    async def _chat_with_web(
        self,
        history: list[dict],
        user_text: str,
        *,
        context: str = "",
        now: datetime | None = None,
    ) -> str:
        safe_context = self._sanitize_web_context(context)
        safe_user_text = self._sanitize_web_context(user_text)
        current_time = now.isoformat() if now is not None else "未提供"
        instructions = (
            f"{self.system_prompt}\n"
            "你可以使用 web_search 获取天气、时间、新闻和其他实时或外部信息。"
            "本地资料存在时以本地资料为准；用户询问的事件未出现在本地资料中且需要外部事实时，应联网搜索。"
            "不要把无关的群成员姓名、身份或聊天内容写入搜索词。"
            "回答中不要附来源列表、引用链接或 URL；只给出简洁结论。"
            f"机器人收到消息时的本地时间：{current_time}。"
        )
        input_items: list[dict] = []
        if safe_context:
            input_items.append(
                {
                    "role": "system",
                    "content": "以下是完成回答所需的最小本地资料，不代表公开网络信息：\n" + safe_context,
                }
            )
        input_items.extend(
            {
                "role": item["role"],
                "content": self._sanitize_web_context(str(item["content"])),
            }
            for item in history
            if item.get("role") in {"user", "assistant"} and item.get("content")
        )
        input_items.append({"role": "user", "content": safe_user_text})
        force_search = bool(_FORCED_WEB_SEARCH_PATTERN.search(user_text))
        body = {
            "model": self.model,
            "instructions": instructions,
            "input": input_items,
            "tools": [{"type": "web_search"}],
            "tool_choice": {"type": "web_search"} if force_search else "auto",
            "stream": False,
            "max_output_tokens": self.max_tokens,
        }
        try:
            payload = await asyncio.to_thread(
                self._post,
                body,
                self.responses_endpoint,
                self.web_search_timeout_seconds,
            )
            answer, used_web, sources, actions = self._responses_content(payload)
        except DeepSeekAPIError as exc:
            raise DeepSeekWebSearchError(str(exc)) from exc
        if force_search and not used_web:
            raise DeepSeekWebSearchError("DeepSeek 未执行被强制要求的联网搜索")
        self._log_web_search(used_web, sources, actions)
        return self._strip_source_links(answer) if used_web else answer

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
            self.endpoint,
            self.timeout_seconds,
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

    def _post(self, body: dict, endpoint: str, timeout_seconds: float) -> dict:
        request = Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
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
    def _sanitize_web_context(context: str) -> str:
        without_qq = _QQ_NUMBER_PATTERN.sub("QQ号已隐藏", context)
        return _LONG_NUMBER_PATTERN.sub("[数字标识已隐藏]", without_qq)

    @staticmethod
    def _responses_content(payload: dict) -> tuple[str, bool, list[dict], list[dict]]:
        if payload.get("status") != "completed":
            reason = payload.get("error") or payload.get("incomplete_details") or "未知原因"
            raise DeepSeekAPIError(f"DeepSeek Responses 未完成：{reason}")
        texts: list[str] = []
        sources: list[dict] = []
        actions: list[dict] = []
        used_web = False
        output = payload.get("output")
        if not isinstance(output, list):
            raise DeepSeekAPIError("DeepSeek Responses 返回了无法识别的数据")
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "web_search_call":
                used_web = True
                action = item.get("action")
                if isinstance(action, dict):
                    actions.append(action)
                    action_url = action.get("url")
                    if isinstance(action_url, str) and action_url.startswith(
                        ("http://", "https://")
                    ):
                        sources.append(
                            {
                                "title": str(action.get("title") or "搜索访问页面"),
                                "url": action_url,
                            }
                        )
                continue
            if item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                value = part.get("text")
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())
                for annotation in part.get("annotations") or []:
                    if not isinstance(annotation, dict):
                        continue
                    citation = annotation.get("url_citation")
                    candidate = citation if isinstance(citation, dict) else annotation
                    url = candidate.get("url")
                    if isinstance(url, str) and url.startswith(("http://", "https://")):
                        sources.append(
                            {"title": str(candidate.get("title") or "未命名来源"), "url": url}
                        )
        answer = "\n".join(texts).strip()
        if not answer:
            raise DeepSeekAPIError("DeepSeek Responses 返回了空回复")
        return answer, used_web, sources, actions

    def _log_web_search(
        self, used_web: bool, sources: list[dict], actions: list[dict]
    ) -> None:
        if not used_web:
            logger.info("DeepSeek 本次回答未使用联网搜索。")
            return
        if not self.web_search_log_sources:
            logger.info("DeepSeek 已执行联网搜索；来源日志已关闭。")
            return
        unique: list[dict] = []
        seen_urls: set[str] = set()
        for source in sources:
            url = source["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            unique.append(source)
        limit = self.web_search_max_log_sources
        if limit == 0:
            logger.info("DeepSeek 已执行联网搜索；来源 URL 记录上限为 0。")
            return
        if unique and limit:
            logger.info("DeepSeek 已执行联网搜索，来源（最多 %s 条）：", limit)
            for source in unique[:limit]:
                safe_title = self._sanitize_web_context(source["title"][:120])
                safe_url = self._sanitize_log_url(source["url"])
                logger.info("- %s：%s", safe_title, safe_url)
            return
        action_types = sorted(
            {str(action.get("type") or "unknown") for action in actions}
        )
        logger.info(
            "DeepSeek 已执行联网搜索，但接口未返回来源 URL；搜索动作：%s",
            ", ".join(action_types) if action_types else "未提供",
        )

    @staticmethod
    def _strip_source_links(answer: str) -> str:
        answer = _MARKDOWN_URL_PATTERN.sub(r"\1", answer)
        answer = _PLAIN_URL_PATTERN.sub("", answer)
        return re.sub(r"[ \t]+\n", "\n", answer).strip()

    @staticmethod
    def _sanitize_log_url(value: str) -> str:
        parsed = urlparse(value)
        without_private_query = parsed._replace(query="", fragment="")
        return _LONG_NUMBER_PATTERN.sub(
            "[数字标识已隐藏]", urlunparse(without_private_query)
        )

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
        self._history_last_active: dict[tuple[str, str], float] = {}
        self._conversation_locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._group_reply_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._extraction_semaphore = asyncio.Semaphore(
            self.config.future_extraction_concurrency
        )
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
        minute = current.hour * 60 + current.minute
        return self.config.answer_start_minutes <= minute < self.config.answer_end_minutes

    async def run(self) -> None:
        history_ttl = (
            f"闲置 {self.config.chat_history_ttl_minutes} 分钟过期"
            if self.config.chat_history_ttl_minutes
            else "不按时间过期"
        )
        logger.info(
            "配置已加载：工作时段 %s-%s，主动回复%s（每日随机上限 %s-%s），"
            "群聊上下文 %s 秒/%s 条，对话%s，未来记忆%s，联网搜索%s",
            _format_time(self.config.answer_start_minutes),
            _format_time(self.config.answer_end_minutes),
            "启用" if self.config.spontaneous_replies_enabled else "禁用",
            self.config.spontaneous_daily_min,
            self.config.spontaneous_daily_max,
            self.config.group_context_window_seconds,
            self.config.group_context_max_messages,
            history_ttl,
            "启用" if self.config.future_memory_enabled else "禁用",
            "启用" if self.config.web_search_enabled else "禁用",
        )
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
            # 同时看到旧的最后发言时间并一起越过配置的最小间隔。
            async with self._group_reply_locks[group_id]:
                if mentioned:
                    should_reply = True
                elif text_value:
                    should_reply = self._should_reply_spontaneously(group_id, now)
                    spontaneous = should_reply

                if mentioned and not text_value:
                    await self._send_group_message(
                        ws, group_id, self.config.empty_reply, pending, now=now
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
            should_extract = self.config.future_memory_enabled and text_value and (
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
                ws, group_id, self.config.error_reply, pending, now=now, spontaneous=spontaneous
            )
            return
        conversation_id = (group_id, user_id)
        async with self._conversation_locks[conversation_id]:
            self._prune_expired_histories(now)
            history = self._histories.get(conversation_id, [])
            context = self._build_group_context(
                group_id, user_id, extract_mentioned_ids(payload, self_id), question, now
            )
            try:
                answer = await self.llm_client.chat(
                    history, question, context=context, now=now
                )
            except DeepSeekWebSearchError:
                logger.exception("DeepSeek 联网回答失败")
                await self._send_group_message(
                    ws,
                    group_id,
                    self.config.web_search_failure_reply,
                    pending,
                    now=now,
                    spontaneous=spontaneous,
                )
                return
            except Exception:  # noqa: BLE001
                logger.exception("调用 DeepSeek 失败")
                await self._send_group_message(
                    ws, group_id, self.config.error_reply, pending, now=now, spontaneous=spontaneous
                )
                return
            updated = history + [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ]
            if self.config.max_history_messages:
                self._histories[conversation_id] = updated[
                    -self.config.max_history_messages :
                ]
                self._history_last_active[conversation_id] = now.timestamp()
            else:
                self._histories.pop(conversation_id, None)
                self._history_last_active.pop(conversation_id, None)
            await self._send_group_message(
                ws, group_id, answer, pending, now=now, spontaneous=spontaneous
            )

    def _prune_expired_histories(self, now: datetime) -> None:
        ttl_seconds = self.config.chat_history_ttl_minutes * 60
        if not ttl_seconds:
            return
        cutoff = now.timestamp() - ttl_seconds
        expired = [
            key for key, last_active in self._history_last_active.items() if last_active < cutoff
        ]
        for key in expired:
            self._histories.pop(key, None)
            self._history_last_active.pop(key, None)

    def _remember_recent(
        self, group_id: str, user_id: str, text_value: str, payload: dict, now: datetime
    ) -> None:
        sender = payload.get("sender") or {}
        display_name = str(sender.get("card") or sender.get("nickname") or user_id)
        queue = self._recent_messages[group_id]
        queue.append(
            (
                now.timestamp(),
                user_id,
                display_name,
                text_value[: self.config.group_context_message_max_chars],
            )
        )
        retention = max(
            self.config.group_context_window_seconds,
            self.config.spontaneous_traffic_window_seconds,
        )
        cutoff = now.timestamp() - retention
        while queue and queue[0][0] < cutoff:
            queue.popleft()
        storage_limit = max(
            self.config.group_context_max_messages,
            self.config.spontaneous_traffic_full_score_messages,
        )
        while len(queue) > storage_limit:
            queue.popleft()

    def _should_reply_spontaneously(self, group_id: str, now: datetime) -> bool:
        if not self.config.spontaneous_replies_enabled:
            return False
        activity = self.memory.get_activity(group_id, now.date().isoformat())
        daily_limit = self.memory.ensure_daily_spontaneous_limit(
            group_id,
            now.date().isoformat(),
            self.config.spontaneous_daily_min,
            self.config.spontaneous_daily_max,
            self.rng.randint(
                self.config.spontaneous_daily_min,
                self.config.spontaneous_daily_max,
            ),
        )
        if activity["spontaneous_count"] >= daily_limit:
            return False
        last_sent = activity["last_bot_sent_at"]
        silence = float("inf") if last_sent is None else max(0.0, now.timestamp() - last_sent)
        if silence < self.config.spontaneous_min_interval_seconds:
            return False
        traffic_cutoff = now.timestamp() - self.config.spontaneous_traffic_window_seconds
        message_count = sum(
            1 for sent_at, *_ in self._recent_messages[group_id] if sent_at >= traffic_cutoff
        )
        traffic_score = min(
            message_count / self.config.spontaneous_traffic_full_score_messages, 1.0
        )
        silence_score = min(
            silence / self.config.spontaneous_silence_full_score_seconds, 1.0
        )
        score = (
            self.config.spontaneous_random_weight * self.rng.random()
            + self.config.spontaneous_traffic_weight * traffic_score
            + self.config.spontaneous_silence_weight * silence_score
        )
        logger.debug(
            "群 %s 主动回复评分 %.3f（近 %s 秒 %s 条）",
            group_id,
            score,
            self.config.spontaneous_traffic_window_seconds,
            message_count,
        )
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
            group_id,
            question,
            include_inactive=include_inactive,
            limit=self.config.member_context_match_limit,
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

        events = (
            self.memory.get_active_events(
                group_id, now.timestamp(), limit=self.config.future_context_max_events
            )
            if self.config.future_memory_enabled
            else []
        )
        if events:
            lines.append("仍有效的未来事项：")
            for event in events:
                event_time = datetime.fromtimestamp(event["event_at"], self.config.timezone)
                lines.append(f"- {event_time:%Y-%m-%d %H:%M}：{event['summary']}")

        recent_cutoff = now.timestamp() - self.config.group_context_window_seconds
        recent = [
            item for item in self._recent_messages[group_id] if item[0] >= recent_cutoff
        ][-self.config.group_context_max_messages :]
        if self.config.group_context_max_messages == 0:
            recent = []
        if recent:
            lines.append(f"最近 {self.config.group_context_window_seconds} 秒群聊：")
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
        day = datetime.combine(local.date(), time(0), self.config.timezone)
        start = day + timedelta(minutes=self.config.answer_start_minutes)
        end = day + timedelta(minutes=self.config.answer_end_minutes)
        if local < start:
            previous_day = day - timedelta(days=1)
            return (
                previous_day
                + timedelta(minutes=self.config.answer_end_minutes)
                - timedelta(seconds=1)
            )
        if local >= end:
            return end - timedelta(seconds=1)
        return local

    def _adjust_to_next_answer_time(self, moment: datetime) -> datetime:
        local = moment.astimezone(self.config.timezone)
        day = datetime.combine(local.date(), time(0), self.config.timezone)
        start = day + timedelta(minutes=self.config.answer_start_minutes)
        end = day + timedelta(minutes=self.config.answer_end_minutes)
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
        current_minute = now.hour * 60 + now.minute
        for group_id in sorted(self._joined_group_ids):
            async with self._group_reply_locks[group_id]:
                activity = self.memory.get_activity(group_id, local_date)
                if (
                    active
                    and self.config.morning_greeting_enabled
                    and not activity["morning_sent"]
                ):
                    if not await self._send_scheduled_message(
                        ws,
                        group_id,
                        self.rng.choice(self.config.morning_messages),
                        pending,
                        now=now,
                        morning=True,
                    ):
                        continue
                if (
                    not active
                    and self.config.night_greeting_enabled
                    and self.config.answer_end_minutes <= current_minute
                    < self.config.answer_end_minutes + 10
                    and not activity["night_sent"]
                ):
                    await self._send_scheduled_message(
                        ws,
                        group_id,
                        self.rng.choice(self.config.night_messages),
                        pending,
                        now=now,
                        night=True,
                    )

        if not active or not self.config.future_memory_enabled:
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
                followup = now + timedelta(
                    minutes=self.rng.uniform(
                        self.config.reminder_followup_min_minutes,
                        self.config.reminder_followup_max_minutes,
                    )
                )
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
    try:
        config = BotConfig.from_env()
        deepseek_base_url = _env_text("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL)
        deepseek_model = _env_text("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL)
        deepseek_system_prompt = _env_text(
            "DEEPSEEK_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT
        )
        napcat_ws_url = _env_text("NAPCAT_WS_URL", DEFAULT_WS_URL)
    except ConfigError as exc:
        logger.error("配置错误：%s", exc)
        return 2
    if config.error_log_enabled:
        install_error_context_handler(
            config.error_log_path,
            before_records=config.error_log_before_records,
            after_records=config.error_log_after_records,
            max_bytes=config.error_log_max_bytes,
            backup_count=config.error_log_backup_count,
        )
        logger.info(
            "错误现场日志已启用：仅在报错时写入 %s（前 %s 条，后 %s 条）",
            config.error_log_path,
            config.error_log_before_records,
            config.error_log_after_records,
        )
    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        logger.error("缺少 DEEPSEEK_API_KEY，请在 .env 中填写 DeepSeek API Key。")
        return 2
    llm_client = DeepSeekClient(
        api_key,
        base_url=deepseek_base_url,
        model=deepseek_model,
        system_prompt=deepseek_system_prompt,
        timeout_seconds=config.deepseek_timeout_seconds,
        max_tokens=config.deepseek_max_tokens,
        web_search_enabled=config.web_search_enabled,
        web_search_timeout_seconds=config.web_search_timeout_seconds,
        web_search_log_sources=config.web_search_log_sources,
        web_search_max_log_sources=config.web_search_max_log_sources,
    )
    bot = QQBot(
        napcat_ws_url,
        (os.getenv("NAPCAT_WS_TOKEN") or "").strip(),
        llm_client=llm_client,
        config=config,
    )
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("已手动退出。")
    except Exception:
        logger.exception("机器人因未处理异常停止")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
