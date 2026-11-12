"""Owner-scoped local question history with read-time source reauthorization."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.api.documents import DocumentRepository
from app.security.policy import Subject


class ConversationRepository:
    def __init__(self, path: str | Path, *, enable_delete_outbox: bool = False):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.enable_delete_outbox = enable_delete_outbox
        with self._lock, self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                title TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            self._db.execute("CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(org_id, owner_id, updated_at DESC)")
            self._db.execute("""CREATE TABLE IF NOT EXISTS conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                question TEXT NOT NULL, mode TEXT NOT NULL, answer_json TEXT NOT NULL,
                workflow_run_id TEXT
            )""")
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(conversation_turns)")}
            if "workflow_run_id" not in columns:
                self._db.execute("ALTER TABLE conversation_turns ADD COLUMN workflow_run_id TEXT")
            self._db.execute("CREATE INDEX IF NOT EXISTS turns_conversation ON conversation_turns(conversation_id, id)")
            self._db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS turns_workflow_run
                ON conversation_turns(workflow_run_id) WHERE workflow_run_id IS NOT NULL""")
            if enable_delete_outbox:
                self._db.execute("""CREATE TABLE IF NOT EXISTS conversation_delete_outbox (
                    id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    available_at TEXT NOT NULL, delivered_at TEXT,
                    lease_token TEXT, lease_until TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, last_error_code TEXT
                )""")
                self._db.execute("""CREATE INDEX IF NOT EXISTS conversation_delete_outbox_ready
                    ON conversation_delete_outbox(delivered_at, available_at, created_at)""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS conversation_memory_outbox (
                    id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, turn_id INTEGER NOT NULL,
                    include_ordinary INTEGER NOT NULL, created_at TEXT NOT NULL,
                    available_at TEXT NOT NULL, delivered_at TEXT,
                    lease_token TEXT, lease_until TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, last_error_code TEXT
                )""")
                self._db.execute("""CREATE INDEX IF NOT EXISTS conversation_memory_outbox_ready
                    ON conversation_memory_outbox(delivered_at, available_at, created_at)""")

    def _record_delete_event(self, subject: Subject, conversation_id: str) -> None:
        if not self.enable_delete_outbox:
            return
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        self._db.execute("""INSERT INTO conversation_delete_outbox
            (id, org_id, owner_id, conversation_id, created_at, available_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (uuid4().hex, subject.org_id, subject.user_id, conversation_id, now, now))

    def owns(self, subject: Subject, conversation_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM conversations WHERE id = ? AND org_id = ? AND owner_id = ?",
                                    (conversation_id, subject.org_id, subject.user_id)).fetchone() is not None

    @contextmanager
    def hold_owned_source(self, subject: Subject, conversation_id: str):
        """Keep a source live through a cross-store manual suggestion acceptance.

        BEGIN IMMEDIATE serializes deletion by other SQLite connections too. The
        PostgreSQL write commits before this short SQLite guard is released.
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                owned = self._db.execute("""SELECT 1 FROM conversations
                    WHERE id = ? AND org_id = ? AND owner_id = ?""",
                    (conversation_id, subject.org_id, subject.user_id)).fetchone() is not None
                yield owned
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def owned_source_ids(self, subject: Subject, conversation_ids: set[str]) -> set[str]:
        """Read source liveness in one SQLite snapshot for a scoped memory read."""
        if not conversation_ids:
            return set()
        if len(conversation_ids) > 100:
            raise ValueError("TOO_MANY_CONVERSATION_SOURCES")
        placeholders = ", ".join("?" for _ in conversation_ids)
        with self._lock:
            rows = self._db.execute(
                f"SELECT id FROM conversations WHERE org_id = ? AND owner_id = ? "
                f"AND id IN ({placeholders})",
                (subject.org_id, subject.user_id, *conversation_ids),
            ).fetchall()
        return {row["id"] for row in rows}

    def append(self, subject: Subject, conversation_id: str | None, question: str,
               mode: str, answer: dict, workflow_run_id: str | None = None,
               *, memory_include_ordinary: bool | None = None) -> str | None:
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._lock, self._db:
            if workflow_run_id is not None:
                existing = self._db.execute("""SELECT c.id FROM conversation_turns t
                    JOIN conversations c ON c.id = t.conversation_id
                    WHERE t.workflow_run_id = ? AND c.org_id = ? AND c.owner_id = ?""",
                    (workflow_run_id, subject.org_id, subject.user_id)).fetchone()
                if existing:
                    return existing["id"]
            if conversation_id is None:
                conversation_id = uuid4().hex
                self._db.execute("INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
                                 (conversation_id, subject.org_id, subject.user_id, question[:80], now))
            elif not self.owns(subject, conversation_id):
                return None
            turn = self._db.execute("""INSERT INTO conversation_turns
                (conversation_id, question, mode, answer_json, workflow_run_id) VALUES (?, ?, ?, ?, ?)""",
                (conversation_id, question, mode, json.dumps(answer, ensure_ascii=False), workflow_run_id))
            if memory_include_ordinary is not None and self.enable_delete_outbox and mode == "general":
                from app.api.memory_suggestions import eligible_auto_memory_text, eligible_user_text

                eligible = (eligible_auto_memory_text if memory_include_ordinary
                            else eligible_user_text)
                if eligible(question):
                    self._db.execute("""INSERT INTO conversation_memory_outbox
                        (id, org_id, owner_id, conversation_id, turn_id,
                         include_ordinary, created_at, available_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (uuid4().hex, subject.org_id, subject.user_id, conversation_id,
                         turn.lastrowid, int(memory_include_ordinary), now, now))
            self._db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        return conversation_id

    def list_for(self, subject: Subject) -> list[dict]:
        with self._lock:
            rows = self._db.execute("""SELECT c.id, c.title, c.updated_at, COUNT(t.id) AS turn_count
                FROM conversations c LEFT JOIN conversation_turns t ON t.conversation_id = c.id
                WHERE c.org_id = ? AND c.owner_id = ? GROUP BY c.id
                ORDER BY c.updated_at DESC LIMIT 30""", (subject.org_id, subject.user_id)).fetchall()
        return [dict(row) for row in rows]

    def delete(self, subject: Subject, conversation_id: str) -> bool:
        """Remove only the owner's conversation and all its saved turns."""
        with self._lock, self._db:
            row = self._db.execute("""SELECT 1 FROM conversations
                WHERE id = ? AND org_id = ? AND owner_id = ?""",
                (conversation_id, subject.org_id, subject.user_id)).fetchone()
            if row is None:
                return False
            if self.enable_delete_outbox:
                self._db.execute("""DELETE FROM conversation_memory_outbox
                    WHERE org_id = ? AND owner_id = ? AND conversation_id = ?""",
                    (subject.org_id, subject.user_id, conversation_id))
            self._db.execute("DELETE FROM conversation_turns WHERE conversation_id = ?", (conversation_id,))
            self._db.execute("""DELETE FROM conversations
                WHERE id = ? AND org_id = ? AND owner_id = ?""",
                (conversation_id, subject.org_id, subject.user_id))
            self._record_delete_event(subject, conversation_id)
        return True

    def conversation_for_run(self, subject: Subject, workflow_run_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("""SELECT c.id FROM conversation_turns t
                JOIN conversations c ON c.id = t.conversation_id
                WHERE t.workflow_run_id = ? AND c.org_id = ? AND c.owner_id = ?""",
                (workflow_run_id, subject.org_id, subject.user_id)).fetchone()
            return row["id"] if row else None

    def merge(self, subject: Subject, target_id: str, source_id: str) -> bool:
        if target_id == source_id:
            return False
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT id FROM conversations WHERE id IN (?, ?) AND org_id = ? AND owner_id = ?",
                (target_id, source_id, subject.org_id, subject.user_id),
            ).fetchall()
            if {row["id"] for row in rows} != {target_id, source_id}:
                return False
            if self.enable_delete_outbox:
                self._db.execute("""DELETE FROM conversation_memory_outbox
                    WHERE org_id = ? AND owner_id = ? AND conversation_id = ?""",
                    (subject.org_id, subject.user_id, source_id))
            self._db.execute("UPDATE conversation_turns SET conversation_id = ? WHERE conversation_id = ?",
                             (target_id, source_id))
            self._db.execute("DELETE FROM conversations WHERE id = ?", (source_id,))
            self._record_delete_event(subject, source_id)
        return True

    def _require_delete_outbox(self) -> None:
        if not self.enable_delete_outbox:
            raise RuntimeError("CONVERSATION_DELETE_OUTBOX_DISABLED")

    def claim_delete_event(self) -> dict | None:
        """Claim one deletion for a relay; unacknowledged claims can be retried."""
        self._require_delete_outbox()
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="microseconds")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("""DELETE FROM conversation_delete_outbox
                    WHERE delivered_at IS NOT NULL AND delivered_at < ?""",
                    ((now - timedelta(days=7)).isoformat(timespec="microseconds"),))
                self._db.execute("""UPDATE conversation_delete_outbox
                    SET lease_token = NULL, lease_until = NULL
                    WHERE delivered_at IS NULL AND lease_until <= ?""", (now_text,))
                row = self._db.execute("""SELECT id, org_id, owner_id, conversation_id,
                    attempts FROM conversation_delete_outbox
                    WHERE delivered_at IS NULL AND lease_token IS NULL AND available_at <= ?
                    ORDER BY available_at, created_at, id LIMIT 1""", (now_text,)).fetchone()
                if row is None:
                    self._db.commit()
                    return None
                token = uuid4().hex
                self._db.execute("""UPDATE conversation_delete_outbox SET
                    lease_token = ?, lease_until = ?, attempts = attempts + 1 WHERE id = ?""",
                    (token, (now + timedelta(seconds=90)).isoformat(timespec="microseconds"),
                     row["id"]))
                self._db.commit()
                return {**dict(row), "attempts": row["attempts"] + 1, "lease_token": token}
            except Exception:
                self._db.rollback()
                raise

    def ack_delete_event(self, event_id: str, lease_token: str) -> bool:
        self._require_delete_outbox()
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._lock, self._db:
            result = self._db.execute("""UPDATE conversation_delete_outbox SET
                delivered_at = ?, lease_token = NULL, lease_until = NULL,
                last_error_code = NULL WHERE id = ? AND lease_token = ?
                AND delivered_at IS NULL AND lease_until > ?""",
                (now, event_id, lease_token, now))
            return result.rowcount == 1

    def retry_delete_event(self, event_id: str, lease_token: str,
                           reason_code: str) -> bool:
        self._require_delete_outbox()
        if reason_code not in ("target_unavailable", "target_error"):
            raise ValueError("INVALID_DELETE_OUTBOX_REASON")
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="microseconds")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("""SELECT attempts FROM conversation_delete_outbox
                    WHERE id = ? AND lease_token = ? AND delivered_at IS NULL
                    AND lease_until > ?""", (event_id, lease_token, now_text)).fetchone()
                if row is None:
                    self._db.commit()
                    return False
                delay = min(300, 2 ** min(row["attempts"], 9))
                result = self._db.execute("""UPDATE conversation_delete_outbox SET
                    available_at = ?, lease_token = NULL, lease_until = NULL,
                    last_error_code = ? WHERE id = ? AND lease_token = ?
                    AND delivered_at IS NULL AND lease_until > ?""",
                    ((now + timedelta(seconds=delay)).isoformat(timespec="microseconds"),
                     reason_code, event_id, lease_token, now_text))
                self._db.commit()
                return result.rowcount == 1
            except Exception:
                self._db.rollback()
                raise

    def pending_delete_count(self) -> int:
        self._require_delete_outbox()
        with self._lock:
            return self._db.execute("""SELECT count(*) FROM conversation_delete_outbox
                WHERE delivered_at IS NULL""").fetchone()[0]

    def claim_memory_event(self) -> dict | None:
        """Lease a committed turn; the outbox stores only its turn ID."""
        self._require_delete_outbox()
        from app.api.memory_suggestions import eligible_auto_memory_text

        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="microseconds")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("""DELETE FROM conversation_memory_outbox
                    WHERE delivered_at IS NOT NULL AND delivered_at < ?""",
                    ((now - timedelta(days=7)).isoformat(timespec="microseconds"),))
                self._db.execute("""UPDATE conversation_memory_outbox SET
                    lease_token = NULL, lease_until = NULL
                    WHERE delivered_at IS NULL AND lease_until <= ?""", (now_text,))
                row = self._db.execute("""SELECT id, org_id, owner_id, conversation_id,
                    turn_id, include_ordinary, attempts FROM conversation_memory_outbox
                    WHERE delivered_at IS NULL AND lease_token IS NULL AND available_at <= ?
                    ORDER BY available_at, created_at, id LIMIT 1""", (now_text,)).fetchone()
                if row is None:
                    self._db.commit()
                    return None
                token = uuid4().hex
                self._db.execute("""UPDATE conversation_memory_outbox SET
                    lease_token = ?, lease_until = ?, attempts = attempts + 1 WHERE id = ?""",
                    (token, (now + timedelta(seconds=90)).isoformat(timespec="microseconds"),
                     row["id"]))
                turn = self._db.execute("""SELECT t.question FROM conversation_turns t
                    JOIN conversations c ON c.id = t.conversation_id
                    WHERE t.id = ? AND t.conversation_id = ?
                      AND c.org_id = ? AND c.owner_id = ?""",
                    (row["turn_id"], row["conversation_id"],
                     row["org_id"], row["owner_id"])).fetchone()
                context = []
                if turn and row["include_ordinary"]:
                    prior = self._db.execute("""SELECT question FROM conversation_turns
                        WHERE conversation_id = ? AND id < ? AND mode = 'general'
                        ORDER BY id DESC""",
                        (row["conversation_id"], row["turn_id"]))
                    for item in prior:
                        if eligible_auto_memory_text(item["question"]):
                            context.append(item["question"])
                            if len(context) == 3:
                                break
                    context.reverse()
                self._db.commit()
                return {**dict(row), "question": turn["question"] if turn else None,
                        "context": tuple(context), "lease_token": token}
            except Exception:
                self._db.rollback()
                raise

    def ack_memory_event(self, event_id: str, lease_token: str) -> bool:
        self._require_delete_outbox()
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._lock, self._db:
            result = self._db.execute("""UPDATE conversation_memory_outbox SET
                delivered_at = ?, lease_token = NULL, lease_until = NULL,
                last_error_code = NULL WHERE id = ? AND lease_token = ?
                AND delivered_at IS NULL AND lease_until > ?""",
                (now, event_id, lease_token, now))
            return result.rowcount == 1

    def retry_memory_event(self, event_id: str, lease_token: str,
                           reason_code: str) -> bool:
        self._require_delete_outbox()
        if reason_code not in ("target_unavailable", "target_error"):
            raise ValueError("INVALID_MEMORY_OUTBOX_REASON")
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="microseconds")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("""SELECT attempts FROM conversation_memory_outbox
                    WHERE id = ? AND lease_token = ? AND delivered_at IS NULL
                      AND lease_until > ?""", (event_id, lease_token, now_text)).fetchone()
                if row is None:
                    self._db.commit()
                    return False
                delay = min(300, 2 ** min(row["attempts"], 9))
                result = self._db.execute("""UPDATE conversation_memory_outbox SET
                    available_at = ?, lease_token = NULL, lease_until = NULL,
                    last_error_code = ? WHERE id = ? AND lease_token = ?
                      AND delivered_at IS NULL AND lease_until > ?""",
                    ((now + timedelta(seconds=delay)).isoformat(timespec="microseconds"),
                     reason_code, event_id, lease_token, now_text))
                self._db.commit()
                return result.rowcount == 1
            except Exception:
                self._db.rollback()
                raise

    def pending_memory_event_count(self) -> int:
        self._require_delete_outbox()
        with self._lock:
            return self._db.execute("""SELECT count(*) FROM conversation_memory_outbox
                WHERE delivered_at IS NULL""").fetchone()[0]

    def delete_outbox_metrics(self) -> dict:
        """Aggregate monitoring only: no account IDs or conversation content."""
        self._require_delete_outbox()
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="microseconds")
        hour_ago = (now - timedelta(hours=1)).isoformat(timespec="microseconds")
        with self._lock:
            row = self._db.execute("""SELECT
                count(*) FILTER (WHERE delivered_at IS NULL) AS pending,
                count(*) FILTER (WHERE delivered_at IS NULL AND attempts > 1) AS retried,
                count(*) FILTER (WHERE delivered_at IS NULL AND last_error_code IS NOT NULL)
                    AS failed_pending,
                count(*) FILTER (WHERE delivered_at IS NULL AND lease_token IS NOT NULL
                    AND lease_until > ?) AS in_flight,
                count(*) FILTER (WHERE delivered_at >= ?) AS delivered_last_hour,
                min(CASE WHEN delivered_at IS NULL THEN created_at END) AS oldest
                FROM conversation_delete_outbox""", (now_text, hour_ago)).fetchone()
        oldest = row["oldest"]
        return {"pending": row["pending"], "retried": row["retried"],
                "failed_pending": row["failed_pending"],
                "in_flight": row["in_flight"],
                "delivered_last_hour": row["delivered_last_hour"],
                "oldest_pending_seconds": max(0.0, (
                    now - datetime.fromisoformat(oldest)).total_seconds()) if oldest else None}

    def get(self, subject: Subject, conversation_id: str, documents: DocumentRepository,
            artifacts=None) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM conversations WHERE id = ? AND org_id = ? AND owner_id = ?",
                                   (conversation_id, subject.org_id, subject.user_id)).fetchone()
            if row is None:
                return None
            turns = self._db.execute("SELECT question, mode, answer_json FROM conversation_turns WHERE conversation_id = ? ORDER BY id LIMIT 100",
                                     (conversation_id,)).fetchall()
        return render_conversation(row, turns, subject, documents, artifacts)

    def close(self) -> None:
        self._db.close()


