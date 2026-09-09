"""消息解析、智能回答策略、同步、未来记忆与提醒测试。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from src.bot import (
    BotConfig,
    DeepSeekClient,
    OneBotActionError,
    QQBot,
    extract_mentioned_ids,
    extract_message_text,
    is_at_self,
)
from src.memory import MemoryStore

TZ = ZoneInfo("Asia/Shanghai")
SELF_ID = "10001"
GROUP_ID = "30003"


def at_time(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 9, hour, minute, tzinfo=TZ)


def group_event(message, *, user_id="20002", group_id=GROUP_ID, message_id="40004"):
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": SELF_ID,
        "user_id": user_id,
        "group_id": group_id,
        "message_id": message_id,
        "message": message,
        "raw_message": message if isinstance(message, str) else "",
        "sender": {"user_id": user_id, "nickname": f"用户{user_id}", "card": ""},
    }


def mention_event(text="你好", *, user_id="20002"):
    return group_event(
        [
            {"type": "at", "data": {"qq": SELF_ID}},
            {"type": "text", "data": {"text": f" {text}"}},
        ],
        user_id=user_id,
    )


class FakeWebSocket:
    def __init__(self, pending, responses=None):
        self.pending = pending
        self.responses = responses or {}
        self.sent = []

    async def send(self, raw: str):
        action = json.loads(raw)
        self.sent.append(action)
        response_data = self.responses.get(action["action"], {})
        if callable(response_data):
            response_data = response_data(action["params"])
        future = self.pending[action["echo"]]
        future.set_result({"status": "ok", "retcode": 0, "data": response_data})


class FakeLLM:
    def __init__(self, *, events=None):
        self.chat_calls = []
        self.extract_calls = []
        self.events = events or []

    async def chat(self, history, user_text, *, context=""):
        self.chat_calls.append((list(history), user_text, context))
        return f"AI:{user_text}"

    async def extract_future_events(self, text, now):
        self.extract_calls.append((text, now))
        return list(self.events)


class FixedRNG:
    def __init__(self, value=1.0, uniform_value=3.0):
        self.value = value
        self.uniform_value = uniform_value

    def random(self):
        return self.value

    def uniform(self, _start, _end):
        return self.uniform_value

    def choice(self, values):
        return values[0]


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class MessageParsingTest(unittest.TestCase):
    def test_array_mentions_and_text(self):
        event = group_event(
            [
                {"type": "at", "data": {"qq": SELF_ID}},
                {"type": "at", "data": {"qq": "23333"}},
                {"type": "text", "data": {"text": " 你好"}},
            ]
        )
        self.assertTrue(is_at_self(event, SELF_ID))
        self.assertEqual(extract_mentioned_ids(event, SELF_ID), ["23333"])
        self.assertEqual(extract_message_text(event), "你好")

    def test_raw_cq_and_at_all(self):
        self.assertEqual(
            extract_message_text(group_event("[CQ:at,qq=10001] 1 &amp; 2")),
            "1 & 2",
        )
        self.assertFalse(is_at_self(group_event("[CQ:at,qq=all] 大家好"), SELF_ID))


class DeepSeekClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_includes_local_context(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeHTTPResponse({"choices": [{"message": {"content": "回复"}}]})

        client = DeepSeekClient("test-key", model="test-model", timeout_seconds=12)
        with patch("src.bot.urlopen", fake_urlopen):
            answer = await client.chat([], "谁是群主", context="张三是群主")
        self.assertEqual(answer, "回复")
        self.assertIn("张三是群主", captured["body"]["messages"][1]["content"])
        self.assertEqual(captured["timeout"], 12)

    async def test_extract_future_event_json(self):
        response = {
            "choices": [
                {"message": {"content": '{"events":[{"summary":"开会","event_at":"2026-09-10T10:00:00+08:00"}]}'}}
            ]
        }
        client = DeepSeekClient("test-key")
        with patch("src.bot.urlopen", return_value=FakeHTTPResponse(response)):
            events = await client.extract_future_events("明天十点开会", at_time(11))
        self.assertEqual(events[0]["summary"], "开会")


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "memory.sqlite3")
        self.current = at_time(11)
        self.llm = FakeLLM()
        self.config = BotConfig(active_group_ids=frozenset({GROUP_ID}), timezone=TZ)
        self.bot = QQBot(
            "ws://test",
            llm_client=self.llm,
            config=self.config,
            memory=self.store,
            now_provider=lambda: self.current,
            rng=FixedRNG(),
        )
        self.pending = {}
        self.ws = FakeWebSocket(self.pending)
        self.bot._joined_group_ids.add(GROUP_ID)

    async def asyncTearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    async def test_at_is_guaranteed_inside_window_and_silent_outside(self):
        await self.bot._handle_group_message(self.ws, mention_event("在吗"), self.pending)
        self.assertEqual(self.llm.chat_calls[0][1], "在吗")
        self.assertEqual(self.ws.sent[-1]["params"]["message"], "AI:在吗")
        self.current = at_time(19)
        await self.bot._handle_group_message(self.ws, mention_event("还在吗"), self.pending)
        self.assertEqual(len(self.llm.chat_calls), 1)

    async def test_spontaneous_score_interval_and_daily_limit(self):
        event = group_event([{"type": "text", "data": {"text": "普通话题"}}])
        await self.bot._handle_group_message(self.ws, event, self.pending)
        self.assertEqual(len(self.llm.chat_calls), 1)

        self.current += timedelta(seconds=100)
        await self.bot._handle_group_message(self.ws, event, self.pending)
        self.assertEqual(len(self.llm.chat_calls), 1)

        limited_config = BotConfig(
            active_group_ids=frozenset({GROUP_ID}), timezone=TZ, spontaneous_daily_limit=1
        )
        limited = QQBot(
            "ws://test",
            llm_client=self.llm,
            config=limited_config,
            memory=self.store,
            now_provider=lambda: self.current + timedelta(hours=1),
            rng=FixedRNG(),
        )
        await limited._handle_group_message(self.ws, event, self.pending)
        self.assertEqual(len(self.llm.chat_calls), 1)

    async def test_concurrent_messages_cannot_bypass_group_interval(self):
        first = group_event(
            [{"type": "text", "data": {"text": "第一条"}}],
            user_id="20002",
            message_id="1",
        )
        second = group_event(
            [{"type": "text", "data": {"text": "第二条"}}],
            user_id="20003",
            message_id="2",
        )
        await asyncio.gather(
            self.bot._handle_group_message(self.ws, first, self.pending),
            self.bot._handle_group_message(self.ws, second, self.pending),
        )
        self.assertEqual(len(self.llm.chat_calls), 1)

    async def test_member_context_knows_roles_titles_and_named_people(self):
        self.store.sync_members(
            GROUP_ID,
            [
                {"user_id": "20002", "nickname": "提问者", "role": "member"},
                {"user_id": "30001", "nickname": "张三", "card": "老张", "role": "owner"},
                {"user_id": "30002", "nickname": "李四", "role": "admin", "title": "群内专家"},
            ],
            self.current.timestamp(),
        )
        await self.bot._handle_group_message(
            self.ws, mention_event("李四是什么身份"), self.pending
        )
        context = self.llm.chat_calls[0][2]
        self.assertIn("老张", context)
        self.assertIn("群主", context)
        self.assertIn("李四", context)
        self.assertIn("管理员", context)
        self.assertIn("群内专家", context)

    async def test_full_group_sync_uses_onebot_member_list(self):
        responses = {
            "get_group_list": [{"group_id": int(GROUP_ID), "group_name": "测试群"}],
            "get_group_member_list": [
                {"user_id": 9, "nickname": "群主", "role": "owner", "title": "创始人"}
            ],
        }
        ws = FakeWebSocket(self.pending, responses)
        await self.bot._sync_all_groups(ws, self.pending)
        self.assertEqual(self.store.get_member(GROUP_ID, "9")["role"], "owner")
        self.assertEqual(
            [item["action"] for item in ws.sent],
            ["get_group_list", "get_group_member_list"],
        )

    async def test_full_sync_skips_allowlisted_group_bot_has_not_joined(self):
        other_group = "99999"
        self.bot.config = BotConfig(
            active_group_ids=frozenset({GROUP_ID, other_group}), timezone=TZ
        )
        responses = {
            "get_group_list": [{"group_id": int(GROUP_ID), "group_name": "测试群"}],
            "get_group_member_list": [],
        }
        ws = FakeWebSocket(self.pending, responses)
        await self.bot._sync_all_groups(ws, self.pending)
        member_calls = [item for item in ws.sent if item["action"] == "get_group_member_list"]
        self.assertEqual(len(member_calls), 1)
        self.assertEqual(member_calls[0]["params"]["group_id"], int(GROUP_ID))
        self.assertEqual(self.bot._joined_group_ids, {GROUP_ID})

    async def test_failed_scheduled_send_pauses_group_without_raising(self):
        self.bot._send_group_message = AsyncMock(
            side_effect=OneBotActionError(1200, "发送失败，你已被移出该群，请重新加群。")
        )
        await self.bot._run_scheduled_once(self.ws, self.pending)
        self.assertNotIn(GROUP_ID, self.bot._joined_group_ids)
        await self.bot._run_scheduled_once(self.ws, self.pending)
        self.bot._send_group_message.assert_awaited_once()

    async def test_notice_debounces_and_refreshes_group(self):
        self.bot._sync_group = AsyncMock()
        notice = {"post_type": "notice", "notice_type": "group_admin", "group_id": GROUP_ID}
        with patch("src.bot.asyncio.sleep", new=AsyncMock()):
            await self.bot._handle_notice(self.ws, notice, self.pending)
        self.bot._sync_group.assert_awaited_once_with(self.ws, self.pending, GROUP_ID)

    async def test_greeting_and_night_are_sent_once(self):
        await self.bot._run_scheduled_once(self.ws, self.pending)
        await self.bot._run_scheduled_once(self.ws, self.pending)
        self.assertEqual(len(self.ws.sent), 1)
        self.current = at_time(19, 1)
        await self.bot._run_scheduled_once(self.ws, self.pending)
        await self.bot._run_scheduled_once(self.ws, self.pending)
        self.assertEqual(len(self.ws.sent), 2)

    async def test_active_window_all_extracts_even_without_reply(self):
        self.bot.rng = FixedRNG(value=0.0)
        event = group_event([{"type": "text", "data": {"text": "明天十点开会"}}])
        await self.bot._handle_group_message(self.ws, event, self.pending)
        self.assertEqual(len(self.llm.chat_calls), 0)
        self.assertEqual(len(self.llm.extract_calls), 1)

    async def test_participated_mode_skips_unanswered_message(self):
        config = BotConfig(
            active_group_ids=frozenset({GROUP_ID}),
            timezone=TZ,
            future_memory_source="participated",
        )
        bot = QQBot(
            "ws://test",
            llm_client=self.llm,
            config=config,
            memory=self.store,
            now_provider=lambda: self.current,
            rng=FixedRNG(value=0.0),
        )
        event = group_event([{"type": "text", "data": {"text": "明天十点开会"}}])
        await bot._handle_group_message(self.ws, event, self.pending)
        self.assertEqual(self.llm.extract_calls, [])

    async def test_future_event_reminder_ack_and_expired_followup(self):
        event_at = self.current + timedelta(hours=1)
        self.store.add_future_event(
            group_id=GROUP_ID,
            source_user_id="20002",
            source_message_id="1",
            summary="参加会议",
            event_at=event_at.timestamp(),
            remind_at=self.current.timestamp(),
            created_at=(self.current - timedelta(hours=1)).timestamp(),
        )
        await self.bot._run_scheduled_once(self.ws, self.pending)
        self.assertIn("提醒一下", self.ws.sent[-1]["params"]["message"])

        self.current += timedelta(minutes=5)
        await self.bot._handle_group_message(
            self.ws, group_event("收到", user_id="20002"), self.pending
        )
        self.current += timedelta(hours=4)
        self.assertEqual(self.store.due_followups(self.current.timestamp()), [])

    async def test_unacknowledged_future_followup_mentions_source_user(self):
        event_at = at_time(18)
        self.store.add_future_event(
            group_id=GROUP_ID,
            source_user_id="20002",
            source_message_id="2",
            summary="提交材料",
            event_at=event_at.timestamp(),
            remind_at=at_time(10).timestamp(),
            created_at=at_time(9).timestamp(),
        )
        event = self.store.get_active_events(GROUP_ID, at_time(10).timestamp())[0]
        self.store.mark_reminded(event["id"], at_time(10).timestamp(), at_time(14).timestamp())
        self.current = at_time(14)
        await self.bot._run_scheduled_once(self.ws, self.pending)
        message = self.ws.sent[-1]["params"]["message"]
        self.assertEqual(message[0], {"type": "at", "data": {"qq": "20002"}})
        self.assertIn("提交材料", message[1]["data"]["text"])

    def test_sleeping_reminder_moves_to_previous_active_period(self):
        ideal = at_time(9)
        adjusted = self.bot._adjust_reminder_before(ideal)
        self.assertEqual(adjusted, datetime(2026, 9, 8, 18, 59, 59, tzinfo=TZ))


class EmptyAllowlistTest(unittest.TestCase):
    def test_empty_allowlist_is_supported(self):
        config = BotConfig(active_group_ids=frozenset(), timezone=TZ)
        self.assertEqual(config.active_group_ids, frozenset())


if __name__ == "__main__":
    unittest.main()
