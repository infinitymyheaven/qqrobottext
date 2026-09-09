"""SQLite 长期记忆测试。"""

from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from src.memory import MemoryStore


class MemoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.sqlite3"
        self.store = MemoryStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_members_persist_and_keep_change_history(self):
        member = {
            "user_id": 10001,
            "nickname": "小明",
            "card": "明明",
            "role": "admin",
            "title": "活跃之星",
        }
        self.store.sync_members("30003", [member], 100.0, "测试群")
        member["role"] = "owner"
        member["title"] = "群主大人"
        self.store.sync_members("30003", [member], 200.0, "测试群")
        self.assertEqual(self.store.history_count("30003", "10001"), 2)
        self.store.close()

        self.store = MemoryStore(self.db_path)
        saved = self.store.get_member("30003", "10001")
        self.assertEqual(saved["role"], "owner")
        self.assertEqual(saved["title"], "群主大人")
        self.assertTrue(saved["is_active"])

    def test_missing_member_is_retained_as_inactive(self):
        member = {"user_id": 10001, "nickname": "小明", "role": "member"}
        self.store.sync_members("30003", [member], 100.0)
        self.store.sync_members("30003", [], 200.0)
        saved = self.store.get_member("30003", "10001")
        self.assertFalse(saved["is_active"])
        self.assertEqual(self.store.history_count("30003", "10001"), 2)
        self.assertEqual(
            self.store.search_members("30003", "小明", include_inactive=True)[0]["user_id"],
            "10001",
        )

    def test_future_event_deduplicates_and_persists_activity(self):
        args = dict(
            group_id="30003",
            source_user_id="10001",
            source_message_id="900",
            summary="明天开会",
            event_at=5000.0,
            remind_at=4000.0,
            created_at=1000.0,
        )
        self.assertTrue(self.store.add_future_event(**args))
        self.assertFalse(self.store.add_future_event(**args))
        self.assertEqual(len(self.store.get_active_events("30003", 2000.0)), 1)

        self.store.record_bot_message(
            "30003", "2026-09-09", 1000.0, spontaneous=True, morning=True
        )
        activity = self.store.get_activity("30003", "2026-09-09")
        self.assertEqual(activity["spontaneous_count"], 1)
        self.assertTrue(activity["morning_sent"])

    def test_daily_limit_persists_and_updates_when_range_changes(self):
        chosen = self.store.ensure_daily_spontaneous_limit(
            "30003", "2026-09-09", 60, 100, 77
        )
        self.assertEqual(chosen, 77)
        self.store.close()
        self.store = MemoryStore(self.db_path)
        self.assertEqual(
            self.store.ensure_daily_spontaneous_limit(
                "30003", "2026-09-09", 60, 100, 88
            ),
            77,
        )
        self.assertEqual(
            self.store.ensure_daily_spontaneous_limit(
                "30003", "2026-09-09", 10, 20, 15
            ),
            15,
        )
        self.assertEqual(
            self.store.ensure_daily_spontaneous_limit(
                "other", "2026-09-09", 60, 100, 88
            ),
            88,
        )
        self.assertEqual(
            self.store.ensure_daily_spontaneous_limit(
                "30003", "2026-09-10", 60, 100, 99
            ),
            99,
        )

    def test_old_activity_schema_is_migrated_without_data_loss(self):
        self.store.close()
        self.db_path.unlink()
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """CREATE TABLE bot_activity (
                group_id TEXT NOT NULL, local_date TEXT NOT NULL,
                spontaneous_count INTEGER NOT NULL DEFAULT 0,
                morning_sent INTEGER NOT NULL DEFAULT 0,
                night_sent INTEGER NOT NULL DEFAULT 0,
                last_bot_sent_at REAL,
                PRIMARY KEY (group_id, local_date))"""
        )
        connection.execute(
            "INSERT INTO bot_activity(group_id, local_date, spontaneous_count) VALUES ('g', 'd', 4)"
        )
        connection.commit()
        connection.close()
        self.store = MemoryStore(self.db_path)
        activity = self.store.get_activity("g", "d")
        self.assertEqual(activity["spontaneous_count"], 4)
        self.assertIsNone(activity["daily_spontaneous_limit"])
        self.assertEqual(activity["algorithm_reply_count"], 0)
        # 旧库打开后会原地补建规范化画像表，不需要删除或重建数据库。
        tables = {
            row[0]
            for row in self.store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue(
            {
                "speech_member_profiles",
                "speech_member_features",
                "speech_bot_profiles",
                "speech_bot_features",
                "willingness_topics",
                "willingness_topic_aliases",
                "willingness_group_topics",
                "willingness_personas",
            }.issubset(tables)
        )

    def test_sql_speech_profiles_persist_decay_and_sparse_features(self):
        self.store.observe_speech_message(
            "g",
            "u",
            100.0,
            {1: 1.0, 9: 0.5},
            activity_half_life_seconds=100.0,
            interest_learning_rate=0.2,
        )
        self.store.record_speech_reply(
            "g",
            "u",
            100.0,
            {1: 1.0},
            bond_half_life_seconds=1000.0,
            bond_learning_rate=0.1,
            interest_learning_rate=0.2,
        )
        self.store.close()
        self.store = MemoryStore(self.db_path)
        member, bot = self.store.get_speech_profiles(
            "g",
            "u",
            200.0,
            activity_half_life_seconds=100.0,
            bond_half_life_seconds=1000.0,
        )
        self.assertAlmostEqual(member["activity_value"], 0.5)
        self.assertAlmostEqual(member["bond_value"], 0.1 * 0.5 ** 0.1)
        self.assertEqual(member["message_count"], 1)
        self.assertEqual(member["reply_count"], 1)
        self.assertEqual(bot["reply_count"], 1)
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM speech_member_features"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM speech_bot_features"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
