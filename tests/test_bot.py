"""QQBot 行为测试：@ 判定、文本提取、DeepSeek 对话与群聊集成。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.bot import (
    DEFAULT_EMPTY_REPLY,
    DeepSeekClient,
    QQBot,
    extract_message_text,
    is_at_self,
)

SELF_ID = "10001"


def group_event(message, *, user_id="20002", group_id="30003", self_id=SELF_ID):
    raw = message if isinstance(message, str) else ""
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": self_id,
        "user_id": user_id,
        "group_id": group_id,
        "message": message,
        "raw_message": raw,
    }


class FakeWebSocket:
    def __init__(self, events, pending):
        self._events = list(events)
        self._index = 0
        self.sent = []
        self._pending = pending

    async def send(self, raw: str) -> None:
        action = json.loads(raw)
        self.sent.append(action)
        future = self._pending.get(action.get("echo"))
        if future is not None and not future.done():
            future.set_result({"status": "ok", "retcode": 0, "data": {}})

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._events):
            raise StopAsyncIteration
        item = self._events[self._index]
        self._index += 1
        return json.dumps(item, ensure_ascii=False)


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, history, user_text):
        self.calls.append((list(history), user_text))
        return f"AI:{user_text}"


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class MessageParsingTest(unittest.TestCase):
    def test_array_format_at_self(self):
        evt = group_event([
            {"type": "at", "data": {"qq": SELF_ID}},
            {"type": "text", "data": {"text": " 你好"}},
        ])
        self.assertTrue(is_at_self(evt, SELF_ID))
        self.assertEqual(extract_message_text(evt), "你好")

    def test_array_format_at_all_not_counted(self):
        evt = group_event([{"type": "at", "data": {"qq": "all"}}])
        self.assertFalse(is_at_self(evt, SELF_ID))

    def test_raw_cq_fallback_and_text_extraction(self):
        evt = group_event("[CQ:at,qq=10001] 1 &amp; 2")
        self.assertTrue(is_at_self(evt, SELF_ID))
        self.assertEqual(extract_message_text(evt), "1 & 2")

    def test_raw_cq_at_all_not_counted(self):
        evt = group_event("[CQ:at,qq=all] 大家好")
        self.assertFalse(is_at_self(evt, SELF_ID))


class DeepSeekClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_openai_compatible_request(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeHTTPResponse(
                {"choices": [{"message": {"content": "  模拟回复  "}}]}
            )

        client = DeepSeekClient(
            "test-key",
            model="test-model",
            system_prompt="系统提示",
            timeout_seconds=12,
        )
        with patch("src.bot.urlopen", fake_urlopen):
            answer = await client.chat(
                [{"role": "assistant", "content": "上一轮"}], "新问题"
            )

        self.assertEqual(answer, "模拟回复")
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(captured["body"]["model"], "test-model")
        self.assertEqual(
            [message["role"] for message in captured["body"]["messages"]],
            ["system", "assistant", "user"],
        )
        self.assertEqual(captured["timeout"], 12)

class IntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def run_events(self, events, *, llm=None):
        pending = {}
        ws = FakeWebSocket(events, pending)
        bot = QQBot("ws://127.0.0.1:3001", llm_client=llm or FakeLLM())
        await bot._read_loop(ws, pending)
        return bot, ws

    async def test_only_mentioned_text_is_sent_to_ai(self):
        events = [
            group_event([{"type": "text", "data": {"text": "普通消息"}}]),
            group_event([
                {"type": "at", "data": {"qq": SELF_ID}},
                {"type": "text", "data": {"text": " 在吗"}},
            ]),
        ]
        private = group_event([{"type": "at", "data": {"qq": SELF_ID}}])
        private["message_type"] = "private"
        events.extend([
            private,
            group_event([{"type": "at", "data": {"qq": SELF_ID}}], user_id=SELF_ID),
            group_event([{"type": "at", "data": {"qq": "all"}}]),
        ])

        llm = FakeLLM()
        _, ws = await self.run_events(events, llm=llm)
        replies = [item for item in ws.sent if item["action"] == "send_group_msg"]
        self.assertEqual([call[1] for call in llm.calls], ["在吗"])
        self.assertEqual([reply["params"]["message"] for reply in replies], ["AI:在吗"])

    async def test_keeps_history_per_group_and_user(self):
        llm = FakeLLM()
        events = [
            group_event("[CQ:at,qq=10001] 第一问"),
            group_event("[CQ:at,qq=10001] 第二问"),
            group_event("[CQ:at,qq=10001] 另一个人", user_id="99999"),
        ]
        await self.run_events(events, llm=llm)
        self.assertEqual(llm.calls[0], ([], "第一问"))
        self.assertEqual(llm.calls[1][0][-1], {"role": "assistant", "content": "AI:第一问"})
        self.assertEqual(llm.calls[2], ([], "另一个人"))

    async def test_empty_question_does_not_call_ai(self):
        llm = FakeLLM()
        _, ws = await self.run_events(
            [group_event([{"type": "at", "data": {"qq": SELF_ID}}])], llm=llm
        )
        self.assertEqual(llm.calls, [])
        self.assertEqual(ws.sent[0]["params"]["message"], DEFAULT_EMPTY_REPLY)


if __name__ == "__main__":
    unittest.main()
