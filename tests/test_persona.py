"""人格内容因子、隐私清洗、版本存储和采集器测试。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.memory import MemoryStore
from src.bot import OneBotActionError
from src.persona import (
    PersonaContentEngine,
    PersonaSample,
    merge_persona_profiles,
    normalize_persona_analysis,
    redact_persona_text,
    render_content_factors,
)
from src.persona_collector import (
    Conversation,
    _evaluation_passes,
    _evaluation_samples,
    build_persona_samples,
    collect_conversation,
    parse_selection,
)


def profile(
    *,
    scene: str = "group",
    score: float = 0.8,
    evidence: int = 10,
    summary: str = "轻松直接",
) -> dict:
    return {
        "summary": summary,
        "interests": ["游戏", "编程"],
        "dimensions": [
            {
                "name": "directness",
                "scene": scene,
                "score": score,
                "description": f"{scene}表达",
                "confidence": 1.0,
                "evidence_count": evidence,
            }
        ],
        "phrases": [
            {"text": "确实", "scene": scene, "frequency": 3, "confidence": 0.9},
            {"text": "只出现一次", "scene": scene, "frequency": 1, "confidence": 1.0},
        ],
        "exemplars": [
            {"situation": "有人分享趣事", "response": "这确实有点意思", "scene": scene}
        ],
        "source_started_at": 10,
        "source_ended_at": 20,
        "source_message_count": evidence,
        "group_message_count": evidence if scene == "group" else 0,
        "private_message_count": evidence if scene == "private" else 0,
    }


class PersonaPrivacyTest(unittest.TestCase):
    def test_redaction_removes_direct_identifiers_and_aliases(self):
        cleaned = redact_persona_text(
            "小王联系 QQ:123456789，手机13800138000，a@example.com，https://example.com/x",
            {"小王": "成员1"},
        )
        self.assertIn("成员1", cleaned)
        self.assertNotIn("123456789", cleaned)
        self.assertNotIn("13800138000", cleaned)
        self.assertNotIn("a@example.com", cleaned)
        self.assertNotIn("example.com", cleaned)

    def test_normalization_bounds_values_and_drops_rare_phrases(self):
        normalized = normalize_persona_analysis(profile(score=4.0))
        self.assertEqual(normalized["dimensions"][0]["score"], 1.0)
        self.assertEqual([item["text"] for item in normalized["phrases"]], ["确实"])

    def test_normalization_controls_enums_and_derives_non_text_metadata(self):
        value = profile()
        value["exemplars"] = [
            {
                "situation": "考试没发挥好",
                "response": "没事，下次再来？",
                "scene": "group",
                "intent": "invalid",
                "emotion": "invalid",
                "keywords": [*(f"词{index}" for index in range(10))],
                "confidence": 2,
                "evidence_count": 3,
                "quality_score": -1,
                "unknown": "不能进入结构",
            }
        ]
        exemplar = normalize_persona_analysis(value)["exemplars"][0]
        self.assertEqual(exemplar["intent"], "other")
        self.assertEqual(exemplar["emotion"], "neutral")
        self.assertEqual(len(exemplar["keywords"]), 8)
        self.assertEqual(exemplar["confidence"], 1.0)
        self.assertEqual(exemplar["quality_score"], 0.0)
        self.assertEqual(exemplar["response_length"], len("没事，下次再来？"))
        self.assertEqual(exemplar["punctuation"]["question"], 1)
        self.assertNotIn("unknown", exemplar)

    def test_context_retrieval_is_relevant_bounded_diverse_and_deterministic(self):
        value = profile()
        value["interests"] = ["考试复习", "电子游戏"]
        value["dimensions"] = [
            {
                "name": name,
                "scene": "group",
                "score": index / 10,
                "description": f"维度{index}",
                "confidence": 1 - index / 100,
                "evidence_count": 5,
            }
            for index, name in enumerate(
                [
                    "tone",
                    "sentence_length",
                    "punctuation_emoji",
                    "vocabulary",
                    "humor",
                    "directness",
                    "emotion",
                    "questioning",
                    "disagreement",
                    "interaction_rhythm",
                ]
            )
        ]
        value["phrases"] = [
            {
                "text": f"考试短语{index}",
                "scene": "group",
                "frequency": 10 - index,
                "confidence": 0.9,
                "intent": "comfort",
                "keywords": ["考试", "难过"],
            }
            for index in range(6)
        ] + [
            {
                "text": "开黑走起",
                "scene": "group",
                "frequency": 99,
                "confidence": 1,
                "intent": "joke",
                "keywords": ["游戏"],
            }
        ]
        value["exemplars"] = [
            {
                "situation": f"考试失利后的安慰场景{index}",
                "response": f"先缓缓，下一次再来{index}",
                "scene": "group",
                "intent": "comfort",
                "emotion": "caring",
                "keywords": ["考试", "失利"],
                "confidence": 0.9,
                "quality_score": 0.8,
                "source_at": 100 + index,
            }
            for index in range(6)
        ]
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.sqlite3")
            try:
                store.save_persona_version("u", value, 200, activate=True)
                engine = PersonaContentEngine(store, "u", "")
                first = engine.build_factor(
                    query="这次考试没考好，有点难过",
                    history=[{"role": "user", "content": "刚出成绩"}],
                ).guidance
                second = engine.build_factor(
                    query="这次考试没考好，有点难过",
                    history=[{"role": "user", "content": "刚出成绩"}],
                ).guidance
                self.assertEqual(first, second)
                self.assertIn("考试复习", first)
                self.assertNotIn("电子游戏", first)
                self.assertNotIn("开黑走起", first)
                self.assertLessEqual(first.count("当前强度"), 6)
                self.assertLessEqual(first.count("考试短语"), 4)
                self.assertLessEqual(first.count("- 场景："), 4)

                fallback = engine.build_factor(query="量子纠缠实验参数").guidance
                self.assertIn("总体性格与表达倾向", fallback)
                self.assertNotIn("脱敏风格样例", fallback)
            finally:
                store.close()

    def test_group_private_merge_uses_configured_scene_weight(self):
        grouped = profile(scene="group", score=1.0, evidence=10, summary="群聊总结")
        private = profile(scene="private", score=0.0, evidence=10, summary="私聊总结")
        merged = merge_persona_profiles([grouped, private], group_weight=0.70)
        values = {(item["name"], item["scene"]): item for item in merged["dimensions"]}
        # 场景画像分开保存；在线渲染时群聊描述可以优先排列，不会混成失真的单值。
        self.assertEqual(values[("directness", "group")]["score"], 1.0)
        self.assertEqual(values[("directness", "private")]["score"], 0.0)
        self.assertEqual(merged["group_message_count"], 10)
        self.assertEqual(merged["private_message_count"], 10)
        self.assertTrue(merged["summary"].startswith("群聊总结"))
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.sqlite3")
            try:
                store.save_persona_version("u", merged, 100, activate=True)
                guidance = PersonaContentEngine(
                    store, "u", "", group_style_weight=0.70
                ).build_factor().guidance
                self.assertLess(guidance.index("group表达"), guidance.index("private表达"))
            finally:
                store.close()

    def test_content_factor_is_bounded_and_contains_no_template_id(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.sqlite3")
            try:
                store.save_persona_version("123456789", profile(), 100, activate=True)
                factor = PersonaContentEngine(store, "123456789", "后备背景").build_factor()
                rendered = render_content_factors((factor,), max_total_chars=8000)
                self.assertIn("轻松直接", rendered)
                self.assertIn("不得冒充模板用户", rendered)
                self.assertNotIn("123456789", rendered)
                self.assertLessEqual(len(rendered), 8000)
            finally:
                store.close()


class PersonaStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "memory.sqlite3"
        self.store = MemoryStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_draft_activate_rollback_and_delete(self):
        first = self.store.save_persona_version("u", profile(summary="第一版"), 100, activate=True)
        second = self.store.save_persona_version("u", profile(summary="第二版"), 200)
        self.assertEqual((first, second), (1, 2))
        self.assertEqual(self.store.get_persona_profile("u", "后备")["summary"], "第一版")
        self.assertEqual(self.store.get_persona_profile("u", "", version=2)["summary"], "第二版")
        self.store.activate_persona_version("u", 2)
        self.assertEqual(self.store.get_persona_profile("u", "")["summary"], "第二版")
        self.assertEqual(self.store.rollback_persona_version("u"), 1)
        self.assertEqual(self.store.get_persona_profile("u", "")["summary"], "第一版")
        self.store.delete_persona_data("u")
        self.assertEqual(self.store.list_persona_versions("u"), [])

    def test_first_draft_does_not_change_online_fallback(self):
        version = self.store.save_persona_version(
            "u", profile(summary="尚未批准的人格"), 100, activate=False
        )
        online = self.store.get_persona_profile("u", "安全后备")
        draft = self.store.get_persona_profile("u", "安全后备", version=version)
        self.assertEqual(online["summary"], "安全后备")
        self.assertEqual(online["active_version"], 0)
        self.assertEqual(draft["summary"], "尚未批准的人格")
        self.assertEqual(
            self.store.conn.execute(
                "SELECT last_source_message_at FROM willingness_personas WHERE user_id='u'"
            ).fetchone()[0],
            20,
        )

    def test_evaluation_gate_and_forced_activation_are_aggregate_only(self):
        self.store.save_persona_version("u", profile(summary="第一版"), 100, activate=True)
        candidate = self.store.save_persona_version("u", profile(summary="第二版"), 200)
        with self.assertRaisesRegex(ValueError, "尚未通过"):
            self.store.activate_persona_version(
                "u", candidate, require_passed_evaluation=True
            )
        self.store.save_persona_evaluation(
            "u",
            {
                "version": candidate,
                "baseline_version": 1,
                "evaluated_at": 300,
                "sample_count": 10,
                "candidate_wins": 6,
                "baseline_wins": 4,
                "ties": 0,
                "neither_count": 0,
                "candidate_preference_rate": 0.6,
                "safety_failures": 0,
                "metrics": {
                    "candidate_style_distance": 0.2,
                    "baseline_style_distance": 0.3,
                    "raw_context": "绝不应保存的聊天正文",
                },
                "passed": True,
            },
        )
        self.store.activate_persona_version(
            "u", candidate, require_passed_evaluation=True, activated_at=400
        )
        stored = self.store.conn.execute(
            "SELECT metrics_json FROM persona_evaluations"
        ).fetchone()[0]
        self.assertNotIn("聊天正文", stored)
        self.assertEqual(self.store.get_persona_profile("u", "")["summary"], "第二版")

        third = self.store.save_persona_version("u", profile(summary="应急版"), 500)
        with self.assertRaisesRegex(ValueError, "非空原因"):
            self.store.activate_persona_version("u", third, force=True)
        self.store.activate_persona_version(
            "u",
            third,
            require_passed_evaluation=True,
            force=True,
            reason="线上紧急修复",
            activated_at=600,
        )
        event = self.store.conn.execute(
            "SELECT version, reason FROM persona_activation_events "
            "ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(event), (third, "线上紧急修复"))
        evaluation_columns = {
            row["name"]
            for row in self.store.conn.execute("PRAGMA table_info(persona_evaluations)")
        }
        self.assertNotIn("user_id", evaluation_columns)

    def test_collection_checkpoint_only_contains_derived_json(self):
        self.store.save_persona_collection_state(
            "u",
            "group:g",
            {
                "conversation_type": "group",
                "cursor": "cursor",
                "fingerprint": "hash",
                "derived_profile": profile(),
                "scanned_count": 8,
                "completed": True,
                "updated_at": 100,
            },
        )
        raw = self.store.conn.execute(
            "SELECT derived_profile_json FROM persona_collection_state"
        ).fetchone()[0]
        self.assertIn("轻松直接", raw)
        self.assertNotIn("聊天原文", raw)
        state = self.store.get_persona_collection_state("u", "group:g")
        self.assertTrue(state["completed"])
        self.assertEqual(state["derived_profile"]["summary"], "轻松直接")

    def test_old_persona_schema_is_migrated_without_data_loss(self):
        self.store.close()
        self.path.unlink()
        connection = sqlite3.connect(self.path)
        connection.execute(
            """CREATE TABLE willingness_personas (
                user_id TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '',
                interests TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 0,
                last_source_message_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0)"""
        )
        connection.execute(
            "INSERT INTO willingness_personas VALUES ('u', '旧摘要', '旧兴趣', 1, 10, 20)"
        )
        connection.commit()
        connection.close()
        self.store = MemoryStore(self.path)
        columns = {
            row["name"]
            for row in self.store.conn.execute("PRAGMA table_info(willingness_personas)")
        }
        self.assertIn("active_version", columns)
        self.assertEqual(self.store.get_persona_profile("u", "")["summary"], "旧摘要")

    def test_old_structured_persona_tables_gain_metadata_without_rebuild(self):
        self.store.close()
        self.path.unlink()
        connection = sqlite3.connect(self.path)
        connection.execute(
            """CREATE TABLE persona_phrases (
                user_id TEXT, version INTEGER, phrase TEXT, scene TEXT,
                frequency INTEGER, confidence REAL,
                PRIMARY KEY (user_id, version, phrase, scene))"""
        )
        connection.execute(
            "INSERT INTO persona_phrases VALUES ('u', 1, '确实', 'group', 3, 0.8)"
        )
        connection.execute(
            """CREATE TABLE persona_exemplars (
                user_id TEXT, version INTEGER, exemplar_order INTEGER,
                scene TEXT, situation TEXT, response TEXT,
                PRIMARY KEY (user_id, version, exemplar_order))"""
        )
        connection.execute(
            "INSERT INTO persona_exemplars VALUES ('u', 1, 0, 'group', '场景', '回复')"
        )
        connection.commit()
        connection.close()
        self.store = MemoryStore(self.path)
        phrase_columns = {
            row["name"]
            for row in self.store.conn.execute("PRAGMA table_info(persona_phrases)")
        }
        exemplar_columns = {
            row["name"]
            for row in self.store.conn.execute("PRAGMA table_info(persona_exemplars)")
        }
        self.assertTrue({"intent", "keywords_json"}.issubset(phrase_columns))
        self.assertTrue(
            {"intent", "emotion", "quality_score", "punctuation_json"}.issubset(
                exemplar_columns
            )
        )
        self.assertEqual(
            self.store.conn.execute("SELECT phrase FROM persona_phrases").fetchone()[0],
            "确实",
        )


class PersonaCollectorTest(unittest.IsolatedAsyncioTestCase):
    def test_selection_parser(self):
        self.assertEqual(parse_selection("1,3-4", 4), [0, 2, 3])
        self.assertEqual(parse_selection("all", 3), [0, 1, 2])
        with self.assertRaises(ValueError):
            parse_selection("5", 4)

    def test_evaluation_threshold_requires_ten_decisive_and_zero_safety_failures(self):
        self.assertEqual(
            _evaluation_passes(
                sample_count=10,
                candidate_wins=6,
                baseline_wins=4,
                safety_failures=0,
                min_samples=10,
            ),
            (True, 0.6),
        )
        self.assertFalse(
            _evaluation_passes(
                sample_count=10,
                candidate_wins=5,
                baseline_wins=4,
                safety_failures=0,
                min_samples=10,
            )[0]
        )
        self.assertFalse(
            _evaluation_passes(
                sample_count=10,
                candidate_wins=6,
                baseline_wins=4,
                safety_failures=1,
                min_samples=10,
            )[0]
        )

    async def test_evaluation_samples_strictly_exclude_training_period(self):
        page = {
            "messages": [
                {
                    "message_id": str(index),
                    "message_seq": str(index),
                    "time": timestamp,
                    "user_id": "target",
                    "message": [{"type": "text", "data": {"text": f"回复{index}"}}],
                }
                for index, timestamp in enumerate((99, 100, 101, 102), 1)
            ]
        }

        class RPC:
            async def request(_self, _action, _params):
                return page

        samples = await _evaluation_samples(
            RPC(),
            [Conversation("group:g", "group", "g", "测试")],
            "target",
            source_ended_at=100,
            limit=30,
        )
        self.assertEqual([item.sent_at for item in samples], [101, 102])

    async def test_evaluation_skips_one_napcat_conversation_with_empty_local_history(self):
        page = {
            "messages": [
                {
                    "message_id": "new",
                    "message_seq": "1",
                    "time": 101,
                    "user_id": "target",
                    "message": [{"type": "text", "data": {"text": "有效回复"}}],
                }
            ]
        }

        class RPC:
            async def request(_self, _action, params):
                if params.get("group_id") == "empty":
                    raise OneBotActionError(1200, "消息 undefined 不存在")
                return page

        with patch("sys.stderr"):
            samples = await _evaluation_samples(
                RPC(),
                [
                    Conversation("group:empty", "group", "empty", "空会话"),
                    Conversation("group:ok", "group", "ok", "有效会话"),
                ],
                "target",
                source_ended_at=100,
                limit=30,
            )
        self.assertEqual([item.response for item in samples], ["有效回复"])

    def test_context_builder_anonymizes_names_and_keeps_limited_context(self):
        messages = [
            {
                "message_id": str(index),
                "time": index,
                "user_id": "target" if index == 4 else f"u{index}",
                "sender": {"nickname": f"姓名{index}"},
                "message": [{"type": "text", "data": {"text": f"内容{index}"}}],
            }
            for index in range(1, 7)
        ]
        samples = build_persona_samples(messages, "target", "group", before=2, after=1)
        self.assertEqual(len(samples), 1)
        self.assertIn("内容2", samples[0].context)
        self.assertNotIn("内容5", samples[0].context)
        self.assertIn("内容5", samples[0].outcome)
        self.assertNotIn("内容1", samples[0].context)
        self.assertNotIn("姓名", samples[0].context)

    async def test_collection_stops_on_repeated_page_and_saves_only_derived_state(self):
        page = {
            "messages": [
                {
                    "message_id": "m1",
                    "message_seq": "9",
                    "time": 100,
                    "user_id": "other",
                    "sender": {"nickname": "真实朋友"},
                    "message": [{"type": "text", "data": {"text": "联系13800138000"}}],
                },
                {
                    "message_id": "m2",
                    "message_seq": "8",
                    "time": 101,
                    "user_id": "target",
                    "sender": {"nickname": "模板名字"},
                    "message": [{"type": "text", "data": {"text": "确实挺有意思"}}],
                },
            ]
        }

        class RPC:
            async def request(_self, action, params):
                return page

        class LLM:
            async def analyze_persona_samples(_self, samples, **_kwargs):
                self.assertNotIn("13800138000", samples[0].context)
                return profile(evidence=len(samples))

        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.sqlite3")
            try:
                result, report = await collect_conversation(
                    RPC(),
                    LLM(),
                    store,
                    Conversation("group:g", "group", "g", "测试"),
                    "target",
                    cutoff=0,
                    remaining=20,
                    seed_background="后备",
                    public_metadata={},
                    restart=True,
                )
                self.assertEqual(result["source_message_count"], 1)
                self.assertIn("重复页", report["gap"])
                persisted = store.conn.execute(
                    """SELECT conversation_key, processed_hashes_json,
                              derived_profile_json FROM persona_collection_state"""
                ).fetchone()
                self.assertNotEqual(persisted[0], "group:g")
                self.assertNotIn(":g", persisted[0])
                self.assertNotIn("m1", persisted[1])
                self.assertNotIn("m2", persisted[1])
                persisted = persisted[2]
                self.assertNotIn("13800138000", persisted)
                self.assertNotIn("真实朋友", persisted)
            finally:
                store.close()

    def test_merge_uses_time_decay_and_keeps_best_near_duplicate_representative(self):
        old = profile(score=0.0, summary="旧")
        old["source_ended_at"] = 20
        old["exemplars"][0].update(
            {"confidence": 0.2, "quality_score": 0.2, "evidence_count": 1}
        )
        recent = profile(score=1.0, summary="新")
        recent["source_ended_at"] = 20 + 180 * 86400
        recent["phrases"][0]["text"] = "确实！"
        recent["exemplars"][0].update(
            {
                "response": "这确实有点意思！",
                "confidence": 0.9,
                "quality_score": 0.9,
                "evidence_count": 8,
            }
        )
        merged = merge_persona_profiles([old, recent])
        self.assertGreater(merged["dimensions"][0]["score"], 0.6)
        self.assertEqual(len(merged["phrases"]), 1)
        self.assertEqual(merged["phrases"][0]["frequency"], 6)
        self.assertEqual(len(merged["exemplars"]), 1)
        self.assertEqual(merged["exemplars"][0]["response"], "这确实有点意思！")

    async def test_collection_carries_three_older_messages_across_page_boundary(self):
        newer = {
            "messages": [
                {
                    "message_id": "target",
                    "message_seq": "100",
                    "time": 100,
                    "user_id": "target",
                    "message": [{"type": "text", "data": {"text": "接上这句话"}}],
                }
            ]
        }
        older = {
            "messages": [
                {
                    "message_id": f"old-{index}",
                    "message_seq": str(100 - index),
                    "time": 100 - index,
                    "user_id": f"u{index}",
                    "message": [{"type": "text", "data": {"text": f"前文{index}"}}],
                }
                for index in range(1, 5)
            ]
        }

        class RPC:
            def __init__(_self):
                _self.calls = 0

            async def request(_self, _action, _params):
                _self.calls += 1
                return newer if _self.calls == 1 else older if _self.calls == 2 else {"messages": []}

        contexts = []

        class LLM:
            async def analyze_persona_samples(_self, samples, **_kwargs):
                contexts.extend(item.context for item in samples)
                return profile(evidence=len(samples))

        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.sqlite3")
            try:
                result, _report = await collect_conversation(
                    RPC(),
                    LLM(),
                    store,
                    Conversation("group:g", "group", "g", "测试"),
                    "target",
                    cutoff=0,
                    remaining=20,
                    seed_background="后备",
                    public_metadata={},
                    restart=True,
                )
                self.assertEqual(result["source_message_count"], 1)
                self.assertEqual(len(contexts), 1)
                self.assertNotIn("前文4", contexts[0])
                self.assertIn("前文3", contexts[0])
                self.assertIn("前文2", contexts[0])
                self.assertIn("前文1", contexts[0])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
