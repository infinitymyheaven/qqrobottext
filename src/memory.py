"""SQLite 持久化：群成员资料、未来事项和机器人每日活动。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Iterable


MEMBER_FIELDS = ("nickname", "card", "role", "title")


class MemoryStore:
    """小型同步 SQLite 存储；由 asyncio 事件循环单线程调用。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self) -> None:
        self.conn.close()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                group_id TEXT PRIMARY KEY,
                group_name TEXT NOT NULL DEFAULT '',
                last_synced_at REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS members (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                card TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'member',
                title TEXT NOT NULL DEFAULT '',
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (group_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS member_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                card TEXT NOT NULL,
                role TEXT NOT NULL,
                title TEXT NOT NULL,
                is_active INTEGER NOT NULL,
                observed_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_member_history_lookup
                ON member_history(group_id, user_id, observed_at);

            CREATE TABLE IF NOT EXISTS future_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL UNIQUE,
                group_id TEXT NOT NULL,
                source_user_id TEXT NOT NULL,
                source_message_id TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL,
                event_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                remind_at REAL NOT NULL,
                reminded_at REAL,
                acknowledged_at REAL,
                followup_at REAL,
                followup_sent_at REAL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_future_events_due
                ON future_events(remind_at, reminded_at, expires_at);

            CREATE TABLE IF NOT EXISTS bot_activity (
                group_id TEXT NOT NULL,
                local_date TEXT NOT NULL,
                spontaneous_count INTEGER NOT NULL DEFAULT 0,
                daily_spontaneous_limit INTEGER,
                algorithm_reply_count INTEGER NOT NULL DEFAULT 0,
                morning_sent INTEGER NOT NULL DEFAULT 0,
                night_sent INTEGER NOT NULL DEFAULT 0,
                last_bot_sent_at REAL,
                PRIMARY KEY (group_id, local_date)
            );

            -- 成员画像主表只保存标量；兴趣向量拆到子表，避免 JSON 黑盒字段。
            CREATE TABLE IF NOT EXISTS speech_member_profiles (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                activity_value REAL NOT NULL DEFAULT 0,
                activity_updated_at REAL NOT NULL DEFAULT 0,
                bond_value REAL NOT NULL DEFAULT 0,
                bond_updated_at REAL NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 0,
                reply_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, user_id)
            );

            -- 稀疏向量只保存非零维度，联合主键保证 UPSERT 可原子合并。
            CREATE TABLE IF NOT EXISTS speech_member_features (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                feature_id INTEGER NOT NULL,
                value REAL NOT NULL,
                PRIMARY KEY (group_id, user_id, feature_id),
                FOREIGN KEY (group_id, user_id)
                    REFERENCES speech_member_profiles(group_id, user_id)
                    ON DELETE CASCADE
            );

            -- 机器人话题画像按群隔离，防止不同群的聊天偏好相互污染。
            CREATE TABLE IF NOT EXISTS speech_bot_profiles (
                group_id TEXT PRIMARY KEY,
                reply_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );

            -- 机器人兴趣同样按“一维一行”保存，可直接用 SQL 查询和增量更新。
            CREATE TABLE IF NOT EXISTS speech_bot_features (
                group_id TEXT NOT NULL,
                feature_id INTEGER NOT NULL,
                value REAL NOT NULL,
                PRIMARY KEY (group_id, feature_id),
                FOREIGN KEY (group_id) REFERENCES speech_bot_profiles(group_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_speech_member_profiles_updated
                ON speech_member_profiles(group_id, updated_at);

            -- 全局话题知识与群内热度分离：知识可以复用，群聊热度不会串群。
            CREATE TABLE IF NOT EXISTS willingness_topics (
                topic_id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL DEFAULT '',
                public_knowledge TEXT NOT NULL DEFAULT '',
                familiarity REAL NOT NULL DEFAULT 0,
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                analysis_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS willingness_topic_features (
                topic_id TEXT NOT NULL,
                feature_id INTEGER NOT NULL,
                value REAL NOT NULL,
                PRIMARY KEY (topic_id, feature_id),
                FOREIGN KEY (topic_id) REFERENCES willingness_topics(topic_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS willingness_topic_aliases (
                topic_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                PRIMARY KEY (topic_id, alias),
                FOREIGN KEY (topic_id) REFERENCES willingness_topics(topic_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS willingness_group_topics (
                group_id TEXT NOT NULL,
                topic_id TEXT NOT NULL,
                heat_level TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                participant_count INTEGER NOT NULL DEFAULT 0,
                emotion_intensity REAL NOT NULL DEFAULT 0,
                reply_count INTEGER NOT NULL DEFAULT 0,
                analyzed_at REAL NOT NULL,
                PRIMARY KEY (group_id, topic_id),
                FOREIGN KEY (topic_id) REFERENCES willingness_topics(topic_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_willingness_group_topics
                ON willingness_group_topics(group_id, analyzed_at);

            CREATE TABLE IF NOT EXISTS willingness_analysis_state (
                group_id TEXT PRIMARY KEY,
                last_attempt_at REAL NOT NULL DEFAULT 0,
                last_success_at REAL NOT NULL DEFAULT 0,
                next_analysis_at REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS willingness_personas (
                user_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                interests TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 0,
                last_source_message_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS willingness_persona_features (
                user_id TEXT NOT NULL,
                feature_id INTEGER NOT NULL,
                value REAL NOT NULL,
                PRIMARY KEY (user_id, feature_id),
                FOREIGN KEY (user_id) REFERENCES willingness_personas(user_id)
                    ON DELETE CASCADE
            );

            -- 每次提炼生成不可变版本；只有显式激活的版本进入线上回答。
            CREATE TABLE IF NOT EXISTS persona_profile_versions (
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                summary TEXT NOT NULL DEFAULT '',
                interests TEXT NOT NULL DEFAULT '',
                source_started_at REAL NOT NULL DEFAULT 0,
                source_ended_at REAL NOT NULL DEFAULT 0,
                source_message_count INTEGER NOT NULL DEFAULT 0,
                group_message_count INTEGER NOT NULL DEFAULT 0,
                private_message_count INTEGER NOT NULL DEFAULT 0,
                coverage_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                PRIMARY KEY (user_id, version),
                FOREIGN KEY (user_id) REFERENCES willingness_personas(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS persona_style_dimensions (
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                scene TEXT NOT NULL,
                score REAL NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, version, name, scene),
                FOREIGN KEY (user_id, version)
                    REFERENCES persona_profile_versions(user_id, version)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS persona_phrases (
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                phrase TEXT NOT NULL,
                scene TEXT NOT NULL,
                frequency INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0,
                intent TEXT NOT NULL DEFAULT 'other',
                keywords_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (user_id, version, phrase, scene),
                FOREIGN KEY (user_id, version)
                    REFERENCES persona_profile_versions(user_id, version)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS persona_exemplars (
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                exemplar_order INTEGER NOT NULL,
                scene TEXT NOT NULL,
                situation TEXT NOT NULL DEFAULT '',
                response TEXT NOT NULL,
                intent TEXT NOT NULL DEFAULT 'other',
                emotion TEXT NOT NULL DEFAULT 'neutral',
                keywords_json TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 0,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                quality_score REAL NOT NULL DEFAULT 0,
                response_length INTEGER NOT NULL DEFAULT 0,
                punctuation_json TEXT NOT NULL DEFAULT '{}',
                source_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, version, exemplar_order),
                FOREIGN KEY (user_id, version)
                    REFERENCES persona_profile_versions(user_id, version)
                    ON DELETE CASCADE
            );

            -- 断点只保存游标、覆盖统计和模型派生画像，绝不保存原始会话。
            CREATE TABLE IF NOT EXISTS persona_collection_state (
                user_id TEXT NOT NULL,
                conversation_key TEXT NOT NULL,
                conversation_type TEXT NOT NULL,
                cursor TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT '',
                oldest_at REAL NOT NULL DEFAULT 0,
                newest_at REAL NOT NULL DEFAULT 0,
                scanned_count INTEGER NOT NULL DEFAULT 0,
                processed_hashes_json TEXT NOT NULL DEFAULT '[]',
                derived_profile_json TEXT NOT NULL DEFAULT '{}',
                completed INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, conversation_key)
            );

            CREATE TABLE IF NOT EXISTS persona_evaluations (
                evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL,
                baseline_version INTEGER NOT NULL DEFAULT 0,
                evaluated_at REAL NOT NULL,
                sample_count INTEGER NOT NULL,
                candidate_wins INTEGER NOT NULL,
                baseline_wins INTEGER NOT NULL,
                ties INTEGER NOT NULL,
                neither_count INTEGER NOT NULL,
                candidate_preference_rate REAL NOT NULL,
                safety_failures INTEGER NOT NULL DEFAULT 0,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                passed INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS persona_activation_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT ''
            );
            """
        )
        activity_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(bot_activity)")
        }
        if "daily_spontaneous_limit" not in activity_columns:
            self.conn.execute(
                "ALTER TABLE bot_activity ADD COLUMN daily_spontaneous_limit INTEGER"
            )
        if "algorithm_reply_count" not in activity_columns:
            self.conn.execute(
                "ALTER TABLE bot_activity ADD COLUMN algorithm_reply_count INTEGER NOT NULL DEFAULT 0"
            )
        persona_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(willingness_personas)")
        }
        if "active_version" not in persona_columns:
            self.conn.execute(
                "ALTER TABLE willingness_personas ADD COLUMN active_version INTEGER NOT NULL DEFAULT 0"
            )
        collection_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(persona_collection_state)")
        }
        if "processed_hashes_json" not in collection_columns:
            self.conn.execute(
                "ALTER TABLE persona_collection_state ADD COLUMN processed_hashes_json TEXT NOT NULL DEFAULT '[]'"
            )
        phrase_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(persona_phrases)")
        }
        for name, definition in (
            ("intent", "TEXT NOT NULL DEFAULT 'other'"),
            ("keywords_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if name not in phrase_columns:
                self.conn.execute(f"ALTER TABLE persona_phrases ADD COLUMN {name} {definition}")
        exemplar_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(persona_exemplars)")
        }
        for name, definition in (
            ("intent", "TEXT NOT NULL DEFAULT 'other'"),
            ("emotion", "TEXT NOT NULL DEFAULT 'neutral'"),
            ("keywords_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("confidence", "REAL NOT NULL DEFAULT 0"),
            ("evidence_count", "INTEGER NOT NULL DEFAULT 0"),
            ("quality_score", "REAL NOT NULL DEFAULT 0"),
            ("response_length", "INTEGER NOT NULL DEFAULT 0"),
            ("punctuation_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("source_at", "REAL NOT NULL DEFAULT 0"),
        ):
            if name not in exemplar_columns:
                self.conn.execute(f"ALTER TABLE persona_exemplars ADD COLUMN {name} {definition}")
        self.conn.commit()

    @staticmethod
    def _member_values(member: dict) -> dict:
        return {
            "user_id": str(member.get("user_id") or ""),
            "nickname": str(member.get("nickname") or ""),
            "card": str(member.get("card") or ""),
            "role": str(member.get("role") or "member"),
            "title": str(member.get("title") or ""),
        }

    def _snapshot(self, group_id: str, values: dict, active: bool, now: float) -> None:
        self.conn.execute(
            """
            INSERT INTO member_history
                (group_id, user_id, nickname, card, role, title, is_active, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                values["user_id"],
                values["nickname"],
                values["card"],
                values["role"],
                values["title"],
                int(active),
                now,
            ),
        )

    def sync_members(
        self,
        group_id: str | int,
        members: Iterable[dict],
        now: float,
        group_name: str = "",
    ) -> None:
        """保存完整成员快照，并把本次缺失的旧成员标记为已退群。"""
        gid = str(group_id)
        existing = {
            row["user_id"]: dict(row)
            for row in self.conn.execute(
                "SELECT * FROM members WHERE group_id = ?", (gid,)
            )
        }
        incoming_ids: set[str] = set()
        for raw_member in members:
            values = self._member_values(raw_member)
            uid = values["user_id"]
            if not uid:
                continue
            incoming_ids.add(uid)
            old = existing.get(uid)
            changed = old is None or not bool(old["is_active"]) or any(
                old[field] != values[field] for field in MEMBER_FIELDS
            )
            if changed:
                self._snapshot(gid, values, True, now)
            first_seen = old["first_seen_at"] if old else now
            self.conn.execute(
                """
                INSERT INTO members
                    (group_id, user_id, nickname, card, role, title,
                     first_seen_at, last_seen_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    nickname=excluded.nickname,
                    card=excluded.card,
                    role=excluded.role,
                    title=excluded.title,
                    last_seen_at=excluded.last_seen_at,
                    is_active=1
                """,
                (
                    gid,
                    uid,
                    values["nickname"],
                    values["card"],
                    values["role"],
                    values["title"],
                    first_seen,
                    now,
                ),
            )

        for uid, old in existing.items():
            if uid in incoming_ids or not bool(old["is_active"]):
                continue
            values = {field: old[field] for field in ("user_id", *MEMBER_FIELDS)}
            self._snapshot(gid, values, False, now)
            self.conn.execute(
                "UPDATE members SET is_active=0, last_seen_at=? WHERE group_id=? AND user_id=?",
                (now, gid, uid),
            )

        self.conn.execute(
            """
            INSERT INTO groups(group_id, group_name, last_synced_at) VALUES (?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                group_name=CASE WHEN excluded.group_name='' THEN groups.group_name
                                ELSE excluded.group_name END,
                last_synced_at=excluded.last_synced_at
            """,
            (gid, group_name, now),
        )
        self.conn.commit()

    def get_member(self, group_id: str | int, user_id: str | int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM members WHERE group_id=? AND user_id=?",
            (str(group_id), str(user_id)),
        ).fetchone()
        return dict(row) if row else None

    def get_members_by_role(self, group_id: str | int, roles: Iterable[str]) -> list[dict]:
        wanted = tuple(roles)
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        rows = self.conn.execute(
            f"""SELECT * FROM members
                WHERE group_id=? AND is_active=1 AND role IN ({placeholders})
                ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
                         COALESCE(NULLIF(card, ''), nickname)""",
            (str(group_id), *wanted),
        ).fetchall()
        return [dict(row) for row in rows]

    def search_members(
        self,
        group_id: str | int,
        query: str,
        *,
        include_inactive: bool = False,
        limit: int = 20,
    ) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM members WHERE group_id=? AND (is_active=1 OR ?=1)",
            (str(group_id), int(include_inactive)),
        ).fetchall()
        needle = query.casefold().strip()
        matches: list[tuple[int, dict]] = []
        for row in rows:
            item = dict(row)
            fields = [item["user_id"], item["nickname"], item["card"], item["title"]]
            score = 0
            for value in fields:
                folded = str(value).casefold().strip()
                if not folded:
                    continue
                if folded == needle:
                    score = max(score, 100)
                elif folded in needle:
                    score = max(score, 80 + min(len(folded), 15))
                elif needle and needle in folded:
                    score = max(score, 60 + min(len(needle), 15))
            if score:
                matches.append((score + int(bool(item["is_active"])), item))
        matches.sort(key=lambda pair: (-pair[0], pair[1]["user_id"]))
        return [item for _, item in matches[:limit]]

    def add_future_event(
        self,
        *,
        group_id: str | int,
        source_user_id: str | int,
        source_message_id: str | int | None,
        summary: str,
        event_at: float,
        remind_at: float,
        created_at: float,
    ) -> bool:
        normalized = " ".join(summary.split()).strip()
        if not normalized:
            return False
        raw_key = f"{group_id}|{source_user_id}|{normalized.casefold()}|{int(event_at)}"
        dedupe_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO future_events
                (dedupe_key, group_id, source_user_id, source_message_id, summary,
                 event_at, expires_at, remind_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dedupe_key,
                str(group_id),
                str(source_user_id),
                str(source_message_id or ""),
                normalized,
                event_at,
                event_at,
                remind_at,
                created_at,
            ),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_active_events(
        self, group_id: str | int, now: float, *, limit: int = 20
    ) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM future_events
               WHERE group_id=? AND expires_at>?
               ORDER BY event_at LIMIT ?""",
            (str(group_id), now, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def due_initial_reminders(self, now: float) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM future_events
               WHERE remind_at<=? AND reminded_at IS NULL AND expires_at>?
               ORDER BY remind_at""",
            (now, now),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_reminded(self, event_id: int, now: float, followup_at: float) -> None:
        self.conn.execute(
            "UPDATE future_events SET reminded_at=?, followup_at=? WHERE id=?",
            (now, followup_at, event_id),
        )
        self.conn.commit()

    def acknowledge_user(self, group_id: str | int, user_id: str | int, now: float) -> int:
        cursor = self.conn.execute(
            """UPDATE future_events SET acknowledged_at=?
               WHERE group_id=? AND source_user_id=?
                 AND reminded_at IS NOT NULL AND reminded_at<?
                 AND acknowledged_at IS NULL AND expires_at>?""",
            (now, str(group_id), str(user_id), now, now),
        )
        self.conn.commit()
        return cursor.rowcount

    def due_followups(self, now: float) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM future_events
               WHERE followup_at<=? AND acknowledged_at IS NULL
                 AND followup_sent_at IS NULL AND expires_at>?
               ORDER BY followup_at""",
            (now, now),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_followup_sent(self, event_id: int, now: float) -> None:
        self.conn.execute(
            "UPDATE future_events SET followup_sent_at=? WHERE id=?",
            (now, event_id),
        )
        self.conn.commit()

    def get_activity(self, group_id: str | int, local_date: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM bot_activity WHERE group_id=? AND local_date=?",
            (str(group_id), local_date),
        ).fetchone()
        if row:
            return dict(row)
        return {
            "group_id": str(group_id),
            "local_date": local_date,
            "spontaneous_count": 0,
            "daily_spontaneous_limit": None,
            "algorithm_reply_count": 0,
            "morning_sent": 0,
            "night_sent": 0,
            "last_bot_sent_at": None,
        }

    def ensure_daily_spontaneous_limit(
        self,
        group_id: str | int,
        local_date: str,
        minimum: int,
        maximum: int,
        candidate: int,
    ) -> int:
        """返回当天持久化上限；配置区间改变且旧值越界时更新。"""
        current = self.get_activity(group_id, local_date)["daily_spontaneous_limit"]
        if current is not None and minimum <= int(current) <= maximum:
            return int(current)
        chosen = min(maximum, max(minimum, int(candidate)))
        self.conn.execute(
            """
            INSERT INTO bot_activity(group_id, local_date, daily_spontaneous_limit)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id, local_date) DO UPDATE SET
                daily_spontaneous_limit=excluded.daily_spontaneous_limit
            """,
            (str(group_id), local_date, chosen),
        )
        self.conn.commit()
        return chosen

    # ==================== 消息流意愿模块持久化 ====================

    def get_algorithm_reply_count(self, group_id: str | int, local_date: str) -> int:
        """读取每群当天所有算法回复的成功发送数量。"""
        return int(
            self.get_activity(group_id, local_date).get("algorithm_reply_count") or 0
        )

    def record_algorithm_reply(
        self, group_id: str | int, local_date: str, sent_at: float
    ) -> None:
        """原子增加算法回复计数；问候和提醒不会调用本方法。"""
        self.conn.execute(
            """INSERT INTO bot_activity
                   (group_id, local_date, algorithm_reply_count, last_bot_sent_at)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(group_id, local_date) DO UPDATE SET
                   algorithm_reply_count=bot_activity.algorithm_reply_count+1,
                   last_bot_sent_at=excluded.last_bot_sent_at""",
            (str(group_id), local_date, sent_at),
        )
        self.conn.commit()

    @staticmethod
    def _ebbinghaus_bond(
        value: float,
        last_interaction_at: float,
        now: float,
        grace_seconds: float,
        zero_seconds: float,
    ) -> float:
        """一天宽限后指数遗忘，并在配置的约 30 天边界明确归零。"""
        value = min(1.0, max(0.0, float(value)))
        elapsed = max(0.0, now - float(last_interaction_at or 0))
        if not value or not last_interaction_at or elapsed <= grace_seconds:
            return value
        if elapsed >= zero_seconds or zero_seconds <= grace_seconds:
            return 0.0
        decay_span = zero_seconds - grace_seconds
        decayed = value * math.exp(-4.605 * (elapsed - grace_seconds) / decay_span)
        return 0.0 if decayed < 0.01 else decayed

    def get_willingness_bond(
        self,
        group_id: str | int,
        user_id: str | int,
        now: float,
        *,
        grace_seconds: float,
        zero_seconds: float,
    ) -> float:
        """读取当前时刻经过爱宾浩斯式衰减的关系强度。"""
        row = self.conn.execute(
            """SELECT bond_value, bond_updated_at FROM speech_member_profiles
               WHERE group_id=? AND user_id=?""",
            (str(group_id), str(user_id)),
        ).fetchone()
        if not row:
            return 0.0
        return self._ebbinghaus_bond(
            row["bond_value"], row["bond_updated_at"], now, grace_seconds, zero_seconds
        )

    def update_willingness_bond(
        self,
        group_id: str | int,
        user_id: str | int,
        now: float,
        learning_rate: float,
        *,
        grace_seconds: float = 86_400,
        zero_seconds: float = 30 * 86_400,
        inbound: bool = False,
        outbound: bool = False,
    ) -> float:
        """把现有关系向 1 拉近，并保留双向互动计数语义。"""
        gid, uid = str(group_id), str(user_id)
        row = self.conn.execute(
            """SELECT bond_value, bond_updated_at FROM speech_member_profiles
               WHERE group_id=? AND user_id=?""",
            (gid, uid),
        ).fetchone()
        # 更新前使用与决策一致的宽限和归零配置，防止读写采用不同遗忘曲线。
        current = (
            self._ebbinghaus_bond(
                row["bond_value"],
                row["bond_updated_at"],
                now,
                grace_seconds,
                zero_seconds,
            )
            if row
            else 0.0
        )
        updated = current + (1.0 - current) * learning_rate
        with self.conn:
            self.conn.execute(
                """INSERT INTO speech_member_profiles
                       (group_id, user_id, activity_updated_at, bond_value,
                        bond_updated_at, reply_count, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(group_id, user_id) DO UPDATE SET
                       bond_value=excluded.bond_value,
                       bond_updated_at=excluded.bond_updated_at,
                       reply_count=speech_member_profiles.reply_count+excluded.reply_count,
                       updated_at=excluded.updated_at""",
                (gid, uid, now, updated, now, int(outbound), now),
            )
        return updated

    def _read_feature_rows(self, table: str, key_column: str, key: str) -> dict[int, float]:
        """从固定的规范化稀疏特征表读取一组向量。"""
        rows = self.conn.execute(
            f"SELECT feature_id, value FROM {table} WHERE {key_column}=?", (key,)
        ).fetchall()
        return {int(row["feature_id"]): float(row["value"]) for row in rows}

    def list_willingness_topics(
        self, group_id: str | int, *, limit: int = 500
    ) -> list[dict]:
        """读取指定群最近分析过的话题及其全局知识。"""
        rows = self.conn.execute(
            """SELECT t.*, g.heat_level, g.message_count, g.participant_count,
                      g.emotion_intensity, g.reply_count, g.analyzed_at
               FROM willingness_group_topics AS g
               JOIN willingness_topics AS t ON t.topic_id=g.topic_id
               WHERE g.group_id=? ORDER BY g.analyzed_at DESC, g.message_count DESC
               LIMIT ?""",
            (str(group_id), limit),
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["features"] = self._read_feature_rows(
                "willingness_topic_features", "topic_id", item["topic_id"]
            )
            output.append(item)
        return output

    def get_willingness_analysis_state(self, group_id: str | int) -> dict:
        """读取群话题分析调度状态。"""
        row = self.conn.execute(
            "SELECT * FROM willingness_analysis_state WHERE group_id=?",
            (str(group_id),),
        ).fetchone()
        return dict(row) if row else {
            "group_id": str(group_id),
            "last_attempt_at": 0.0,
            "last_success_at": 0.0,
            "next_analysis_at": 0.0,
        }

    def mark_willingness_analysis_attempt(
        self, group_id: str | int, attempted_at: float, next_at: float
    ) -> None:
        """在调用模型前持久化尝试时间，防止失败后高频重试。"""
        self.conn.execute(
            """INSERT INTO willingness_analysis_state
                   (group_id, last_attempt_at, next_analysis_at)
               VALUES (?, ?, ?)
               ON CONFLICT(group_id) DO UPDATE SET
                   last_attempt_at=excluded.last_attempt_at,
                   next_analysis_at=excluded.next_analysis_at""",
            (str(group_id), attempted_at, next_at),
        )
        self.conn.commit()

    def mark_willingness_analysis_success(
        self, group_id: str | int, succeeded_at: float, next_at: float
    ) -> None:
        """记录成功分析和下次建议时间。"""
        self.conn.execute(
            """INSERT INTO willingness_analysis_state
                   (group_id, last_attempt_at, last_success_at, next_analysis_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(group_id) DO UPDATE SET
                   last_attempt_at=excluded.last_attempt_at,
                   last_success_at=excluded.last_success_at,
                   next_analysis_at=excluded.next_analysis_at""",
            (str(group_id), succeeded_at, succeeded_at, next_at),
        )
        self.conn.commit()

    @staticmethod
    def _topic_name(value) -> str:
        return " ".join(str(value or "").split())[:80]

    def save_willingness_topics(
        self,
        group_id: str | int,
        topics: Iterable[dict],
        enrichment: dict,
        analyzed_at: float,
    ) -> None:
        """校验模型输出，并用事务保存三级话题和规范化特征。"""
        cleaned = []
        for raw in topics:
            if not isinstance(raw, dict):
                continue
            name = self._topic_name(raw.get("name"))
            if not name:
                continue
            cleaned.append(
                {
                    "name": name,
                    "summary": " ".join(str(raw.get("summary") or "").split())[:500],
                    "aliases": [
                        self._topic_name(alias)
                        for alias in (raw.get("aliases") or [])[:20]
                        if self._topic_name(alias)
                    ] if isinstance(raw.get("aliases") or [], list) else [],
                    "message_count": max(1, int(raw.get("message_count") or 1)),
                    "participant_count": max(1, int(raw.get("participant_count") or 1)),
                    "emotion_intensity": min(
                        1.0, max(0.0, float(raw.get("emotion_intensity") or 0))
                    ),
                }
            )
        if not cleaned:
            return
        cleaned.sort(key=lambda item: item["message_count"], reverse=True)
        maximum = cleaned[0]["message_count"]
        with self.conn:
            for index, item in enumerate(cleaned):
                level = (
                    "core"
                    if index == 0
                    else "secondary"
                    if item["message_count"] >= maximum * 0.5
                    else "peripheral"
                )
                topic_id = hashlib.sha256(item["name"].casefold().encode("utf-8")).hexdigest()
                heat = {"core": 1.0, "secondary": 0.6, "peripheral": 0.3}[level]
                exposure = min(item["message_count"] / 50.0, 1.0)
                knowledge = str(enrichment.get(item["name"]) or "")[:2000]
                old = self.conn.execute(
                    "SELECT familiarity FROM willingness_topics WHERE topic_id=?",
                    (topic_id,),
                ).fetchone()
                familiarity = float(old["familiarity"]) if old else 0.0
                increment = 0.05 + 0.20 * heat * exposure + (0.10 if knowledge else 0.0)
                familiarity += (1.0 - familiarity) * increment
                self.conn.execute(
                    """INSERT INTO willingness_topics
                           (topic_id, name, summary, public_knowledge, familiarity,
                            first_seen_at, last_seen_at, analysis_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                       ON CONFLICT(topic_id) DO UPDATE SET
                           summary=excluded.summary,
                           public_knowledge=CASE WHEN excluded.public_knowledge=''
                               THEN willingness_topics.public_knowledge
                               ELSE excluded.public_knowledge END,
                           familiarity=excluded.familiarity,
                           last_seen_at=excluded.last_seen_at,
                           analysis_count=willingness_topics.analysis_count+1""",
                    (
                        topic_id,
                        item["name"],
                        item["summary"],
                        knowledge,
                        familiarity,
                        analyzed_at,
                        analyzed_at,
                    ),
                )
                self.conn.execute(
                    "DELETE FROM willingness_topic_features WHERE topic_id=?", (topic_id,)
                )
                vector = self._text_vector(
                    f"{item['name']} {' '.join(item['aliases'])} {item['summary']} {knowledge}"
                )
                self.conn.executemany(
                    """INSERT INTO willingness_topic_features(topic_id, feature_id, value)
                       VALUES (?, ?, ?)""",
                    [(topic_id, feature_id, value) for feature_id, value in vector.items()],
                )
                self.conn.executemany(
                    "INSERT OR IGNORE INTO willingness_topic_aliases(topic_id, alias) VALUES (?, ?)",
                    [(topic_id, alias) for alias in item["aliases"]],
                )
                self.conn.execute(
                    """INSERT INTO willingness_group_topics
                           (group_id, topic_id, heat_level, message_count,
                            participant_count, emotion_intensity, reply_count, analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(group_id, topic_id) DO UPDATE SET
                           heat_level=excluded.heat_level,
                           message_count=excluded.message_count,
                           participant_count=excluded.participant_count,
                           emotion_intensity=excluded.emotion_intensity,
                           reply_count=willingness_group_topics.reply_count+excluded.reply_count,
                           analyzed_at=excluded.analyzed_at""",
                    (
                        str(group_id),
                        topic_id,
                        level,
                        item["message_count"],
                        item["participant_count"],
                        item["emotion_intensity"],
                        item["message_count"],
                        analyzed_at,
                    ),
                )

    @staticmethod
    def _text_vector(text: str) -> dict[int, float]:
        """与意愿模块一致的稳定向量实现，避免存储层反向依赖业务模块。"""
        normalized = "".join(re.findall(r"[\w\u4e00-\u9fff]", text.casefold()))
        if not normalized:
            return {}
        tokens = [normalized] if len(normalized) == 1 else [
            normalized[index : index + 2] for index in range(len(normalized) - 1)
        ]
        counts: dict[int, float] = {}
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            feature_id = int.from_bytes(digest, "big") % 128
            counts[feature_id] = counts.get(feature_id, 0.0) + 1.0
        norm = math.sqrt(sum(value * value for value in counts.values()))
        return {feature_id: value / norm for feature_id, value in counts.items()}

    def get_persona_profile(
        self, user_id: str, fallback: str, *, version: int | None = None
    ) -> dict:
        """读取当前激活或指定人格版本，并继续提供意愿模块所需向量。"""
        row = self.conn.execute(
            "SELECT * FROM willingness_personas WHERE user_id=?", (str(user_id),)
        ).fetchone()
        if not row:
            return {
                "summary": fallback,
                "interests": [],
                "features": self._text_vector(fallback),
                "version": 0,
                "active_version": 0,
                "dimensions": [],
                "phrases": [],
                "exemplars": [],
            }
        profile = dict(row)
        selected_version = int(version if version is not None else profile.get("active_version") or 0)
        if selected_version:
            version_row = self.conn.execute(
                """SELECT * FROM persona_profile_versions
                   WHERE user_id=? AND version=?""",
                (str(user_id), selected_version),
            ).fetchone()
            if version_row:
                profile.update(dict(version_row))
                profile["interests"] = [
                    item for item in str(version_row["interests"]).split("、") if item
                ]
                profile["dimensions"] = [
                    dict(item)
                    for item in self.conn.execute(
                        """SELECT name, scene, score, description, confidence, evidence_count
                           FROM persona_style_dimensions
                           WHERE user_id=? AND version=? ORDER BY confidence DESC, name""",
                        (str(user_id), selected_version),
                    )
                ]
                profile["phrases"] = [
                    {
                        "text": item["phrase"],
                        "scene": item["scene"],
                        "frequency": item["frequency"],
                        "confidence": item["confidence"],
                        "intent": item["intent"],
                        "keywords": json.loads(item["keywords_json"] or "[]"),
                    }
                    for item in self.conn.execute(
                        """SELECT phrase, scene, frequency, confidence, intent, keywords_json
                           FROM persona_phrases WHERE user_id=? AND version=?
                           ORDER BY frequency DESC, phrase""",
                        (str(user_id), selected_version),
                    )
                ]
                profile["exemplars"] = [
                    {
                        **{
                            key: value
                            for key, value in dict(item).items()
                            if key not in {"keywords_json", "punctuation_json"}
                        },
                        "keywords": json.loads(item["keywords_json"] or "[]"),
                        "punctuation": json.loads(item["punctuation_json"] or "{}"),
                    }
                    for item in self.conn.execute(
                        """SELECT situation, response, scene, intent, emotion,
                                  keywords_json, confidence, evidence_count, quality_score,
                                  response_length, punctuation_json, source_at
                           FROM persona_exemplars
                           WHERE user_id=? AND version=? ORDER BY exemplar_order""",
                        (str(user_id), selected_version),
                    )
                ]
                profile["coverage"] = json.loads(version_row["coverage_json"] or "{}")
        else:
            has_drafts = self.conn.execute(
                "SELECT 1 FROM persona_profile_versions WHERE user_id=? LIMIT 1",
                (str(user_id),),
            ).fetchone()
            # 新系统中的未激活草稿不能影响线上；没有版本行的才是旧 schema 画像。
            if has_drafts:
                profile["summary"] = fallback
                profile["interests"] = []
            else:
                profile["interests"] = [
                    item for item in str(profile.get("interests") or "").split("、") if item
                ]
            profile.update({"dimensions": [], "phrases": [], "exemplars": []})
        features = self._read_feature_rows(
            "willingness_persona_features", "user_id", str(user_id)
        )
        profile["features"] = features or self._text_vector(str(profile.get("summary") or fallback))
        return profile

    def save_persona_profile(
        self, user_id: str, profile: dict, updated_at: float
    ) -> None:
        """兼容旧调用：保存并立即激活一个画像版本。"""
        self.save_persona_version(user_id, profile, updated_at, activate=True)

    def save_persona_version(
        self,
        user_id: str,
        profile: dict,
        updated_at: float,
        *,
        activate: bool = False,
    ) -> int:
        """保存不可变结构化画像；采集器默认生成草稿，线上更新可立即激活。"""
        uid = str(user_id)
        summary = " ".join(str(profile.get("summary") or "").split())[:2000]
        interests_raw = profile.get("interests") or []
        interests = (
            "、".join(" ".join(str(item).split())[:40] for item in interests_raw)
            if isinstance(interests_raw, list)
            else str(interests_raw)
        )[:1000]
        if not summary:
            raise ValueError("人格画像 summary 不能为空")
        source_at = float(
            profile.get("last_source_message_at")
            or profile.get("source_ended_at")
            or updated_at
        )
        with self.conn:
            existing = self.conn.execute(
                """SELECT version, active_version, summary, interests
                   FROM willingness_personas WHERE user_id=?""",
                (uid,),
            ).fetchone()
            next_version = int(existing["version"] if existing else 0) + 1
            active_version = int(existing["active_version"] if existing else 0)
            self.conn.execute(
                """INSERT INTO willingness_personas
                       (user_id, summary, interests, version, last_source_message_at,
                        updated_at, active_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       summary=CASE WHEN excluded.active_version>0
                           THEN excluded.summary ELSE willingness_personas.summary END,
                       interests=CASE WHEN excluded.active_version>0
                           THEN excluded.interests ELSE willingness_personas.interests END,
                       version=excluded.version,
                       last_source_message_at=MAX(
                           willingness_personas.last_source_message_at,
                           excluded.last_source_message_at),
                       updated_at=excluded.updated_at,
                       active_version=CASE WHEN excluded.active_version>0
                           THEN excluded.active_version ELSE willingness_personas.active_version END""",
                (
                    uid,
                    summary if activate else str(existing["summary"] if existing else ""),
                    interests if activate else str(existing["interests"] if existing else ""),
                    next_version,
                    source_at,
                    updated_at,
                    next_version if activate else active_version,
                ),
            )
            self.conn.execute(
                """INSERT INTO persona_profile_versions
                       (user_id, version, status, summary, interests,
                        source_started_at, source_ended_at, source_message_count,
                        group_message_count, private_message_count, coverage_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    uid,
                    next_version,
                    "active" if activate else "draft",
                    summary,
                    interests,
                    float(profile.get("source_started_at") or 0),
                    float(profile.get("source_ended_at") or source_at),
                    int(profile.get("source_message_count") or 0),
                    int(profile.get("group_message_count") or 0),
                    int(profile.get("private_message_count") or 0),
                    json.dumps(profile.get("coverage") or {}, ensure_ascii=False, sort_keys=True),
                    updated_at,
                ),
            )
            if activate:
                self.conn.execute(
                    """UPDATE persona_profile_versions SET status='archived'
                       WHERE user_id=? AND version<>? AND status='active'""",
                    (uid, next_version),
                )
            dimensions = [item for item in profile.get("dimensions") or [] if isinstance(item, dict)]
            self.conn.executemany(
                """INSERT INTO persona_style_dimensions
                       (user_id, version, name, scene, score, description,
                        confidence, evidence_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        uid,
                        next_version,
                        str(item.get("name") or "")[:40],
                        str(item.get("scene") or "all")[:16],
                        min(1.0, max(0.0, float(item.get("score") or 0))),
                        " ".join(str(item.get("description") or "").split())[:240],
                        min(1.0, max(0.0, float(item.get("confidence") or 0))),
                        max(0, int(item.get("evidence_count") or 0)),
                    )
                    for item in dimensions
                    if item.get("name")
                ],
            )
            phrases = [item for item in profile.get("phrases") or [] if isinstance(item, dict)]
            self.conn.executemany(
                """INSERT INTO persona_phrases
                       (user_id, version, phrase, scene, frequency, confidence,
                        intent, keywords_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        uid,
                        next_version,
                        " ".join(str(item.get("text") or "").split())[:40],
                        str(item.get("scene") or "all")[:16],
                        max(0, int(item.get("frequency") or 0)),
                        min(1.0, max(0.0, float(item.get("confidence") or 0))),
                        str(item.get("intent") or "other")[:16],
                        json.dumps(item.get("keywords") or [], ensure_ascii=False),
                    )
                    for item in phrases
                    if item.get("text")
                ],
            )
            exemplars = [item for item in profile.get("exemplars") or [] if isinstance(item, dict)]
            self.conn.executemany(
                """INSERT INTO persona_exemplars
                       (user_id, version, exemplar_order, scene, situation, response,
                        intent, emotion, keywords_json, confidence, evidence_count,
                        quality_score, response_length, punctuation_json, source_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        uid,
                        next_version,
                        index,
                        str(item.get("scene") or "group")[:16],
                        " ".join(str(item.get("situation") or "").split())[:80],
                        " ".join(str(item.get("response") or "").split())[:100],
                        str(item.get("intent") or "other")[:16],
                        str(item.get("emotion") or "neutral")[:16],
                        json.dumps(item.get("keywords") or [], ensure_ascii=False),
                        min(1.0, max(0.0, float(item.get("confidence") or 0))),
                        max(0, int(item.get("evidence_count") or 0)),
                        min(1.0, max(0.0, float(item.get("quality_score") or 0))),
                        max(0, int(item.get("response_length") or 0)),
                        json.dumps(item.get("punctuation") or {}, sort_keys=True),
                        max(0.0, float(item.get("source_at") or 0)),
                    )
                    for index, item in enumerate(exemplars[:200])
                    if item.get("response")
                ],
            )
            if not activate:
                return next_version
            self.conn.execute(
                "DELETE FROM willingness_persona_features WHERE user_id=?", (uid,)
            )
            vector = self._text_vector(f"{summary} {interests}")
            self.conn.executemany(
                """INSERT INTO willingness_persona_features(user_id, feature_id, value)
                   VALUES (?, ?, ?)""",
                [(uid, feature_id, value) for feature_id, value in vector.items()],
            )
        return next_version

    def list_persona_versions(self, user_id: str) -> list[dict]:
        """列出本地画像版本，不包含脱敏样例正文。"""
        return [
            dict(row)
            for row in self.conn.execute(
                """SELECT version, status, source_started_at, source_ended_at,
                          source_message_count, group_message_count,
                          private_message_count, created_at
                   FROM persona_profile_versions WHERE user_id=? ORDER BY version DESC""",
                (str(user_id),),
            )
        ]

    def get_latest_persona_profile(self, user_id: str, fallback: str = "") -> dict:
        """读取最新版本作为草稿合并基线，不改变线上激活版本。"""
        row = self.conn.execute(
            "SELECT MAX(version) AS version FROM persona_profile_versions WHERE user_id=?",
            (str(user_id),),
        ).fetchone()
        version = int(row["version"] or 0) if row else 0
        return self.get_persona_profile(user_id, fallback, version=version or None)

    def save_persona_evaluation(self, user_id: str, result: dict) -> int:
        """只保存评测汇总，调用方不得传入对话或候选正文。"""
        raw_metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        allowed_metrics = {}
        for key in ("candidate_style_distance", "baseline_style_distance"):
            try:
                allowed_metrics[key] = float(raw_metrics[key])
            except (KeyError, TypeError, ValueError):
                continue
        with self.conn:
            cursor = self.conn.execute(
                """INSERT INTO persona_evaluations
                       (version, baseline_version, evaluated_at, sample_count,
                        candidate_wins, baseline_wins, ties, neither_count,
                        candidate_preference_rate, safety_failures, metrics_json, passed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(result["version"]),
                    int(result.get("baseline_version") or 0),
                    float(result["evaluated_at"]),
                    max(0, int(result.get("sample_count") or 0)),
                    max(0, int(result.get("candidate_wins") or 0)),
                    max(0, int(result.get("baseline_wins") or 0)),
                    max(0, int(result.get("ties") or 0)),
                    max(0, int(result.get("neither_count") or 0)),
                    min(1.0, max(0.0, float(result.get("candidate_preference_rate") or 0))),
                    max(0, int(result.get("safety_failures") or 0)),
                    json.dumps(allowed_metrics, ensure_ascii=False, sort_keys=True),
                    int(bool(result.get("passed"))),
                ),
            )
        return int(cursor.lastrowid)

    def get_latest_persona_evaluation(self, user_id: str, version: int) -> dict:
        row = self.conn.execute(
            """SELECT * FROM persona_evaluations
               WHERE version=? ORDER BY evaluation_id DESC LIMIT 1""",
            (int(version),),
        ).fetchone()
        if not row:
            return {}
        result = dict(row)
        result["passed"] = bool(result["passed"])
        result["metrics"] = json.loads(result.pop("metrics_json") or "{}")
        return result

    def activate_persona_version(
        self,
        user_id: str,
        version: int,
        *,
        require_passed_evaluation: bool = False,
        force: bool = False,
        reason: str = "",
        activated_at: float = 0,
    ) -> None:
        """原子激活指定版本，并同步意愿算法使用的相关性向量。"""
        uid = str(user_id)
        row = self.conn.execute(
            "SELECT summary, interests FROM persona_profile_versions WHERE user_id=? AND version=?",
            (uid, int(version)),
        ).fetchone()
        if not row:
            raise ValueError(f"人格版本不存在：{version}")
        if force and not str(reason).strip():
            raise ValueError("强制激活必须提供非空原因")
        evaluation = self.get_latest_persona_evaluation(uid, int(version))
        if require_passed_evaluation and not force and not evaluation.get("passed"):
            raise ValueError("人格版本尚未通过留出评测；如需应急激活请使用 --force --reason")
        with self.conn:
            self.conn.execute(
                "UPDATE persona_profile_versions SET status='archived' WHERE user_id=? AND status='active'",
                (uid,),
            )
            self.conn.execute(
                "UPDATE persona_profile_versions SET status='active' WHERE user_id=? AND version=?",
                (uid, int(version)),
            )
            self.conn.execute(
                """UPDATE willingness_personas SET summary=?, interests=?,
                          active_version=?,
                          updated_at=MAX(updated_at, CAST(strftime('%s','now') AS REAL))
                   WHERE user_id=?""",
                (row["summary"], row["interests"], int(version), uid),
            )
            self.conn.execute("DELETE FROM willingness_persona_features WHERE user_id=?", (uid,))
            vector = self._text_vector(f"{row['summary']} {row['interests']}")
            self.conn.executemany(
                "INSERT INTO willingness_persona_features(user_id, feature_id, value) VALUES (?, ?, ?)",
                [(uid, feature_id, value) for feature_id, value in vector.items()],
            )
            if force:
                self.conn.execute(
                    """INSERT INTO persona_activation_events(version, reason)
                       VALUES (?, ?)""",
                    (int(version), " ".join(str(reason).split())[:240]),
                )

    def rollback_persona_version(self, user_id: str) -> int:
        """回退到当前激活版本之前最近的版本。"""
        uid = str(user_id)
        row = self.conn.execute(
            "SELECT active_version FROM willingness_personas WHERE user_id=?", (uid,)
        ).fetchone()
        active = int(row["active_version"] if row else 0)
        previous = self.conn.execute(
            """SELECT MAX(version) AS version FROM persona_profile_versions
               WHERE user_id=? AND version<? AND status='archived'""",
            (uid, active),
        ).fetchone()
        version = int(previous["version"] or 0)
        if not version:
            raise ValueError("没有可回退的人格版本")
        self.activate_persona_version(uid, version)
        return version

    def delete_persona_data(self, user_id: str) -> None:
        """按用户删除画像、派生样例和采集断点；不影响其他机器人记忆。"""
        uid = str(user_id)
        with self.conn:
            versions = [
                int(row["version"])
                for row in self.conn.execute(
                    "SELECT version FROM persona_profile_versions WHERE user_id=?", (uid,)
                )
            ]
            if versions:
                placeholders = ",".join("?" for _ in versions)
                self.conn.execute(
                    f"DELETE FROM persona_evaluations WHERE version IN ({placeholders})",
                    versions,
                )
                self.conn.execute(
                    f"DELETE FROM persona_activation_events WHERE version IN ({placeholders})",
                    versions,
                )
            self.conn.execute("DELETE FROM persona_collection_state WHERE user_id=?", (uid,))
            self.conn.execute("DELETE FROM willingness_personas WHERE user_id=?", (uid,))

    def get_persona_collection_state(self, user_id: str, conversation_key: str) -> dict:
        row = self.conn.execute(
            """SELECT * FROM persona_collection_state
               WHERE user_id=? AND conversation_key=?""",
            (str(user_id), str(conversation_key)),
        ).fetchone()
        if not row:
            return {}
        value = dict(row)
        try:
            value["derived_profile"] = json.loads(value.pop("derived_profile_json") or "{}")
        except json.JSONDecodeError:
            value["derived_profile"] = {}
        try:
            value["processed_hashes"] = json.loads(
                value.pop("processed_hashes_json") or "[]"
            )
        except json.JSONDecodeError:
            value["processed_hashes"] = []
        return value

    def save_persona_collection_state(
        self, user_id: str, conversation_key: str, state: dict
    ) -> None:
        """保存可恢复断点；调用方只能传模型派生画像，不能传原始消息。"""
        derived = state.get("derived_profile") or {}
        with self.conn:
            self.conn.execute(
                """INSERT INTO persona_collection_state
                       (user_id, conversation_key, conversation_type, cursor,
                        fingerprint, oldest_at, newest_at, scanned_count,
                        processed_hashes_json, derived_profile_json, completed, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, conversation_key) DO UPDATE SET
                       conversation_type=excluded.conversation_type,
                       cursor=excluded.cursor, fingerprint=excluded.fingerprint,
                       oldest_at=excluded.oldest_at, newest_at=excluded.newest_at,
                       scanned_count=excluded.scanned_count,
                       processed_hashes_json=excluded.processed_hashes_json,
                       derived_profile_json=excluded.derived_profile_json,
                       completed=excluded.completed, updated_at=excluded.updated_at""",
                (
                    str(user_id),
                    str(conversation_key),
                    str(state.get("conversation_type") or "group"),
                    str(state.get("cursor") or ""),
                    str(state.get("fingerprint") or ""),
                    float(state.get("oldest_at") or 0),
                    float(state.get("newest_at") or 0),
                    int(state.get("scanned_count") or 0),
                    json.dumps(list(state.get("processed_hashes") or [])[-50_000:]),
                    json.dumps(derived, ensure_ascii=False, sort_keys=True),
                    int(bool(state.get("completed"))),
                    float(state.get("updated_at") or 0),
                ),
            )

    def record_bot_message(
        self,
        group_id: str | int,
        local_date: str,
        sent_at: float,
        *,
        spontaneous: bool = False,
        morning: bool = False,
        night: bool = False,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO bot_activity
                (group_id, local_date, spontaneous_count, morning_sent,
                 night_sent, last_bot_sent_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, local_date) DO UPDATE SET
                spontaneous_count=bot_activity.spontaneous_count+excluded.spontaneous_count,
                morning_sent=MAX(bot_activity.morning_sent, excluded.morning_sent),
                night_sent=MAX(bot_activity.night_sent, excluded.night_sent),
                last_bot_sent_at=excluded.last_bot_sent_at
            """,
            (
                str(group_id),
                local_date,
                int(spontaneous),
                int(morning),
                int(night),
                sent_at,
            ),
        )
        self.conn.commit()

    def history_count(self, group_id: str | int, user_id: str | int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM member_history WHERE group_id=? AND user_id=?",
            (str(group_id), str(user_id)),
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def _decay(value: float, updated_at: float, now: float, half_life_seconds: float) -> float:
        """按半衰期衰减长期数值；读取和写入都使用同一时间语义。"""
        if value <= 0 or updated_at <= 0 or now <= updated_at:
            return max(0.0, float(value))
        return float(value) * 0.5 ** ((now - updated_at) / half_life_seconds)

    def _member_features(self, group_id: str, user_id: str) -> dict[int, float]:
        """通过联合索引读取单个成员的全部非零兴趣维度。"""
        rows = self.conn.execute(
            """SELECT feature_id, value FROM speech_member_features
               WHERE group_id=? AND user_id=?""",
            (group_id, user_id),
        ).fetchall()
        return {int(row["feature_id"]): float(row["value"]) for row in rows}

    def _bot_features(self, group_id: str) -> dict[int, float]:
        """读取机器人在指定群内学习到的稀疏话题向量。"""
        rows = self.conn.execute(
            "SELECT feature_id, value FROM speech_bot_features WHERE group_id=?",
            (group_id,),
        ).fetchall()
        return {int(row["feature_id"]): float(row["value"]) for row in rows}

    def get_speech_profiles(
        self,
        group_id: str | int,
        user_id: str | int,
        now: float,
        *,
        activity_half_life_seconds: float,
        bond_half_life_seconds: float,
    ) -> tuple[dict, dict]:
        """读取发言者和机器人画像，并按读取时刻计算衰减值。"""
        gid, uid = str(group_id), str(user_id)
        # 主表和特征表分开读取，让 SQL schema 保持规范化且便于独立索引。
        row = self.conn.execute(
            "SELECT * FROM speech_member_profiles WHERE group_id=? AND user_id=?",
            (gid, uid),
        ).fetchone()
        if row:
            member = dict(row)
            member["activity_value"] = self._decay(
                member["activity_value"],
                member["activity_updated_at"],
                now,
                activity_half_life_seconds,
            )
            member["bond_value"] = self._decay(
                member["bond_value"],
                member["bond_updated_at"],
                now,
                bond_half_life_seconds,
            )
            member["features"] = self._member_features(gid, uid)
        else:
            member = {
                "activity_value": 0.0,
                "bond_value": 0.0,
                "message_count": 0,
                "reply_count": 0,
                "features": {},
            }
        bot_row = self.conn.execute(
            "SELECT * FROM speech_bot_profiles WHERE group_id=?", (gid,)
        ).fetchone()
        bot = dict(bot_row) if bot_row else {"reply_count": 0, "updated_at": 0.0}
        bot["features"] = self._bot_features(gid) if bot_row else {}
        return member, bot

    @staticmethod
    def _update_sparse_features(
        connection: sqlite3.Connection,
        table: str,
        keys: tuple,
        features: dict[int, float],
        learning_rate: float,
    ) -> None:
        """在当前事务中用 EMA 更新一组规范化稀疏特征行。"""
        # 表名只能来自本类内部的两个固定调用点，不接受任何外部输入。
        where_columns = (
            "group_id=? AND user_id=?" if table == "speech_member_features" else "group_id=?"
        )
        # 先整体衰减旧向量，连本次消息未出现的维度也必须降低权重。
        connection.execute(
            f"UPDATE {table} SET value=value*? WHERE {where_columns}",
            (1.0 - learning_rate, *keys),
        )
        if table == "speech_member_features":
            sql = """INSERT INTO speech_member_features(group_id, user_id, feature_id, value)
                     VALUES (?, ?, ?, ?)
                     ON CONFLICT(group_id, user_id, feature_id) DO UPDATE SET
                         value=speech_member_features.value+excluded.value"""
        else:
            sql = """INSERT INTO speech_bot_features(group_id, feature_id, value)
                     VALUES (?, ?, ?)
                     ON CONFLICT(group_id, feature_id) DO UPDATE SET
                         value=speech_bot_features.value+excluded.value"""
        # 再批量 UPSERT 本次非零特征；整个过程由调用方事务包裹。
        connection.executemany(
            sql,
            [(*keys, feature_id, learning_rate * value) for feature_id, value in features.items()],
        )
        # 删除接近零的行，长期运行时数据库不会积累无意义的微小维度。
        connection.execute(
            f"DELETE FROM {table} WHERE {where_columns} AND ABS(value)<1e-9", keys
        )

    def observe_speech_message(
        self,
        group_id: str | int,
        user_id: str | int,
        now: float,
        features: dict[int, float],
        *,
        activity_half_life_seconds: float,
        interest_learning_rate: float,
    ) -> None:
        """事务化记录一条成员文字消息及其稀疏兴趣特征。"""
        gid, uid = str(group_id), str(user_id)
        row = self.conn.execute(
            "SELECT activity_value, activity_updated_at FROM speech_member_profiles "
            "WHERE group_id=? AND user_id=?",
            (gid, uid),
        ).fetchone()
        # 新消息贡献 1；旧活跃值先按距上次消息的时间做半衰期衰减。
        activity = 1.0
        if row:
            activity += self._decay(
                row["activity_value"], row["activity_updated_at"], now, activity_half_life_seconds
            )
        # 主表计数和子表向量必须同时成功或同时回滚。
        with self.conn:
            self.conn.execute(
                """INSERT INTO speech_member_profiles
                       (group_id, user_id, activity_value, activity_updated_at,
                        bond_updated_at, message_count, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(group_id, user_id) DO UPDATE SET
                       activity_value=excluded.activity_value,
                       activity_updated_at=excluded.activity_updated_at,
                       message_count=speech_member_profiles.message_count+1,
                       updated_at=excluded.updated_at""",
                (gid, uid, activity, now, now, now),
            )
            self._update_sparse_features(
                self.conn,
                "speech_member_features",
                (gid, uid),
                features,
                interest_learning_rate,
            )

    def record_speech_reply(
        self,
        group_id: str | int,
        user_id: str | int,
        now: float,
        features: dict[int, float],
        *,
        bond_half_life_seconds: float,
        bond_learning_rate: float,
        interest_learning_rate: float,
    ) -> None:
        """事务化强化成员关系，并更新机器人在该群参与过的话题。"""
        gid, uid = str(group_id), str(user_id)
        row = self.conn.execute(
            "SELECT bond_value, bond_updated_at FROM speech_member_profiles "
            "WHERE group_id=? AND user_id=?",
            (gid, uid),
        ).fetchone()
        bond = 0.0
        if row:
            bond = self._decay(
                row["bond_value"], row["bond_updated_at"], now, bond_half_life_seconds
            )
        # 每次成功回答把关系值向 1 拉近，避免线性累加突破合法范围。
        bond += (1.0 - bond) * bond_learning_rate
        # 成员关系、机器人计数和机器人话题向量组成一个不可分割的事务。
        with self.conn:
            self.conn.execute(
                """INSERT INTO speech_member_profiles
                       (group_id, user_id, activity_updated_at, bond_value,
                        bond_updated_at, reply_count, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(group_id, user_id) DO UPDATE SET
                       bond_value=excluded.bond_value,
                       bond_updated_at=excluded.bond_updated_at,
                       reply_count=speech_member_profiles.reply_count+1,
                       updated_at=excluded.updated_at""",
                (gid, uid, now, bond, now, now),
            )
            self.conn.execute(
                """INSERT INTO speech_bot_profiles(group_id, reply_count, updated_at)
                   VALUES (?, 1, ?)
                   ON CONFLICT(group_id) DO UPDATE SET
                       reply_count=speech_bot_profiles.reply_count+1,
                       updated_at=excluded.updated_at""",
                (gid, now),
            )
            self._update_sparse_features(
                self.conn,
                "speech_bot_features",
                (gid,),
                features,
                interest_learning_rate,
            )
