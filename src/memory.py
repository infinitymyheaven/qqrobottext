"""SQLite 持久化：群成员资料、未来事项和机器人每日活动。"""

from __future__ import annotations

import hashlib
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
                morning_sent INTEGER NOT NULL DEFAULT 0,
                night_sent INTEGER NOT NULL DEFAULT 0,
                last_bot_sent_at REAL,
                PRIMARY KEY (group_id, local_date)
            );
            """
        )
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
            "morning_sent": 0,
            "night_sent": 0,
            "last_bot_sent_at": None,
        }

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
