"""Owner-bound local checkpoints for resumable knowledge workflows."""

import sqlite3
import threading
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver

from app.api.documents import DocumentRepository
from app.api.workflow_answers import KnowledgePlanner, SubjectSearch
from app.security.policy import Subject
from app.workflow.graph import Evidence, WorkResult, build_workflow


class WorkflowRunRepository:
    def __init__(self, path: str | Path):
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
            self._db.execute("""CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                question TEXT NOT NULL, area TEXT, status TEXT NOT NULL,
                conversation_id TEXT, display_question TEXT
            )""")
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(workflow_runs)")}
            if "conversation_id" not in columns:
                self._db.execute("ALTER TABLE workflow_runs ADD COLUMN conversation_id TEXT")
            if "display_question" not in columns:
                self._db.execute("ALTER TABLE workflow_runs ADD COLUMN display_question TEXT")
            self._db.execute("CREATE INDEX IF NOT EXISTS workflow_runs_owner ON workflow_runs(org_id, owner_id)")

    def _owned(self, subject: Subject, run_id: str):
        return self._db.execute(
            "SELECT * FROM workflow_runs WHERE id = ? AND org_id = ? AND owner_id = ?",
            (run_id, subject.org_id, subject.user_id),
        ).fetchone()

    def _saver(self):
        return (nullcontext(self._memory_checkpoint) if self._memory
                else SqliteSaver.from_conn_string(self._checkpoint_path))

    @staticmethod
    def _graph(saver: SqliteSaver, subject: Subject, documents: DocumentRepository, area: str | None):
        return build_workflow(
            KnowledgePlanner(), SubjectSearch(documents, subject, area),
            checkpointer=saver, interrupt_before=["validate"],
            validate_evidence=lambda item: (
                (record := documents.get_visible(subject, item.document_id)) is not None
                and record.version == item.version_id
            ),
        )

    def create(self, subject: Subject, documents: DocumentRepository, question: str,
               area: str | None = None, conversation_id: str | None = None,
               display_question: str | None = None) -> dict:
        run_id = uuid4().hex
        config = {"configurable": {"thread_id": run_id}}
        with self._lock:
            with self._db:
                self._db.execute("""INSERT INTO workflow_runs
                    (id, org_id, owner_id, question, area, status, conversation_id, display_question)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, subject.org_id, subject.user_id, question, area, "pending",
                     conversation_id, display_question or question))
            try:
                with self._saver() as saver:
                    graph = self._graph(saver, subject, documents, area)
                    graph.invoke({"question": question, "steps": 0}, config)
                    if graph.get_state(config).next != ("validate",):
                        raise RuntimeError("workflow did not pause before validation")
            except Exception:
                with self._db:
                    self._db.execute("UPDATE workflow_runs SET status = 'failed' WHERE id = ?", (run_id,))
                raise
        return {"id": run_id, "status": "pending"}

    def get(self, subject: Subject, run_id: str) -> dict | None:
        with self._lock:
            row = self._owned(subject, run_id)
            return {"id": row["id"], "status": row["status"]} if row else None

    def matches(self, subject: Subject, run_id: str, question: str, area: str | None,
                conversation_id: str | None) -> bool:
        with self._lock:
            row = self._owned(subject, run_id)
            return (row is not None and (row["display_question"] or row["question"]) == question and row["area"] == area
                    and row["conversation_id"] == conversation_id)

    def question(self, subject: Subject, run_id: str) -> str | None:
        with self._lock:
            row = self._owned(subject, run_id)
            return row["question"] if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
            if self._memory_checkpoint:
                self._memory_checkpoint.conn.close()

    def resume(self, subject: Subject, documents: DocumentRepository, run_id: str) -> WorkResult | None:
        config = {"configurable": {"thread_id": run_id}}
        with self._lock:
            row = self._owned(subject, run_id)
            if row is None:
                return None
            if row["status"] == "failed":
                raise RuntimeError("workflow failed before recovery")
            with self._saver() as saver:
                graph = self._graph(saver, subject, documents, row["area"])
                snapshot = graph.get_state(config)
                if not snapshot.values:
                    graph.invoke({"question": row["question"], "steps": 0}, config)
                    snapshot = graph.get_state(config)
                elif snapshot.next and snapshot.next != ("validate",):
                    graph.invoke(None, config)
                    snapshot = graph.get_state(config)
                if snapshot.next == ("validate",):
                    state = graph.invoke(None, config)
                elif snapshot.next == () and "result" in snapshot.values:
                    state = snapshot.values
                else:
                    raise RuntimeError("invalid workflow checkpoint")
            result = WorkResult.model_validate(state["result"])
            if result.status == "evidence_ready":
                evidence = [item for item in result.evidence if self._current(subject, documents, item)]
                result = (result.model_copy(update={"evidence": evidence}) if evidence else
                          WorkResult(status="no_evidence", text="未找到可靠的授权资料，无法回答或生成草稿。"))
            with self._db:
                self._db.execute("UPDATE workflow_runs SET status = ? WHERE id = ?",
                                 ("failed" if result.status == "failed" else "completed", run_id))
            return result

    @staticmethod
    def _current(subject: Subject, documents: DocumentRepository, evidence: Evidence) -> bool:
        record = documents.get_visible(subject, evidence.document_id)
        return record is not None and record.version == evidence.version_id
