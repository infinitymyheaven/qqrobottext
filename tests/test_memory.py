"""SQLite 长期记忆测试。"""

from __future__ import annotations

import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
