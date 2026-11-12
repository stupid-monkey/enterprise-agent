"""Persistent, membership-scoped department chat rooms."""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.security.policy import Subject


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class MeetingRoomRepository:
    def __init__(self, path: str | Path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS meeting_rooms (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, department_id TEXT NOT NULL,
                title TEXT NOT NULL, owner_id TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(org_id, department_id))""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS meeting_members (
                room_id TEXT NOT NULL, user_id TEXT NOT NULL, added_by TEXT NOT NULL,
                joined_at TEXT NOT NULL, PRIMARY KEY(room_id, user_id))""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS meeting_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL,
                sender_id TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS meeting_messages_room ON meeting_messages(room_id, id)")
            self._db.execute("CREATE INDEX IF NOT EXISTS meeting_members_user ON meeting_members(user_id, room_id)")

    def _visible(self, subject: Subject, room_id: str):
        return self._db.execute("""SELECT r.* FROM meeting_rooms r
            JOIN meeting_members m ON m.room_id = r.id AND m.user_id = ?
            WHERE r.id = ? AND r.org_id = ?""",
            (subject.user_id, room_id, subject.org_id)).fetchone()

    def list_for(self, subject: Subject) -> list[dict]:
        with self._lock:
            rows = self._db.execute("""SELECT r.*, COUNT(all_members.user_id) AS member_count,
                (SELECT text FROM meeting_messages WHERE room_id = r.id ORDER BY id DESC LIMIT 1) AS last_message,
                COALESCE((SELECT created_at FROM meeting_messages WHERE room_id = r.id ORDER BY id DESC LIMIT 1),
                         r.created_at) AS updated_at
                FROM meeting_rooms r
                JOIN meeting_members mine ON mine.room_id = r.id AND mine.user_id = ?
                JOIN meeting_members all_members ON all_members.room_id = r.id
                WHERE r.org_id = ? GROUP BY r.id ORDER BY updated_at DESC""",
                (subject.user_id, subject.org_id)).fetchall()
            return [dict(row) for row in rows]

    def create(self, subject: Subject, department_id: str, title: str) -> dict | None:
        if department_id not in subject.department_ids:
            return None
        room_id = uuid4().hex
        now = _now()
        with self._lock, self._db:
            try:
                self._db.execute("INSERT INTO meeting_rooms VALUES (?, ?, ?, ?, ?, ?)",
                                 (room_id, subject.org_id, department_id, title, subject.user_id, now))
            except sqlite3.IntegrityError:
                return None
            self._db.execute("INSERT INTO meeting_members VALUES (?, ?, ?, ?)",
                             (room_id, subject.user_id, subject.user_id, now))
        return self.get(subject, room_id)

    def get(self, subject: Subject, room_id: str) -> dict | None:
        with self._lock:
            room = self._visible(subject, room_id)
            if room is None:
                return None
            members = self._db.execute("""SELECT user_id, added_by, joined_at FROM meeting_members
                WHERE room_id = ? ORDER BY joined_at, user_id""", (room_id,)).fetchall()
            return dict(room) | {"members": [dict(row) for row in members]}

    def add_member(self, subject: Subject, room_id: str, user_id: str) -> bool:
        with self._lock, self._db:
            room = self._visible(subject, room_id)
            if room is None or room["owner_id"] != subject.user_id:
                return False
            self._db.execute("INSERT OR IGNORE INTO meeting_members VALUES (?, ?, ?, ?)",
                             (room_id, user_id, subject.user_id, _now()))
        return True

    def remove_member(self, subject: Subject, room_id: str, user_id: str) -> bool:
        with self._lock, self._db:
            room = self._visible(subject, room_id)
            if room is None or room["owner_id"] != subject.user_id or user_id == room["owner_id"]:
                return False
            cursor = self._db.execute("DELETE FROM meeting_members WHERE room_id = ? AND user_id = ?",
                                      (room_id, user_id))
            return cursor.rowcount > 0

    def messages(self, subject: Subject, room_id: str, *, after_id: int = 0,
                 before_id: int | None = None) -> list[dict] | None:
        with self._lock:
            if self._visible(subject, room_id) is None:
                return None
            if before_id is not None:
                rows = self._db.execute("""SELECT * FROM meeting_messages WHERE room_id = ? AND id < ?
                    ORDER BY id DESC LIMIT 100""", (room_id, before_id)).fetchall()
            elif after_id:
                rows = self._db.execute("""SELECT * FROM meeting_messages WHERE room_id = ? AND id > ?
                    ORDER BY id LIMIT 100""", (room_id, after_id)).fetchall()
                return [dict(row) for row in rows]
            else:
                rows = self._db.execute("""SELECT * FROM meeting_messages WHERE room_id = ?
                    ORDER BY id DESC LIMIT 100""", (room_id,)).fetchall()
            return [dict(row) for row in reversed(rows)]

    def post(self, subject: Subject, room_id: str, text: str) -> dict | None:
        with self._lock, self._db:
            if self._visible(subject, room_id) is None:
                return None
            now = _now()
            cursor = self._db.execute("INSERT INTO meeting_messages(room_id, sender_id, text, created_at) VALUES (?, ?, ?, ?)",
                                      (room_id, subject.user_id, text, now))
            return {"id": cursor.lastrowid, "room_id": room_id, "sender_id": subject.user_id,
                    "text": text, "created_at": now}

    def close(self) -> None:
        self._db.close()