def render_conversation(row, turns, subject: Subject, documents: DocumentRepository,
                        artifacts=None) -> dict:
    """Apply the same read-time authorization to either conversation store."""
    result = []
    for turn in turns:
        answer = json.loads(turn["answer_json"])
        if turn["mode"] == "artifact_edit":
            proposal = (artifacts.get_edit_proposal(subject, answer.get("proposal_id", ""), documents)
                        if artifacts is not None else None)
            answer = ({"status": "edit_proposed", "paragraphs": ["请核对修改内容后确认。"],
                       "citations": [], "edit": proposal} if proposal is not None and proposal["status"] != "stale" else
                      {"status": "source_unavailable", "paragraphs": ["文件权限或来源版本已变化，修改提案不可查看。"],
                       "citations": [], "edit": None})
            result.append({"question": turn["question"], "mode": turn["mode"], "answer": answer})
            continue
        citations = answer.get("citations", [])
        target_artifact_id = answer.get("target_artifact_id")
        target_artifact = (artifacts.get(subject, target_artifact_id, documents)
                           if target_artifact_id and artifacts is not None else None)
        target_document_id = answer.get("target_document_id")
        target_document = (documents.get_visible(subject, target_document_id)
                           if target_document_id else None)
        if (target_artifact_id and (target_artifact is None or target_artifact["source_status"] != "current"
                or target_artifact["draft_token"] != answer.get("target_artifact_token"))
                or (target_document_id and (target_document is None
                    or target_document.version != answer.get("target_document_version")))
                or any((source := documents.get_visible(subject, item["document_id"])) is None
               or source.version != item["version"] for item in citations)):
            answer = {"status": "source_unavailable", "paragraphs": ["来源资料权限或版本已变化，历史回答不可查看。"], "citations": []}
        result.append({"question": turn["question"], "mode": turn["mode"], "answer": answer})
    return {"id": row["id"], "title": row["title"], "updated_at": row["updated_at"], "turns": result}
