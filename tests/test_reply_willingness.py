"""独立消息流回复意愿模块的边界、概率、日志和持久化测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.memory import MemoryStore
from src.reply_willingness import (
    ReplyWillingnessEngine,
    StreamMessage,
    WillingnessConfig,
)


class SequenceRNG:
    """按顺序返回固定随机量，使概率测试可以完全复现。"""

    def __init__(self, *values: float):
        self.values = list(values) or [0.0]

    def random(self) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class ReplyWillingnessTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "memory.sqlite3")
        self.config = WillingnessConfig(personal_background="Python 人工智能 游戏")
        self.engine = ReplyWillingnessEngine(
            self.config, self.store, rng=SequenceRNG(0.2, 0.0)
        )
        self.now = 2_000_000_000.0

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def message(self, index: int, age: float = 0, *, user: str | None = None):
        return StreamMessage(
            group_id="g",
            user_id=user or str(index % 7),
            display_name="匿名成员",
            sent_at=self.now - age,
            text=f"Python 游戏话题 {index}",
            message_id=str(index),
        )

    def test_stream_is_sorted_and_pruned_by_age_and_capacity(self):
        small = ReplyWillingnessEngine(
            WillingnessConfig(message_limit=3, message_max_age_seconds=100),
            self.store,
            rng=SequenceRNG(),
        )
        for item in (self.message(1, 20), self.message(2, 10), self.message(3, 200), self.message(4, 5)):
            small.record_message(item)
        messages = small.messages("g", self.now)
        self.assertEqual([item.message_id for item in messages], ["1", "2", "4"])
        self.assertEqual([item.sent_at for item in messages], sorted(item.sent_at for item in messages))

    def test_snapshot_refreshes_at_five_seconds_and_short_density_dominates(self):
        for index in range(10):
            self.engine.record_message(self.message(index, 700))
        old = self.engine.refresh_group("g", self.now)
        self.engine.record_message(self.message(99, 0, user="new"))
        cached = self.engine.refresh_group("g", self.now + 4)
        refreshed = self.engine.refresh_group("g", self.now + 5)
        self.assertEqual(cached.buffer_count, old.buffer_count)
        self.assertEqual(refreshed.buffer_count, old.buffer_count + 1)
        self.assertGreater(refreshed.group_activity, old.group_activity)

    def test_decision_has_exactly_eight_bounded_features_and_safe_full_log(self):
        secret_text = "绝不能进入日志的正文 APIKEY-SECRET"
        self.engine.record_message(
            StreamMessage("g", "u", "用户", self.now, secret_text, mentioned_bot=True)
        )
        with self.assertLogs("qqrobot", level="INFO") as captured:
            decision = self.engine.decide(
                "g", "u", secret_text, mentioned=True, within_work_hours=True,
                local_date="2033-05-18", now=self.now,
            )
        line = next(item for item in captured.output if "意愿计算 " in item)
        payload = json.loads(line.split("意愿计算 ", 1)[1])
        self.assertEqual(len(payload["features"]), 8)
        self.assertEqual(set(payload["features"]), set(payload["weights"]))
        self.assertEqual(set(payload["features"]), set(payload["contributions"]))
        self.assertTrue(all(0 <= value <= 1 for value in payload["features"].values()))
        self.assertTrue(decision.accepted)
        self.assertNotIn(secret_text, line)
        self.assertNotIn(self.config.personal_background, line)

    def test_off_hours_still_calculates_but_never_replies(self):
        self.engine.record_message(self.message(1))
        decision = self.engine.decide(
            "g", "u", "内容", mentioned=True, within_work_hours=False,
            local_date="2033-05-18", now=self.now,
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "工作时段外")
        self.assertIsNotNone(decision.features)

    def test_daily_limit_includes_mentions_and_resets_by_date(self):
        limited = ReplyWillingnessEngine(
            WillingnessConfig(daily_reply_limit=1), self.store, rng=SequenceRNG(0, 0)
        )
        limited.record_reply("g", "u", "2033-05-18", self.now, text="回复")
        blocked = limited.decide(
            "g", "u", "@问题", mentioned=True, within_work_hours=True,
            local_date="2033-05-18", now=self.now + 1,
        )
        next_day = limited.decide(
            "g", "u", "@问题", mentioned=True, within_work_hours=True,
            local_date="2033-05-19", now=self.now + 86_400,
        )
        self.assertEqual(blocked.reason, "达到每群每日回复上限")
        self.assertTrue(next_day.accepted)

    def test_reply_cooldown_recovers_with_new_messages(self):
        self.engine.record_message(self.message(1))
        before = self.engine.refresh_group("g", self.now, force=True)
        self.engine.record_reply("g", "u", "2033-05-18", self.now, text="机器人回复")
        cooled = self.engine.refresh_group("g", self.now + 1, force=True)
        for index in range(4):
            self.engine.record_message(self.message(10 + index, -2 - index))
        recovered = self.engine.refresh_group("g", self.now + 6, force=True)
        self.assertLess(cooled.cooldown_factor, before.cooldown_factor)
        self.assertEqual(recovered.cooldown_factor, 1.0)

    def test_relationship_growth_grace_and_ebbinghaus_zero(self):
        inbound = self.engine.record_inbound_interaction("g", "u", self.now)
        self.engine.record_reply("g", "u", "2033-05-18", self.now, text="回复")
        outbound = self.store.get_willingness_bond(
            "g", "u", self.now, grace_seconds=86_400, zero_seconds=30 * 86_400
        )
        grace = self.store.get_willingness_bond(
            "g", "u", self.now + 86_400, grace_seconds=86_400, zero_seconds=30 * 86_400
        )
        forgotten = self.store.get_willingness_bond(
            "g", "u", self.now + 30 * 86_400, grace_seconds=86_400, zero_seconds=30 * 86_400
        )
        self.assertAlmostEqual(inbound, 0.12)
        self.assertGreater(outbound, inbound)
        self.assertEqual(grace, outbound)
        self.assertEqual(forgotten, 0.0)

    def test_topic_heat_levels_and_analysis_interval(self):
        self.store.save_willingness_topics(
            "g",
            [
                {"name": "核心", "summary": "Python", "message_count": 10, "participant_count": 4},
                {"name": "次要", "summary": "游戏", "message_count": 5, "participant_count": 2},
                {"name": "边缘", "summary": "音乐", "message_count": 2, "participant_count": 1},
            ],
            {},
            self.now,
        )
        levels = {item["name"]: item["heat_level"] for item in self.store.list_willingness_topics("g")}
        self.assertEqual(levels, {"核心": "core", "次要": "secondary", "边缘": "peripheral"})
        for index in range(30):
            self.engine.record_message(self.message(index, index, user=str(index)))
        interval = self.engine.analysis_interval_hours("g", self.now)
        self.assertGreaterEqual(interval, 3)
        self.assertLessEqual(interval, 10)

    async def test_two_stage_topic_analysis_anonymizes_before_public_search(self):
        calls = {}

        class Analyzer:
            async def analyze_willingness_topics(_self, messages, now):
                calls["messages"] = messages
                return [{
                    "name": "Python",
                    "summary": "编程讨论",
                    "message_count": 3,
                    "participant_count": 2,
                    "emotion_intensity": 0.8,
                    "public_query": "Python 最新版本",
                }]

            async def enrich_willingness_topics(_self, queries, now):
                calls["queries"] = queries
                return {"Python 最新版本": "公开知识"}

            async def analyze_persona(_self, messages, metadata, seed, now):
                raise AssertionError("未配置目标账号时不应分析个人背景")

        self.engine.record_message(
            StreamMessage("g", "123456789", "真实姓名", self.now, "Python 很有趣")
        )
        await self.engine.analyze_due_groups(Analyzer(), self.now)
        serialized = json.dumps(calls["messages"], ensure_ascii=False)
        self.assertIn("成员1", serialized)
        self.assertNotIn("真实姓名", serialized)
        self.assertNotIn("123456789", serialized)
        self.assertEqual(calls["queries"], ["Python 最新版本"])
        topic = self.store.list_willingness_topics("g")[0]
        self.assertEqual(topic["public_knowledge"], "公开知识")

    async def test_failed_topic_analysis_keeps_knowledge_and_waits_three_hours(self):
        self.store.save_willingness_topics(
            "g",
            [{"name": "旧知识", "summary": "保留", "message_count": 1}],
            {},
            self.now - 100,
        )
        self.engine.record_message(self.message(1))

        class FailingAnalyzer:
            async def analyze_willingness_topics(_self, messages, now):
                raise RuntimeError("模拟失败")

        with self.assertLogs("qqrobot", level="ERROR"):
            await self.engine.analyze_due_groups(FailingAnalyzer(), self.now)
        state = self.store.get_willingness_analysis_state("g")
        self.assertGreaterEqual(
            state["next_analysis_at"],
            self.now + self.config.topic_analysis_min_hours * 3600,
        )
        self.assertEqual(self.store.list_willingness_topics("g")[0]["name"], "旧知识")


if __name__ == "__main__":
    unittest.main()
