"""Owner-bound checkpoints for the artifact collaboration graph."""

import sqlite3
import threading
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver

from app.api.artifacts import ArtifactRepository, DraftChanged, SourceVersionChanged
from app.api.collaboration import CollaborationInvalid, CollaborationLimits, CollaborationModel, build_collaboration_graph
from app.api.documents import DocumentRepository
from app.security.policy import Subject


class CollaborationRunRepository:
    def __init__(self, path: str | Path, *, limits: CollaborationLimits | None = None):
        self._limits = limits or CollaborationLimits()
        self._memory = path == ":memory:"
        self._path = None if self._memory else Path(path)
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint_path = (str(self._path.with_name(self._path.stem + "_checkpoints.sqlite3"))
                                 if self._path else None)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._memory_checkpoint = (SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
                                   if self._memory else None)
        self._lock = threading.RLock()
        with self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS collaboration_runs (
                id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, org_id TEXT NOT NULL,
                owner_id TEXT NOT NULL, request_id TEXT NOT NULL, draft_token TEXT NOT NULL,
                status TEXT NOT NULL, stage TEXT, error_code TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(artifact_id, request_id)
            )""")
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(collaboration_runs)")}
            if "attempts" not in columns:
                self._db.execute("ALTER TABLE collaboration_runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            self._db.execute("""CREATE INDEX IF NOT EXISTS collaboration_runs_owner
                ON collaboration_runs(org_id, owner_id, artifact_id)""")

    def _saver(self):
        return (nullcontext(self._memory_checkpoint) if self._memory
                else SqliteSaver.from_conn_string(self._checkpoint_path))

    def _latest(self, subject: Subject, artifact_id: str):
        return self._db.execute("""SELECT * FROM collaboration_runs
            WHERE artifact_id = ? AND org_id = ? AND owner_id = ? ORDER BY rowid DESC LIMIT 1""",
            (artifact_id, subject.org_id, subject.user_id)).fetchone()

    def get_latest(self, subject: Subject, artifact_id: str, artifacts: ArtifactRepository,
                   documents: DocumentRepository) -> dict | None:
        item = artifacts.get(subject, artifact_id, documents)
        if item is None or item["source_status"] != "current":
            return None
        with self._lock:
            row = self._latest(subject, artifact_id)
            if row is None or (row["status"] != "completed" and row["draft_token"] != item["draft_token"]):
                return None
            with self._saver() as saver:
                graph = build_collaboration_graph(None, checkpointer=saver)
                snapshot = graph.get_state({"configurable": {"thread_id": row["id"]}})
                trace = snapshot.values.get("trace", []) if snapshot.values else []
            if row["status"] == "completed" and trace != item["collaboration_trace"]:
                return None
            return {"id": row["id"], "request_id": row["request_id"], "draft_token": row["draft_token"],
                    "status": row["status"], "stage": row["stage"],
                    "error_code": row["error_code"], "trace": trace,
                    "attempts": row["attempts"], "max_attempts": self._limits.max_attempts,
                    "can_retry": row["status"] == "failed" and row["attempts"] < self._limits.max_attempts}

    def execute(self, subject: Subject, artifact_id: str, artifacts: ArtifactRepository,
                documents: DocumentRepository, model: CollaborationModel,
                draft_token: str, request_id: str,
                presentation_style: str | None = None) -> dict | None:
        item = artifacts.get(subject, artifact_id, documents)
        if item is None:
            return None
        if item["source_status"] != "current":
            raise SourceVersionChanged()
        if presentation_style in ("concise", "bullets"):
            item = item | {"presentation_style": presentation_style}
        with self._lock:
            row = self._db.execute("""SELECT * FROM collaboration_runs
                WHERE artifact_id = ? AND org_id = ? AND owner_id = ? AND request_id = ?""",
                (artifact_id, subject.org_id, subject.user_id, request_id)).fetchone()
            if row and row["draft_token"] != draft_token:
                raise DraftChanged()
            if row is None:
                if item["status"] != "pending" or item["draft_token"] != draft_token:
                    raise DraftChanged()
                run_id = uuid4().hex
                with self._db:
                    self._db.execute("""INSERT INTO collaboration_runs
                        (id, artifact_id, org_id, owner_id, request_id, draft_token, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                        (run_id, artifact_id, subject.org_id, subject.user_id, request_id, draft_token))
                row = self._latest(subject, artifact_id)
            if row["status"] == "completed":
                return artifacts.get(subject, artifact_id, documents)
            if item["status"] != "pending" or item["draft_token"] != row["draft_token"]:
                raise DraftChanged()
            if row["attempts"] >= self._limits.max_attempts:
                raise CollaborationInvalid("COLLABORATION_RETRY_LIMIT")
            with self._db:
                self._db.execute("UPDATE collaboration_runs SET attempts = attempts + 1, status = 'pending' WHERE id = ?",
                                 (row["id"],))
            config = {"configurable": {"thread_id": row["id"]}}
            with self._saver() as saver:
                graph = build_collaboration_graph(model, checkpointer=saver,
                                                  interrupt_before=["supervisor"], limits=self._limits)
                try:
                    snapshot = graph.get_state(config)
                    if not snapshot.values:
                        graph.invoke({"item": item, "trace": []}, config)
                        snapshot = graph.get_state(config)
                    state = graph.invoke(None, config) if snapshot.next else snapshot.values
                    if "drafts" not in state or "trace" not in state:
                        raise RuntimeError("COLLABORATION_INCOMPLETE")
                    result = artifacts.commit_collaboration(
                        subject, artifact_id, documents, state["drafts"], state["trace"],
                        row["draft_token"], row["request_id"])
                except Exception as exc:
                    snapshot = graph.get_state(config)
                    code = str(exc) if isinstance(exc, CollaborationInvalid) else exc.__class__.__name__
                    stage = snapshot.next[0] if snapshot.next else "commit"
                    with self._db:
                        self._db.execute("""UPDATE collaboration_runs SET status = 'failed',
                            stage = ?, error_code = ? WHERE id = ?""", (stage, code, row["id"]))
                    raise
            with self._db:
                self._db.execute("""UPDATE collaboration_runs SET status = 'completed',
                    stage = 'done', error_code = NULL WHERE id = ?""", (row["id"],))
            return result

    def close(self):
        with self._lock:
            self._db.close()
            if self._memory_checkpoint:
                self._memory_checkpoint.conn.close()
