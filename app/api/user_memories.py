"""Explicit, owner-scoped profile memories for the web application."""

import sqlite3
import threading
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.security.policy import Subject


MEMORY_KINDS = ("role", "task", "answer_preference")
MEMORY_JOB_LEASE = timedelta(seconds=90)
MAX_MEMORY_JOB_ATTEMPTS = 3
MEMORY_FAILURE_RETENTION = timedelta(days=7)


def _task_topic(value: str) -> str:
    return re.sub(r"^(?:我)?(?:目前|现在|长期|一直|主要)?(?:在)?(?:负责|跟进|参与)",
                  "", value).strip(" ：:，,。 ")


def _task_continues(text: str) -> bool:
    return bool(re.search(r"我(?:的)?(?:工作|任务).{0,8}(?:继续|仍|还)|"
                          r"(?:继续|仍|还).{0,8}(?:由我|我).{0,8}(?:负责|跟进|工作)|"
                          r"(?:我|由我).{0,8}(?:继续|仍|还).{0,8}(?:负责|跟进)", text))


class UserMemoryRepository:
    def __init__(self, path: str | Path, *, default_auto_suggest: bool = False):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.default_auto_suggest = default_auto_suggest
        with self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS user_memories (
                org_id TEXT NOT NULL, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
                value TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (org_id, owner_id, kind)
            )""")
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(user_memories)")}
            if "origin" not in columns:
                self._db.execute("ALTER TABLE user_memories ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
            if "source_conversation_id" not in columns:
                self._db.execute("ALTER TABLE user_memories ADD COLUMN source_conversation_id TEXT")
            if "expires_at" not in columns:
                self._db.execute("ALTER TABLE user_memories ADD COLUMN expires_at TEXT")
            self._db.execute("""CREATE TABLE IF NOT EXISTS memory_settings (
                org_id TEXT NOT NULL, owner_id TEXT NOT NULL, auto_suggest INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (org_id, owner_id)
            )""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS memory_suggestions (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                kind TEXT NOT NULL, value TEXT NOT NULL, source_conversation_id TEXT NOT NULL,
                source_text TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE (org_id, owner_id, kind, value)
            )""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS memory_suppression (
                org_id TEXT NOT NULL, owner_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                PRIMARY KEY (org_id, owner_id, fingerprint)
            )""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS memory_suggestion_jobs (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                source_conversation_id TEXT NOT NULL, question TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('queued', 'running')),
                created_at TEXT NOT NULL
            )""")
            job_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(memory_suggestion_jobs)")}
            if "context_json" not in job_columns:
                self._db.execute("ALTER TABLE memory_suggestion_jobs ADD COLUMN context_json TEXT NOT NULL DEFAULT '[]'")
            if "lease_token" not in job_columns:
                self._db.execute("ALTER TABLE memory_suggestion_jobs ADD COLUMN lease_token TEXT")
            if "lease_until" not in job_columns:
                self._db.execute("ALTER TABLE memory_suggestion_jobs ADD COLUMN lease_until TEXT")
            if "attempts" not in job_columns:
                self._db.execute("ALTER TABLE memory_suggestion_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            if "available_at" not in job_columns:
                self._db.execute("ALTER TABLE memory_suggestion_jobs ADD COLUMN available_at TEXT NOT NULL DEFAULT ''")
                self._db.execute("UPDATE memory_suggestion_jobs SET available_at = created_at WHERE available_at = ''")
            self._db.execute("""CREATE TABLE IF NOT EXISTS memory_suggestion_failures (
                job_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                source_conversation_id TEXT NOT NULL, reason_code TEXT NOT NULL,
                attempts INTEGER NOT NULL, failed_at TEXT NOT NULL
            )""")
            self._db.execute("DROP INDEX IF EXISTS memory_jobs_ready")
            self._db.execute("""CREATE INDEX memory_jobs_ready
                ON memory_suggestion_jobs(status, available_at, created_at)""")

    @staticmethod
    def _fingerprint(kind: str, value: str) -> str:
        return hashlib.sha256(f"{kind}\0{value.strip().casefold()}".encode()).hexdigest()

    def auto_suggest_enabled(self, subject: Subject) -> bool:
        with self._lock:
            row = self._db.execute("""SELECT auto_suggest FROM memory_settings
                WHERE org_id = ? AND owner_id = ?""", (subject.org_id, subject.user_id)).fetchone()
        return bool(row["auto_suggest"]) if row else self.default_auto_suggest

    def set_auto_suggest(self, subject: Subject, enabled: bool) -> None:
        with self._lock, self._db:
            self._db.execute("""INSERT INTO memory_settings (org_id, owner_id, auto_suggest)
                VALUES (?, ?, ?) ON CONFLICT (org_id, owner_id) DO UPDATE SET
                auto_suggest = excluded.auto_suggest""", (subject.org_id, subject.user_id, int(enabled)))
            if not enabled:
                self._db.execute("DELETE FROM memory_suggestions WHERE org_id = ? AND owner_id = ?",
                                 (subject.org_id, subject.user_id))
                self._db.execute("DELETE FROM memory_suggestion_jobs WHERE org_id = ? AND owner_id = ?",
                                 (subject.org_id, subject.user_id))
                self._db.execute("DELETE FROM memory_suggestion_failures WHERE org_id = ? AND owner_id = ?",
                                 (subject.org_id, subject.user_id))

    def enqueue_suggestion(self, subject: Subject, question: str, conversation_id: str,
                           *, include_ordinary: bool = False,
                           context: tuple[str, ...] = ()) -> bool:
        """Persist a short-lived job after a successful ordinary conversation turn."""
        from app.api.memory_suggestions import eligible_auto_memory_text, eligible_user_text

        if not (eligible_auto_memory_text(question) if include_ordinary else eligible_user_text(question)):
            return False
        safe_context = tuple(item[:500] for item in context[-3:]
                             if isinstance(item, str) and eligible_auto_memory_text(item)) if include_ordinary else ()
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._lock, self._db:
            if not self.auto_suggest_enabled(subject):
                return False
            total = self._db.execute("""SELECT COUNT(*) FROM memory_suggestion_jobs
                WHERE org_id = ? AND owner_id = ?""", (subject.org_id, subject.user_id)).fetchone()[0]
            if total >= 30:
                return False
            self._db.execute("""INSERT INTO memory_suggestion_jobs
                (id, org_id, owner_id, source_conversation_id, question, status,
                 created_at, available_at, context_json)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                (uuid4().hex, subject.org_id, subject.user_id, conversation_id, question,
                 now, now, json.dumps(safe_context, ensure_ascii=False)))
        return True

    def _recover_stale_jobs_locked(self, now: datetime) -> None:
        cutoff = (now - timedelta(hours=24)).isoformat(timespec="microseconds")
        now_text = now.isoformat(timespec="microseconds")
        failure_cutoff = (now - MEMORY_FAILURE_RETENTION).isoformat(timespec="microseconds")
        self._db.execute("DELETE FROM memory_suggestion_failures WHERE failed_at < ?", (failure_cutoff,))
        self._db.execute("""INSERT OR IGNORE INTO memory_suggestion_failures
            SELECT id, org_id, owner_id, source_conversation_id, 'job_expired', attempts, ?
            FROM memory_suggestion_jobs WHERE created_at < ?""", (now_text, cutoff))
        self._db.execute("DELETE FROM memory_suggestion_jobs WHERE created_at < ?", (cutoff,))
        self._db.execute("""INSERT OR IGNORE INTO memory_suggestion_failures
            SELECT id, org_id, owner_id, source_conversation_id, 'lease_exhausted', attempts, ?
            FROM memory_suggestion_jobs WHERE status = 'running'
            AND (lease_until IS NULL OR lease_until <= ?) AND attempts >= ?""",
            (now_text, now_text, MAX_MEMORY_JOB_ATTEMPTS))
        self._db.execute("""DELETE FROM memory_suggestion_jobs WHERE status = 'running'
            AND (lease_until IS NULL OR lease_until <= ?) AND attempts >= ?""",
            (now_text, MAX_MEMORY_JOB_ATTEMPTS))
        self._db.execute("""UPDATE memory_suggestion_jobs SET status = 'queued',
            lease_token = NULL, lease_until = NULL, available_at = ? WHERE status = 'running'
            AND (lease_until IS NULL OR lease_until <= ?)""", (now_text, now_text))

    def recover_suggestion_jobs(self) -> None:
        """Recover only expired claims; another live process keeps its lease."""
        with self._lock, self._db:
            self._recover_stale_jobs_locked(datetime.now(timezone.utc))

    def claim_suggestion_job(self) -> dict | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            # A write lock precedes selection, so two SQLite connections cannot claim the same row.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._recover_stale_jobs_locked(now)
                row = self._db.execute("""SELECT id, org_id, owner_id, source_conversation_id,
                    question, context_json, attempts FROM memory_suggestion_jobs
                    WHERE status = 'queued' AND available_at <= ?
                    ORDER BY available_at, created_at, rowid LIMIT 1""",
                    (now.isoformat(timespec="microseconds"),)).fetchone()
                if row is None:
                    self._db.commit()
                    return None
                token = uuid4().hex
                lease_until = (now + MEMORY_JOB_LEASE).isoformat(timespec="microseconds")
                self._db.execute("""UPDATE memory_suggestion_jobs SET status = 'running',
                    lease_token = ?, lease_until = ?, attempts = attempts + 1 WHERE id = ?""",
                    (token, lease_until, row["id"]))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        job = dict(row)
        job["context"] = tuple(json.loads(job.pop("context_json")))
        job["lease_token"] = token
        job["lease_until"] = lease_until
        job["attempts"] += 1
        return job

    def next_suggestion_lease_delay(self) -> float | None:
        with self._lock:
            row = self._db.execute("""SELECT MIN(next_at) FROM (
                SELECT lease_until AS next_at FROM memory_suggestion_jobs
                    WHERE status = 'running' AND lease_until IS NOT NULL
                UNION ALL
                SELECT available_at AS next_at FROM memory_suggestion_jobs
                    WHERE status = 'queued')""").fetchone()
        if row[0] is None:
            return None
        remaining = (datetime.fromisoformat(row[0]) - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, remaining)

    def job_lease_current(self, job_id: str, lease_token: str) -> bool:
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._lock:
            return self._job_lease_current_locked(job_id, lease_token, now)

    def _job_lease_current_locked(self, job_id: str, lease_token: str, now: str) -> bool:
        return self._db.execute("""SELECT 1 FROM memory_suggestion_jobs
            WHERE id = ? AND status = 'running' AND lease_token = ? AND lease_until > ?""",
            (job_id, lease_token, now)).fetchone() is not None

    def finish_suggestion_job(self, job_id: str, lease_token: str) -> None:
        # An expired worker cannot remove a job claimed by a newer worker.
        with self._lock, self._db:
            self._db.execute("""DELETE FROM memory_suggestion_jobs
                WHERE id = ? AND lease_token = ?""", (job_id, lease_token))

    def fail_suggestion_job(self, job_id: str, lease_token: str,
                            reason_code: str, *, retryable: bool) -> None:
        """Retry transient extraction failures; retain only metadata when exhausted."""
        if reason_code not in ("model_timeout", "model_unavailable", "model_rate_limited",
                               "model_auth_failed", "model_invalid", "memory_write_failed"):
            raise ValueError("INVALID_MEMORY_FAILURE_CODE")
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="microseconds")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("""SELECT org_id, owner_id, source_conversation_id, attempts
                    FROM memory_suggestion_jobs
                    WHERE id = ? AND lease_token = ? AND status = 'running' AND lease_until > ?""",
                    (job_id, lease_token, now_text)).fetchone()
                if row is None:
                    self._db.commit()
                    return
                if retryable and row["attempts"] < MAX_MEMORY_JOB_ATTEMPTS:
                    available_at = (now + timedelta(seconds=2 ** row["attempts"])).isoformat(
                        timespec="microseconds")
                    self._db.execute("""UPDATE memory_suggestion_jobs SET status = 'queued',
                        lease_token = NULL, lease_until = NULL, available_at = ? WHERE id = ?""",
                        (available_at, job_id))
                else:
                    self._db.execute("""INSERT OR IGNORE INTO memory_suggestion_failures
                        (job_id, org_id, owner_id, source_conversation_id,
                         reason_code, attempts, failed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (job_id, row["org_id"], row["owner_id"], row["source_conversation_id"], reason_code,
                         row["attempts"], now_text))
                    self._db.execute("DELETE FROM memory_suggestion_jobs WHERE id = ?", (job_id,))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def list_suggestion_failures(self, subject: Subject) -> list[dict]:
        """Diagnostic metadata only; never retain a failed job's question or context."""
        with self._lock:
            rows = self._db.execute("""SELECT job_id, reason_code, attempts, failed_at
                FROM memory_suggestion_failures WHERE org_id = ? AND owner_id = ?
                ORDER BY failed_at DESC LIMIT 30""", (subject.org_id, subject.user_id)).fetchall()
        return [dict(row) for row in rows]

    def suggestion_job_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM memory_suggestion_jobs").fetchone()[0]

    def list_suggestions(self, subject: Subject) -> list[dict]:
        with self._lock:
            rows = self._db.execute("""SELECT id, kind, value, source_conversation_id,
                source_text, created_at FROM memory_suggestions WHERE org_id = ? AND owner_id = ?
                ORDER BY created_at DESC LIMIT 30""", (subject.org_id, subject.user_id)).fetchall()
        return [dict(row) for row in rows]

    def discard_conversation_suggestions(self, subject: Subject, conversation_id: str) -> None:
        """Remove pending and automatic facts whose only recorded source was deleted."""
        with self._lock, self._db:
            params = (subject.org_id, subject.user_id, conversation_id)
            self._db.execute("""DELETE FROM memory_suggestions WHERE org_id = ? AND owner_id = ?
                AND source_conversation_id = ?""", params)
            self._db.execute("""DELETE FROM memory_suggestion_jobs WHERE org_id = ? AND owner_id = ?
                AND source_conversation_id = ?""", params)
            self._db.execute("""DELETE FROM memory_suggestion_failures WHERE org_id = ? AND owner_id = ?
                AND source_conversation_id = ?""", params)
            self._db.execute("""DELETE FROM user_memories WHERE org_id = ? AND owner_id = ?
                AND source_conversation_id = ? AND origin = 'auto'""", params)

    def propose(self, subject: Subject, kind: str, value: str, conversation_id: str,
                source_text: str, *, job_lease: tuple[str, str] | None = None) -> None:
        from uuid import uuid4

        if (kind not in MEMORY_KINDS or not value or len(value) > 160
                or value not in source_text
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return
        excerpt_start = max(0, source_text.find(value) - 40)
        excerpt = source_text[excerpt_start:excerpt_start + 220]
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._lock, self._db:
            if job_lease:
                self._db.execute("BEGIN IMMEDIATE")
                if not self._job_lease_current_locked(*job_lease, now):
                    return
            if not self.auto_suggest_enabled(subject):
                return
            if self._db.execute("""SELECT 1 FROM user_memories WHERE org_id = ? AND owner_id = ?
                AND kind = ? AND value = ?""", (subject.org_id, subject.user_id, kind, value)).fetchone():
                return
            if self._db.execute("""SELECT 1 FROM memory_suppression WHERE org_id = ?
                AND owner_id = ? AND fingerprint = ?""", (subject.org_id, subject.user_id,
                    self._fingerprint(kind, value))).fetchone():
                return
            if self._db.execute("""SELECT COUNT(*) AS total FROM memory_suggestions
                WHERE org_id = ? AND owner_id = ?""", (subject.org_id, subject.user_id)).fetchone()["total"] >= 30:
                return
            self._db.execute("""INSERT OR IGNORE INTO memory_suggestions
                (id, org_id, owner_id, kind, value, source_conversation_id, source_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (uuid4().hex, subject.org_id, subject.user_id, kind, value,
                 conversation_id, excerpt, now))

    def reject_suggestion(self, subject: Subject, suggestion_id: str) -> bool:
        with self._lock, self._db:
            row = self._db.execute("""SELECT kind, value FROM memory_suggestions
                WHERE id = ? AND org_id = ? AND owner_id = ?""",
                (suggestion_id, subject.org_id, subject.user_id)).fetchone()
            if row is None:
                return False
            self._db.execute("""INSERT OR IGNORE INTO memory_suppression VALUES (?, ?, ?)""",
                             (subject.org_id, subject.user_id,
                              self._fingerprint(row["kind"], row["value"])))
            self._db.execute("DELETE FROM memory_suggestions WHERE id = ?", (suggestion_id,))
        return True

    def accept_suggestion(self, subject: Subject, suggestion_id: str, value: str) -> dict | None:
        with self._lock, self._db:
            row = self._db.execute("""SELECT kind, value FROM memory_suggestions
                WHERE id = ? AND org_id = ? AND owner_id = ?""",
                (suggestion_id, subject.org_id, subject.user_id)).fetchone()
            if row is None:
                return None
            self._db.execute("INSERT OR IGNORE INTO memory_suppression VALUES (?, ?, ?)",
                             (subject.org_id, subject.user_id,
                              self._fingerprint(row["kind"], row["value"])))
            item = self.upsert(subject, row["kind"], value)
            self._db.execute("DELETE FROM memory_suggestions WHERE id = ?", (suggestion_id,))
        return item

    def list_for(self, subject: Subject) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._lock:
            rows = self._db.execute("""SELECT kind, value, created_at, updated_at,
                origin, source_conversation_id, expires_at
                FROM user_memories WHERE org_id = ? AND owner_id = ?
                AND (expires_at IS NULL OR expires_at > ?) ORDER BY kind""",
                (subject.org_id, subject.user_id, now)).fetchall()
        return [dict(row) for row in rows]

    def upsert_auto(self, subject: Subject, kind: str, value: str,
                    conversation_id: str, source_text: str,
                    *, job_lease: tuple[str, str] | None = None) -> dict | None:
        """Save a validated user quote; never silently replace a manually edited memory."""
        from app.api.memory_suggestions import eligible_auto_memory_text

        if (kind not in MEMORY_KINDS or not isinstance(value, str) or not value.strip()
                or len(value) > 160 or value not in source_text
                or not eligible_auto_memory_text(source_text)
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return None
        value = value.strip()
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="microseconds")
        lifetime = {"role": timedelta(days=180), "task": timedelta(days=30),
                    "answer_preference": timedelta(days=365)}[kind]
        expires_at = (now + lifetime).isoformat(timespec="microseconds")
        with self._lock, self._db:
            if job_lease:
                self._db.execute("BEGIN IMMEDIATE")
                if not self._job_lease_current_locked(*job_lease, now_text):
                    return None
            if not self.auto_suggest_enabled(subject):
                return None
            if self._db.execute("""SELECT 1 FROM memory_suppression WHERE org_id = ?
                AND owner_id = ? AND fingerprint = ?""", (subject.org_id, subject.user_id,
                self._fingerprint(kind, value))).fetchone():
                return None
            existing = self._db.execute("""SELECT origin, value FROM user_memories
                WHERE org_id = ? AND owner_id = ? AND kind = ?""",
                (subject.org_id, subject.user_id, kind)).fetchone()
            if existing and existing["origin"] == "manual":
                return None
            if (kind == "task" and existing and existing["origin"] == "auto"
                    and (topic := _task_topic(existing["value"]))
                    and topic in source_text and _task_continues(source_text)):
                # A finished phase does not supersede the ongoing overall task.
                return None
            self._db.execute("""INSERT INTO user_memories
                (org_id, owner_id, kind, value, created_at, updated_at, origin,
                 source_conversation_id, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'auto', ?, ?)
                ON CONFLICT (org_id, owner_id, kind) DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at,
                origin = 'auto', source_conversation_id = excluded.source_conversation_id,
                expires_at = excluded.expires_at""",
                (subject.org_id, subject.user_id, kind, value, now_text, now_text,
                 conversation_id, expires_at))
            row = self._db.execute("""SELECT kind, value, created_at, updated_at
                FROM user_memories WHERE org_id = ? AND owner_id = ? AND kind = ?""",
                (subject.org_id, subject.user_id, kind)).fetchone()
        return dict(row)

    def forget_auto_from_context(self, subject: Subject, kind: str,
                                 conversation_id: str, context: tuple[str, ...],
                                 *, job_lease: tuple[str, str] | None = None) -> bool:
        """Retract only an automatic fact quoted in prior turns of its source conversation."""
        if kind not in MEMORY_KINDS or not context:
            return False
        with self._lock, self._db:
            if job_lease:
                self._db.execute("BEGIN IMMEDIATE")
                if not self._job_lease_current_locked(
                        *job_lease, datetime.now(timezone.utc).isoformat(timespec="microseconds")):
                    return False
            if not self.auto_suggest_enabled(subject):
                return False
            row = self._db.execute("""SELECT value FROM user_memories
                WHERE org_id = ? AND owner_id = ? AND kind = ? AND origin = 'auto'
                AND source_conversation_id = ?""",
                (subject.org_id, subject.user_id, kind, conversation_id)).fetchone()
            if row is None or not any(row["value"] in item for item in context):
                return False
            self._db.execute("""DELETE FROM user_memories WHERE org_id = ? AND owner_id = ?
                AND kind = ? AND origin = 'auto' AND source_conversation_id = ?""",
                (subject.org_id, subject.user_id, kind, conversation_id))
            return True

    def forget_auto_task_cross_session(self, subject: Subject, expected: dict,
                                       conversation_id: str, latest_text: str, evidence: str,
                                       *, job_lease: tuple[str, str] | None = None) -> bool:
        """Retract one prior automatic task only when the latest text names its subject."""
        if (expected.get("kind") != "task" or expected.get("origin") != "auto"
                or not expected.get("source_conversation_id")
                or expected["source_conversation_id"] == conversation_id
                or not isinstance(evidence, str) or evidence not in latest_text
                or "?" in latest_text or "？" in latest_text
                or not any(term in evidence for term in (
                    "已移交", "已经移交", "不再负责", "不再跟进", "已经结束", "已结束",
                    "结束了", "已经完成", "已完成", "完成了"))):
            return False
        old_value = expected.get("value", "")
        if not isinstance(old_value, str):
            return False
        topic = _task_topic(old_value)
        if (len(topic) < 6 or topic not in latest_text
                or _task_continues(latest_text)
                or (re.search(r"(?:一期|二期|阶段)", latest_text)
                    and not re.search(r"(?:不再负责|不再跟进|已移交|已经移交)", latest_text))):
            return False
        return self._forget_auto_snapshot(subject, expected, job_lease)

    def forget_auto_role_cross_session(self, subject: Subject, expected: dict,
                                       conversation_id: str, latest_text: str, evidence: str,
                                       *, job_lease: tuple[str, str] | None = None) -> bool:
        """Retract a prior automatic role only on an explicit personal negation."""
        old_value = expected.get("value")
        if (expected.get("kind") != "role" or expected.get("origin") != "auto"
                or not expected.get("source_conversation_id")
                or expected["source_conversation_id"] == conversation_id
                or not isinstance(old_value, str) or len(old_value) < 2
                or not isinstance(evidence, str)
                or old_value not in latest_text or evidence not in latest_text
                or "?" in latest_text or "？" in latest_text
                or not re.search(r"我.{0,4}(?:不再做|不再是|不做|已不做|已经不做|"
                                 r"已经不是|离开)", evidence)):
            return False
        return self._forget_auto_snapshot(subject, expected, job_lease)

    def _forget_auto_snapshot(self, subject: Subject, expected: dict,
                              job_lease: tuple[str, str] | None) -> bool:
        """Compare-and-delete the exact active fact observed before model execution."""
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._lock, self._db:
            if job_lease:
                self._db.execute("BEGIN IMMEDIATE")
                if not self._job_lease_current_locked(*job_lease, now):
                    return False
            if not self.auto_suggest_enabled(subject):
                return False
            deleted = self._db.execute("""DELETE FROM user_memories
                WHERE org_id = ? AND owner_id = ? AND kind = ? AND origin = 'auto'
                AND value = ? AND source_conversation_id = ? AND updated_at = ?
                AND (expires_at IS NULL OR expires_at > ?)""",
                (subject.org_id, subject.user_id, expected["kind"], expected["value"],
                 expected["source_conversation_id"], expected.get("updated_at"), now))
            return deleted.rowcount == 1

    def upsert(self, subject: Subject, kind: str, value: str) -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._lock, self._db:
            self._db.execute("""INSERT INTO user_memories
                (org_id, owner_id, kind, value, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (org_id, owner_id, kind) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at,
                    origin = 'manual', source_conversation_id = NULL,
                    expires_at = NULL""",
                (subject.org_id, subject.user_id, kind, value, now, now))
            row = self._db.execute("""SELECT kind, value, created_at, updated_at,
                origin, source_conversation_id, expires_at
                FROM user_memories WHERE org_id = ? AND owner_id = ? AND kind = ?""",
                (subject.org_id, subject.user_id, kind)).fetchone()
        return dict(row)

    def delete(self, subject: Subject, kind: str) -> None:
        with self._lock, self._db:
            row = self._db.execute("""SELECT value FROM user_memories WHERE org_id = ?
                AND owner_id = ? AND kind = ?""", (subject.org_id, subject.user_id, kind)).fetchone()
            if row:
                self._db.execute("INSERT OR IGNORE INTO memory_suppression VALUES (?, ?, ?)",
                                 (subject.org_id, subject.user_id,
                                  self._fingerprint(kind, row["value"])))
            self._db.execute("DELETE FROM user_memories WHERE org_id = ? AND owner_id = ? AND kind = ?",
                             (subject.org_id, subject.user_id, kind))

    def close(self) -> None:
        self._db.close()
