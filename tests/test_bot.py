"""QQBot 行为测试：@ 判定与“仅 @ 本人时回复一次”的集成行为。"""

from __future__ import annotations

import asyncio
import json
import unittest

from src.bot import QQBot, is_at_self

SELF_ID = "10001"


def group_event(message, *, user_id="20002", group_id="30003", self_id=SELF_ID):
    raw = message if isinstance(message, str) else ""
    return {
        "post_type": "message",
        "message_type": "group",
        "time": 0,
        "self_id": self_id,
        "user_id": user_id,
        "group_id": group_id,
        "message_id": "40004",
        "message": message,
        "raw_message": raw,
        "sender": {"user_id": user_id},
    }


class FakeWebSocket:
    """模拟 OneBot 服务端：推送事件，并对动作请求同步回填 echo 响应。"""

    def __init__(self, events, pending):
        self._events = list(events)
        self._index = 0
        self.sent = []
        self._pending = pending

    async def send(self, raw: str) -> None:
        action = json.loads(raw)
        self.sent.append(action)
        # 模拟服务端立即返回动作响应，让发送方不必等待真实网络。
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


class AtDetectionTest(unittest.TestCase):
    def test_array_format_at_self(self):
        evt = group_event(
            [
                {"type": "at", "data": {"qq": SELF_ID}},
                {"type": "text", "data": {"text": "你好"}},
            ]
        )
        self.assertTrue(is_at_self(evt, SELF_ID))

    def test_array_format_at_all_not_counted(self):
        evt = group_event([{"type": "at", "data": {"qq": "all"}}])
        self.assertFalse(is_at_self(evt, SELF_ID))

    def test_raw_cq_fallback(self):
        evt = group_event("[CQ:at,qq=10001] 你好")
        self.assertTrue(is_at_self(evt, SELF_ID))

    def test_raw_cq_at_all_not_counted(self):
        evt = group_event("[CQ:at,qq=all] 大家好")
        self.assertFalse(is_at_self(evt, SELF_ID))


class IntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_reply_when_mentioned(self):
        events = [
            group_event([{"type": "text", "data": {"text": "普通消息"}}]),
            group_event(
                [
                    {"type": "at", "data": {"qq": SELF_ID}},
                    {"type": "text", "data": {"text": "在吗"}},
                ]
            ),
        ]
        # 私聊消息（应忽略）
        private = group_event([{"type": "at", "data": {"qq": SELF_ID}}])
        private["message_type"] = "private"
        events.append(private)
        # 机器人自己的消息（应忽略）
        events.append(group_event([{"type": "at", "data": {"qq": SELF_ID}}], user_id=SELF_ID))
        # @全体成员（应忽略）
        events.append(group_event([{"type": "at", "data": {"qq": "all"}}]))
        # 字符串消息格式兜底（应回复）
        events.append(group_event("[CQ:at,qq=10001] 字符串消息"))
        # 字符串格式 @all（应忽略）
        events.append(group_event("[CQ:at,qq=all] 大家好"))

        pending = {}
        ws = FakeWebSocket(events, pending)
        bot = QQBot("ws://127.0.0.1:3001", "")
        await bot._read_loop(ws, pending)
        # 等待后台事件任务结束。
        for _ in range(20):
            await asyncio.sleep(0.02)

        replies = [s for s in ws.sent if s["action"] == "send_group_msg"]
        self.assertEqual(len(replies), 2)
        for reply in replies:
            self.assertEqual(reply["params"]["message"], "对不起做不到。")
            self.assertEqual(reply["params"]["group_id"], "30003")
            self.assertIsInstance(reply["echo"], str)


if __name__ == "__main__":
    unittest.main()
