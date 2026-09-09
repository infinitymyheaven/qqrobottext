"""消息解析、智能回答策略、同步、未来记忆与提醒测试。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from src.bot import (
    BotConfig,
    ConfigError,
    DeepSeekClient,
    DeepSeekWebSearchError,
    OneBotActionError,
    QQBot,
    cosine_similarity,
    extract_mentioned_ids,
    extract_message_text,
    is_at_self,
    stable_sigmoid,
    text_feature_vector,
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

    async def chat(self, history, user_text, *, context="", now=None):
        self.chat_calls.append((list(history), user_text, context, now))
        return f"AI:{user_text}"

    async def extract_future_events(self, text, now):
        self.extract_calls.append((text, now))
        return list(self.events)


class FailingWebLLM(FakeLLM):
    async def chat(self, history, user_text, *, context="", now=None):
        raise DeepSeekWebSearchError("模拟联网失败")


class FixedRNG:
    def __init__(self, value=0.0, uniform_value=180.0, randint_value=None):
        self.value = value
        self.uniform_value = uniform_value
        self.randint_value = randint_value

    def random(self):
        return self.value

    def uniform(self, _start, _end):
        return self.uniform_value

    def randint(self, start, end):
        return start if self.randint_value is None else min(end, max(start, self.randint_value))

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

    def test_text_vectors_are_stable_and_comparable(self):
        first = text_feature_vector("机械键盘真好用")
        second = text_feature_vector("机械键盘很好用")
        unrelated = text_feature_vector("明天天气如何")
        self.assertEqual(first, text_feature_vector("机械键盘真好用"))
        self.assertGreater(cosine_similarity(first, second), cosine_similarity(first, unrelated))
        self.assertAlmostEqual(stable_sigmoid(0), 0.5)


class DeepSeekClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_includes_local_context(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeHTTPResponse({"choices": [{"message": {"content": "回复"}}]})

        client = DeepSeekClient(
            "test-key", model="test-model", timeout_seconds=12, web_search_enabled=False
        )
        with patch("src.bot.urlopen", fake_urlopen):
            answer = await client.chat([], "谁是群主", context="张三是群主")
        self.assertEqual(answer, "回复")
        self.assertIn("张三是群主", captured["body"]["messages"][1]["content"])
        self.assertEqual(captured["timeout"], 12)

    async def test_web_chat_uses_responses_auto_and_sanitizes_context(self):
        captured = {}
        response = {
            "status": "completed",
            "output": [
                {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "内部推理"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "普通回答"}]},
            ],
        }

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeHTTPResponse(response)

        client = DeepSeekClient("test-key", web_search_timeout_seconds=91)
        with patch("src.bot.urlopen", fake_urlopen):
            answer = await client.chat(
                [],
                "介绍一下这个话题，别泄露 QQ 987654321",
                context="群主（QQ 123456789，群主）",
                now=at_time(12, 30),
            )
        self.assertEqual(answer, "普通回答")
        self.assertTrue(captured["url"].endswith("/responses"))
        self.assertEqual(captured["body"]["tools"], [{"type": "web_search"}])
        self.assertEqual(captured["body"]["tool_choice"], "auto")
        self.assertNotIn("123456789", json.dumps(captured["body"], ensure_ascii=False))
        self.assertNotIn("987654321", json.dumps(captured["body"], ensure_ascii=False))
        self.assertEqual(captured["timeout"], 91)

    async def test_current_time_uses_local_clock_without_web_request(self):
        client = DeepSeekClient("test-key")
        # 如果实现意外访问网络，side_effect 会让测试立即失败。
        with patch("src.bot.urlopen", side_effect=AssertionError("不应请求网络")):
            answer = await client.chat([], "现在几点了？", now=at_time(12, 30))
        self.assertEqual(answer, "现在是 2026年09月09日 12:30（Asia/Shanghai）。")

    async def test_foreign_location_time_is_not_mistaken_for_local_time(self):
        captured = {}
        response = {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "外地时间"}]}
            ],
        }

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse(response)

        client = DeepSeekClient("test-key")
        with patch("src.bot.urlopen", fake_urlopen):
            answer = await client.chat([], "纽约现在几点？", now=at_time(12, 30))
        self.assertEqual(answer, "外地时间")
        self.assertEqual(captured["body"]["tool_choice"], "auto")

    async def test_incomplete_web_response_is_an_error(self):
        client = DeepSeekClient("test-key")
        response = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
        }
        with patch("src.bot.urlopen", return_value=FakeHTTPResponse(response)):
            with self.assertRaisesRegex(Exception, "Responses 未完成"):
                await client.chat([], "查询天气")

    async def test_explicit_search_words_force_web(self):
        response = {
            "status": "completed",
            "output": [
                {"type": "web_search_call", "action": {"type": "search"}},
                {"type": "message", "content": [{"type": "output_text", "text": "已查询"}]},
            ],
        }
        captured = {}

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse(response)

        client = DeepSeekClient("test-key")
        with patch("src.bot.urlopen", fake_urlopen):
            await client.chat([], "请联网搜索今天的天气")
        self.assertEqual(captured["body"]["tool_choice"], {"type": "web_search"})

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

    async def test_at_uses_probability_inside_window_and_is_silent_outside(self):
        await self.bot._handle_group_message(self.ws, mention_event("在吗"), self.pending)
        self.assertEqual(self.llm.chat_calls[0][1], "在吗")
        self.assertEqual(self.ws.sent[-1]["params"]["message"], "AI:在吗")
        self.current = at_time(19)
        await self.bot._handle_group_message(self.ws, mention_event("还在吗"), self.pending)
        self.assertEqual(len(self.llm.chat_calls), 1)

        self.current = at_time(11)
        self.bot.rng = FixedRNG(value=1.0)
        await self.bot._handle_group_message(self.ws, mention_event("概率拒绝"), self.pending)
        self.assertEqual(len(self.llm.chat_calls), 1)

    async def test_at_probability_bypasses_spontaneous_limits(self):
        self.store.ensure_daily_spontaneous_limit(
            GROUP_ID, self.current.date().isoformat(), 1, 1, 1
        )
        self.store.record_bot_message(
            GROUP_ID,
            self.current.date().isoformat(),
            self.current.timestamp(),
            spontaneous=True,
        )
        await self.bot._handle_group_message(
            self.ws, mention_event("仍然参与概率判断"), self.pending
        )
        self.assertEqual(self.llm.chat_calls[-1][1], "仍然参与概率判断")

    async def test_backend_log_contains_weights_contributions_and_decision(self):
        with self.assertLogs("qqrobot", level="INFO") as captured:
            await self.bot._handle_group_message(
                self.ws, mention_event("不要把这段正文写入日志"), self.pending
            )
        decision_line = next(
            line for line in captured.output if "发言判定 " in line
        )
        payload = json.loads(decision_line.split("发言判定 ", 1)[1])
        self.assertEqual(payload["event"], "speech_decision")
        self.assertTrue(payload["will_speak"])
        self.assertIn("is_mentioned", payload["weights"])
        self.assertIn("is_mentioned", payload["features"])
        self.assertIn("mentioned_topic", payload["contributions"])
        self.assertNotIn("不要把这段正文写入日志", decision_line)

    async def test_backend_log_explains_off_hours_without_features(self):
        self.current = at_time(9)
        with self.assertLogs("qqrobot", level="INFO") as captured:
            await self.bot._handle_group_message(
                self.ws, mention_event("休息时段"), self.pending
            )
        payload = json.loads(captured.output[0].split("发言判定 ", 1)[1])
        self.assertFalse(payload["will_speak"])
        self.assertEqual(payload["reason"], "工作时段外")
        self.assertIsNone(payload["features"])
        self.assertTrue(payload["weights"])

    def test_default_probability_calibration(self):
        # 冷启动的典型 @ 应接近九成；FixedRNG 只固定抽样，不改变返回的 probability。
        mentioned = self.bot._decide_speech(
            GROUP_ID, "20002", "在吗？", True, self.current
        )
        self.assertGreaterEqual(mentioned.probability, 0.85)

        # 先学习并记住同一话题，再验证普通高相关消息仍位于约 10%–30% 区间。
        text = "机械键盘真好用？"
        event = group_event([{"type": "text", "data": {"text": text}}])
        self.bot._observe_speech_message(GROUP_ID, "20002", text, self.current)
        self.bot._remember_recent(GROUP_ID, "20002", text, event, self.current)
        related = self.bot._decide_speech(
            GROUP_ID, "20002", text, False, self.current + timedelta(seconds=1)
        )
        self.assertGreaterEqual(related.probability, 0.10)
        self.assertLessEqual(related.probability, 0.30)

    async def test_off_hours_message_learns_profile_without_reply(self):
        self.current = at_time(9)
        event = group_event([{"type": "text", "data": {"text": "我喜欢机械键盘"}}])
        await self.bot._handle_group_message(self.ws, event, self.pending)
        member, _ = self.store.get_speech_profiles(
            GROUP_ID,
            "20002",
            self.current.timestamp(),
            activity_half_life_seconds=86400,
            bond_half_life_seconds=30 * 86400,
        )
        self.assertEqual(self.llm.chat_calls, [])
        self.assertEqual(member["message_count"], 1)
        self.assertTrue(member["features"])

    async def test_web_search_failure_uses_uncertainty_reply(self):
        self.bot.llm_client = FailingWebLLM()
        with self.assertLogs("qqrobot", level="ERROR"):
            await self.bot._handle_group_message(
                self.ws, mention_event("帮我联网查询天气"), self.pending
            )
        self.assertEqual(
            self.ws.sent[-1]["params"]["message"],
            self.config.web_search_failure_reply,
        )
        member, bot = self.store.get_speech_profiles(
            GROUP_ID,
            "20002",
            self.current.timestamp(),
            activity_half_life_seconds=86400,
            bond_half_life_seconds=30 * 86400,
        )
        self.assertEqual(member["reply_count"], 0)
        self.assertEqual(bot["reply_count"], 0)

    def test_minute_answer_time_boundaries(self):
        self.bot.config = BotConfig(
            active_group_ids=frozenset({GROUP_ID}),
            timezone=TZ,
            answer_start_minutes=630,
            answer_end_minutes=1155,
        )
        self.assertFalse(self.bot.is_answer_time(at_time(10, 29)))
        self.assertTrue(self.bot.is_answer_time(at_time(10, 30)))
        self.assertTrue(self.bot.is_answer_time(at_time(19, 14)))
        self.assertFalse(self.bot.is_answer_time(at_time(19, 15)))

    async def test_spontaneous_score_interval_and_daily_limit(self):
        event = group_event([{"type": "text", "data": {"text": "普通话题"}}])
        await self.bot._handle_group_message(self.ws, event, self.pending)
        self.assertEqual(len(self.llm.chat_calls), 1)

        self.current += timedelta(seconds=100)
        await self.bot._handle_group_message(self.ws, event, self.pending)
        self.assertEqual(len(self.llm.chat_calls), 1)

        limited_config = BotConfig(
            active_group_ids=frozenset({GROUP_ID}),
            timezone=TZ,
            spontaneous_daily_min=1,
            spontaneous_daily_max=1,
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

    async def test_custom_greeting_and_switch(self):
        self.bot.config = BotConfig(
            active_group_ids=frozenset({GROUP_ID}),
            timezone=TZ,
            morning_messages=("自定义早安",),
            night_greeting_enabled=False,
        )
        await self.bot._run_scheduled_once(self.ws, self.pending)
        self.assertEqual(self.ws.sent[-1]["params"]["message"], "自定义早安")
        self.current = at_time(19, 1)
        await self.bot._run_scheduled_once(self.ws, self.pending)
        self.assertEqual(len(self.ws.sent), 1)

    async def test_custom_empty_and_error_replies(self):
        self.bot.config = BotConfig(
            active_group_ids=frozenset({GROUP_ID}),
            timezone=TZ,
            empty_reply="请说内容",
            error_reply="服务开小差了",
        )
        empty_mention = group_event([{"type": "at", "data": {"qq": SELF_ID}}])
        await self.bot._handle_group_message(self.ws, empty_mention, self.pending)
        self.assertEqual(self.ws.sent[-1]["params"]["message"], "请说内容")
        self.bot.llm_client = None
        await self.bot._handle_group_message(self.ws, mention_event("问题"), self.pending)
        self.assertEqual(self.ws.sent[-1]["params"]["message"], "服务开小差了")

    async def test_history_expires_after_idle_ttl(self):
        await self.bot._handle_group_message(self.ws, mention_event("第一问"), self.pending)
        self.current += timedelta(minutes=31)
        await self.bot._handle_group_message(self.ws, mention_event("第二问"), self.pending)
        self.assertEqual(self.llm.chat_calls[1][0], [])

    async def test_group_context_uses_time_count_and_character_limits(self):
        self.bot.config = BotConfig(
            active_group_ids=frozenset({GROUP_ID}),
            timezone=TZ,
            group_context_window_seconds=300,
            group_context_max_messages=2,
            group_context_message_max_chars=5,
        )
        for index, text in enumerate(("过期消息", "第一条很长", "第二条很长")):
            when = self.current - timedelta(seconds=301 if index == 0 else 10 - index)
            self.bot._remember_recent(
                GROUP_ID, str(index), text, group_event(text, user_id=str(index)), when
            )
        context = self.bot._build_group_context(GROUP_ID, "20002", [], "问题", self.current)
        self.assertNotIn("过期消息", context)
        self.assertIn("第一条很", context)
        self.assertIn("第二条很", context)

    async def test_traffic_score_uses_independent_window(self):
        self.bot.config = BotConfig(
            active_group_ids=frozenset({GROUP_ID}),
            timezone=TZ,
            group_context_window_seconds=300,
            spontaneous_traffic_window_seconds=60,
        )
        self.bot.rng = FixedRNG(value=0.0)
        for index in range(10):
            self.bot._remember_recent(
                GROUP_ID,
                str(index),
                "旧消息",
                group_event("旧消息", user_id=str(index)),
                self.current - timedelta(seconds=120),
            )
        decision = self.bot._decide_speech(
            GROUP_ID, "20002", "当前消息", False, self.current
        )
        self.assertAlmostEqual(decision.features.group_activity, 0.1)

    async def test_active_window_all_extracts_even_without_reply(self):
        self.bot.rng = FixedRNG(value=1.0)
        event = group_event([{"type": "text", "data": {"text": "明天十点开会"}}])
        await self.bot._handle_group_message(self.ws, event, self.pending)
        self.assertEqual(len(self.llm.chat_calls), 0)
        self.assertEqual(len(self.llm.extract_calls), 1)

    async def test_future_memory_switch_disables_extraction_and_reminders(self):
        self.bot.config = BotConfig(
            active_group_ids=frozenset({GROUP_ID}),
            timezone=TZ,
            future_memory_enabled=False,
            morning_greeting_enabled=False,
        )
        event = group_event([{"type": "text", "data": {"text": "明天十点开会"}}])
        await self.bot._handle_group_message(self.ws, event, self.pending)
        self.assertEqual(self.llm.extract_calls, [])
        self.store.add_future_event(
            group_id=GROUP_ID,
            source_user_id="20002",
            source_message_id="off",
            summary="不会提醒",
            event_at=(self.current + timedelta(hours=1)).timestamp(),
            remind_at=self.current.timestamp(),
            created_at=self.current.timestamp(),
        )
        sent_before = len(self.ws.sent)
        await self.bot._run_scheduled_once(self.ws, self.pending)
        self.assertEqual(len(self.ws.sent), sent_before)

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
            rng=FixedRNG(value=1.0),
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
        saved = self.store.get_active_events(GROUP_ID, self.current.timestamp())[0]
        self.assertEqual(
            saved["followup_at"],
            (self.current + timedelta(minutes=180)).timestamp(),
        )

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


class ConfigParsingTest(unittest.TestCase):
    def test_minute_times_ranges_switches_and_templates(self):
        values = {
            "ANSWER_START_TIME": "10:30",
            "ANSWER_END_TIME": "19:15",
            "SPONTANEOUS_DAILY_MIN": "60",
            "SPONTANEOUS_DAILY_MAX": "100",
            "SPONTANEOUS_REPLIES_ENABLED": "off",
            "SPEAK_WEIGHT_IS_MENTIONED": "0.9",
            "SPEAK_SIGMOID_K": "4.5",
            "SPEAK_BOND_LEARNING_RATE": "0.2",
            "MORNING_GREETING_MESSAGES": "早安一||早安二",
            "ERROR_LOG_ENABLED": "yes",
            "ERROR_LOG_PATH": "runtime/custom-errors.txt",
            "ERROR_LOG_BEFORE_RECORDS": "12",
            "ERROR_LOG_AFTER_RECORDS": "4",
            "ERROR_LOG_MAX_BYTES": "4096",
            "ERROR_LOG_BACKUP_COUNT": "1",
        }
        with patch.dict(os.environ, values, clear=True):
            config = BotConfig.from_env()
        self.assertEqual((config.answer_start_minutes, config.answer_end_minutes), (630, 1155))
        self.assertFalse(config.spontaneous_replies_enabled)
        self.assertEqual(config.speak_weight_is_mentioned, 0.9)
        self.assertEqual(config.speak_sigmoid_k, 4.5)
        self.assertEqual(config.speak_bond_learning_rate, 0.2)
        self.assertEqual(config.morning_messages, ("早安一", "早安二"))
        self.assertTrue(config.web_search_enabled)
        self.assertEqual(config.web_search_timeout_seconds, 90.0)
        self.assertTrue(config.error_log_enabled)
        self.assertEqual(config.error_log_path, "runtime/custom-errors.txt")
        self.assertEqual(config.error_log_before_records, 12)
        self.assertEqual(config.error_log_after_records, 4)
        self.assertEqual(config.error_log_max_bytes, 4096)
        self.assertEqual(config.error_log_backup_count, 1)

    def test_legacy_hour_variables_are_ignored(self):
        with patch.dict(
            os.environ,
            {"ANSWER_START_HOUR": "1", "ANSWER_END_HOUR": "2", "SPONTANEOUS_DAILY_LIMIT": "3"},
            clear=True,
        ):
            config = BotConfig.from_env()
        self.assertEqual((config.answer_start_minutes, config.answer_end_minutes), (600, 1140))
        self.assertEqual((config.spontaneous_daily_min, config.spontaneous_daily_max), (60, 100))

    def test_all_day_time_range(self):
        with patch.dict(
            os.environ,
            {"ANSWER_START_TIME": "00:00", "ANSWER_END_TIME": "24:00"},
            clear=True,
        ):
            config = BotConfig.from_env()
        self.assertEqual((config.answer_start_minutes, config.answer_end_minutes), (0, 1440))

    def test_invalid_configuration_is_rejected(self):
        invalid_values = (
            {"ANSWER_START_TIME": "9:00"},
            {"ANSWER_END_TIME": "24:01"},
            {"ANSWER_START_TIME": "20:00", "ANSWER_END_TIME": "19:00"},
            {"SPONTANEOUS_DAILY_MIN": "101", "SPONTANEOUS_DAILY_MAX": "100"},
            {"SPEAK_WEIGHT_RANDOM_NOISE": "-0.5"},
            {"SPEAK_SIGMOID_K": "0"},
            {"SPEAK_INTEREST_LEARNING_RATE": "1.1"},
            {"FUTURE_MEMORY_ENABLED": "perhaps"},
            {"WEB_SEARCH_ENABLED": "perhaps"},
            {"WEB_SEARCH_TIMEOUT_SECONDS": "0"},
            {"WEB_SEARCH_MAX_LOG_SOURCES": "-1"},
            {"ERROR_LOG_ENABLED": "perhaps"},
            {"ERROR_LOG_PATH": "   "},
            {"ERROR_LOG_BEFORE_RECORDS": "-1"},
            {"ERROR_LOG_MAX_BYTES": "1023"},
        )
        for values in invalid_values:
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(ConfigError):
                    BotConfig.from_env()


if __name__ == "__main__":
    unittest.main()
