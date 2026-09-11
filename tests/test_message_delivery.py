"""第三内容指标的分句、额度收敛和打字节奏测试。"""

from __future__ import annotations

import unittest

from src.message_delivery import DeliveryPolicy, plan_reply


class FixedDeliveryRNG:
    """返回固定抖动系数，保证延迟断言不受随机数影响。"""

    def __init__(self, value: float = 1.0):
        self.value = value

    def uniform(self, _start: float, _end: float) -> float:
        return self.value


class MessageDeliveryTest(unittest.TestCase):
    def test_short_reply_splits_on_weak_and_strong_punctuation(self):
        plan = plan_reply("你好，今天还行。你呢？", rng=FixedDeliveryRNG())
        self.assertEqual(plan.segments, ("你好", "今天还行", "你呢？"))
        self.assertEqual(plan.delays[0], 0.0)
        self.assertTrue(plan.split)

    def test_reply_over_sixty_chars_stays_as_one_message(self):
        text = "长" * 61 + "。后一句"
        plan = plan_reply(text, rng=FixedDeliveryRNG())
        self.assertEqual(plan.segments, (text,))
        self.assertEqual(plan.delays, (0.0,))
        self.assertFalse(plan.split)

    def test_six_segment_limit_merges_remaining_sentences(self):
        plan = plan_reply("一，二，三，四，五，六，七。", rng=FixedDeliveryRNG())
        self.assertEqual(plan.segments, ("一", "二", "三", "四", "五", "六，七"))

    def test_available_quota_merges_tail_without_losing_words(self):
        plan = plan_reply(
            "一，二，三，四。", available_messages=2, rng=FixedDeliveryRNG()
        )
        self.assertEqual(plan.segments, ("一", "二，三，四"))
        self.assertEqual(plan_reply("一，二。", 1).segments, ("一，二。",))
        self.assertEqual(plan_reply("内容", 0).segments, ())

    def test_numbers_urls_domains_and_consecutive_marks_are_preserved(self):
        text = "价格1,000，查https://example.com/a,b，再看example.org，好吗？！"
        plan = plan_reply(text, rng=FixedDeliveryRNG())
        self.assertEqual(
            plan.segments,
            ("价格1,000", "查https://example.com/a,b", "再看example.org", "好吗？！"),
        )

    def test_delay_uses_next_segment_length_and_is_bounded(self):
        short = plan_reply("好，收到。", rng=FixedDeliveryRNG())
        long = plan_reply("好，" + "长" * 50 + "。", rng=FixedDeliveryRNG())
        self.assertGreater(long.delays[1], short.delays[1])
        self.assertEqual(short.delays[0], 0.0)
        self.assertEqual(long.delays[1], 6.0)

        disabled = plan_reply(
            "好，收到。",
            policy=DeliveryPolicy(typing_speed=0),
            rng=FixedDeliveryRNG(),
        )
        self.assertEqual(disabled.delays, (0.0, 0.0))

    def test_empty_fragments_are_ignored_and_single_sentence_is_unchanged(self):
        plan = plan_reply("你好，，，真的？！\n\n行。", rng=FixedDeliveryRNG())
        self.assertEqual(plan.segments, ("你好", "真的？！", "行"))
        self.assertEqual(plan_reply("只有一句。", rng=FixedDeliveryRNG()).segments, ("只有一句。",))

    def test_direct_policy_construction_rejects_non_finite_speed(self):
        with self.assertRaises(ValueError):
            DeliveryPolicy(typing_speed=float("nan"))


if __name__ == "__main__":
    unittest.main()
