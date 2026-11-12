"""SQLite-backed synthetic document catalog with authorization at every read."""

import json
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.api.content import search_terms, split_segments
from app.api.search_cache import RedisSearchCache
from app.security.policy import (
    Classification, DocumentPolicy, DocumentState, ResourceAction,
    Subject, Visibility, authorize,
)


@dataclass(frozen=True)
class DocumentRecord:
    document_id: str
    title: str
    file_type: str
    area: str
    version: str
    updated_at: str
    owner: str
    location: str
    excerpt: str
    policy: DocumentPolicy
    has_original: bool = False


@dataclass(frozen=True)
class SearchHit:
    document: DocumentRecord
    location: str
    text: str


class OriginalVersionChanged(Exception):
    pass


_DEMO_ROWS = (
    ("1", "差旅报销管理制度", "PDF", "制度规范", "v2.1", "10月7日", "行政部 · 王敏", "第 3 页 · 审批流程", "员工提交报销申请时应填写报销单，经部门负责人审批后，由财务部门审核。财务审核完成后，费用将在 5 个工作日内到账。", "INTERNAL", "organization", []),
    ("2", "员工年假与休假管理办法", "DOCX", "制度规范", "v1.8", "10月5日", "人力资源部 · 陈瑜", "第 2 页 · 年假标准", "工作满 1 年的员工每年享受 5 天年假；工作满 5 年的员工每年享受 10 天年假。", "INTERNAL", "organization", []),
    ("3", "产品 Q4 迭代路线图", "PPTX", "产品资料", "v3.0", "10月4日", "产品部 · 林晓宇", "第 6 页 · 季度重点", "本季度聚焦知识检索、权限治理和文档生成三项能力。第一阶段完成核心工作台，第二阶段开展部门试点。", "DEPARTMENT", "department", ["product"]),
    ("4", "客户需求汇总与优先级", "DOCX", "产品资料", "v1.2", "9月28日", "产品部 · 周宁", "第 4 页 · 高优先级需求", "高优先级需求包括资料权限分级、结果引用、Word/PPT 模板和生成任务进度。客户期望人工审核生成内容。", "DEPARTMENT", "department", ["product"]),
    ("5", "产品发布检查清单", "PDF", "项目空间", "v1.4", "9月22日", "项目组 · 刘青", "第 1 页 · 发布前检查", "发布前需检查功能验收、安全测试、权限回归、备份恢复与回滚方案。发布审批由项目负责人完成。", "CONFIDENTIAL", "allowlist", []),
    ("6", "项目复盘会议纪要", "DOCX", "项目空间", "v1.0", "9月18日", "项目组 · 王敏", "第 2 页 · 后续行动", "项目复盘包括目标完成情况、主要风险、问题原因、下阶段负责人和预定完成时间。", "DEPARTMENT", "department", ["product"]),
)


class DocumentRepository:
    def __init__(self, path: str | Path, *, seed_demo: bool = False,
                 search_cache: RedisSearchCache | None = None,
                 record_index_events: bool = False):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.search_cache = search_cache
        self.record_index_events = record_index_events
        with self._lock, self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, title TEXT NOT NULL,
                file_type TEXT NOT NULL, area TEXT NOT NULL, version TEXT NOT NULL,
                updated_at TEXT NOT NULL, owner TEXT NOT NULL, location TEXT NOT NULL,
                excerpt TEXT NOT NULL, state TEXT NOT NULL, classification TEXT NOT NULL,
                visibility TEXT NOT NULL, department_ids TEXT NOT NULL,
                allowed_user_ids TEXT NOT NULL, denied_user_ids TEXT NOT NULL
            )""")
            self._db.execute("CREATE INDEX IF NOT EXISTS documents_org ON documents(org_id)")
            self._db.execute("""CREATE TABLE IF NOT EXISTS document_files (
                document_id TEXT PRIMARY KEY, filename TEXT NOT NULL,
                content BLOB NOT NULL, extracted_text TEXT NOT NULL
            )""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS document_revisions (
                document_id TEXT NOT NULL, version TEXT NOT NULL, submitted_by TEXT NOT NULL,
                filename TEXT NOT NULL, file_type TEXT NOT NULL, content BLOB NOT NULL,
                extracted_text TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (document_id, version)
            )""")
            self._db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_pending_revision
                ON document_revisions(document_id) WHERE status = 'review'""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS document_segments (
                segment_id INTEGER PRIMARY KEY, document_id TEXT NOT NULL,
                location TEXT NOT NULL, text TEXT NOT NULL
            )""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS document_segment_terms (
                segment_id INTEGER NOT NULL, term TEXT NOT NULL,
                PRIMARY KEY (segment_id, term)
            )""")
            self._db.execute("CREATE INDEX IF NOT EXISTS segment_terms_lookup ON document_segment_terms(term, segment_id)")
            self._db.execute("CREATE INDEX IF NOT EXISTS segments_document ON document_segments(document_id)")
            self._db.execute("CREATE TABLE IF NOT EXISTS search_index_meta (version INTEGER NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS search_policy_epoch (version INTEGER NOT NULL)")
            if self._db.execute("SELECT 1 FROM search_policy_epoch LIMIT 1").fetchone() is None:
                self._db.execute("INSERT INTO search_policy_epoch VALUES (1)")
            if seed_demo:
                self._seed_demo()
            version = self._db.execute("SELECT version FROM search_index_meta LIMIT 1").fetchone()
            if version is None or version["version"] != 2:
                self._db.execute("DELETE FROM document_segment_terms")
                self._db.execute("DELETE FROM document_segments")
                self._db.execute("DELETE FROM search_index_meta")
                self._db.execute("INSERT INTO search_index_meta VALUES (2)")
            self._index_missing_documents()
            if self.record_index_events:
                self._db.execute("CREATE TABLE IF NOT EXISTS search_index_source (source_id TEXT NOT NULL)")
                if self._db.execute("SELECT 1 FROM search_index_source LIMIT 1").fetchone() is None:
                    self._db.execute("INSERT INTO search_index_source VALUES (?)", (uuid4().hex,))
                self._db.execute("""CREATE TABLE IF NOT EXISTS search_index_outbox (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    processed_at TEXT, last_error TEXT
                )""")
                self._db.execute("""CREATE INDEX IF NOT EXISTS search_index_outbox_pending
                    ON search_index_outbox(processed_at, attempts, event_id)""")
                if self._db.execute("SELECT 1 FROM search_index_outbox LIMIT 1").fetchone() is None:
                    self._queue_index_rebuild()

    @property
    def index_source_id(self) -> str:
        if not self.record_index_events:
            raise RuntimeError("Search index outbox is disabled")
        with self._lock:
            return self._db.execute("SELECT source_id FROM search_index_source LIMIT 1").fetchone()[0]

    def _seed_demo(self) -> None:
        for row in _DEMO_ROWS:
            document_id, title, file_type, area, version, updated_at, owner, location, excerpt, classification, visibility, departments = row
            allowlist = ["manager-001"] if document_id == "5" else []
            self._db.execute(
                """INSERT OR IGNORE INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (document_id, "demo-org", title, file_type, area, version, updated_at, owner, location, excerpt,
                 "published", classification, visibility, json.dumps(departments), json.dumps(allowlist), "[]"),
            )

    def _index_document(self, document_id: str, title: str, text: str, base_location: str) -> None:
        self._db.execute("DELETE FROM document_segment_terms WHERE segment_id IN (SELECT segment_id FROM document_segments WHERE document_id = ?)", (document_id,))
        self._db.execute("DELETE FROM document_segments WHERE document_id = ?", (document_id,))
        for number, segment in enumerate(split_segments(text), start=1):
            location = f"{base_location} · 片段 {number}" if base_location != "正文" else f"正文 · 片段 {number}"
            cursor = self._db.execute("INSERT INTO document_segments (document_id, location, text) VALUES (?, ?, ?)",
                                      (document_id, location, segment))
            self._db.executemany("INSERT INTO document_segment_terms (segment_id, term) VALUES (?, ?)",
                                 ((cursor.lastrowid, term) for term in search_terms(f"{title} {segment}")))

    def _index_missing_documents(self) -> None:
        rows = self._db.execute("""SELECT d.document_id, d.title, d.location, d.excerpt, f.extracted_text
            FROM documents d LEFT JOIN document_files f USING(document_id)
            WHERE NOT EXISTS (SELECT 1 FROM document_segments s WHERE s.document_id = d.document_id)""").fetchall()
        for row in rows:
            self._index_document(row["document_id"], row["title"],
                                 row["extracted_text"] or row["excerpt"], row["location"])

    @staticmethod
    def _record(row: sqlite3.Row) -> DocumentRecord:
        policy = DocumentPolicy(
            document_id=row["document_id"], org_id=row["org_id"],
            state=DocumentState(row["state"]), classification=Classification[row["classification"]],
            visibility=Visibility(row["visibility"]),
            department_ids=frozenset(json.loads(row["department_ids"])),
            allowed_user_ids=frozenset(json.loads(row["allowed_user_ids"])),
            denied_user_ids=frozenset(json.loads(row["denied_user_ids"])),
        )
        return DocumentRecord(
            row["document_id"], row["title"], row["file_type"], row["area"], row["version"],
            row["updated_at"], row["owner"], row["location"], row["excerpt"], policy,
            "file_document_id" in row.keys() and row["file_document_id"] is not None,
        )

    def list_visible(self, subject: Subject) -> list[DocumentRecord]:
        with self._lock:
            rows = self._db.execute("""SELECT d.*, f.document_id AS file_document_id
                FROM documents d LEFT JOIN document_files f USING(document_id)
                WHERE d.org_id = ? ORDER BY d.document_id""", (subject.org_id,)).fetchall()
        return [record for row in rows if authorize(subject, (record := self._record(row)).policy, ResourceAction.READ).allowed]

    def get_visible(self, subject: Subject, document_id: str) -> DocumentRecord | None:
        with self._lock:
            row = self._db.execute(
                """SELECT d.*, f.document_id AS file_document_id
                FROM documents d LEFT JOIN document_files f USING(document_id)
                WHERE d.org_id = ? AND d.document_id = ?""", (subject.org_id, document_id)
            ).fetchone()
        if row is None:
            return None
        record = self._record(row)
        return record if authorize(subject, record.policy, ResourceAction.READ).allowed else None

    def original_for(self, subject: Subject, document_id: str,
                     expected_version: str | None = None) -> tuple[str, str, bytes] | None:
        """Return only the current published original after a fresh download decision."""
        with self._lock:
            row = self._db.execute("""SELECT d.*, f.filename, f.content
                FROM documents d LEFT JOIN document_files f USING(document_id)
                WHERE d.org_id = ? AND d.document_id = ?""", (subject.org_id, document_id)).fetchone()
            if row is None:
                return None
            record = self._record(row)
            if not authorize(subject, record.policy, ResourceAction.DOWNLOAD).allowed:
                return None
            if expected_version is not None and record.version != expected_version:
                raise OriginalVersionChanged
            if row["content"] is None:
                return None
            return row["filename"], record.file_type, row["content"]

    def generation_original_for(self, subject: Subject, document_id: str,
                                expected_version: str) -> tuple[str, bytes] | None:
        """Read a published original for generation without granting download access."""
        with self._lock:
            row = self._db.execute("""SELECT d.*, f.content FROM documents d
                LEFT JOIN document_files f USING(document_id)
                WHERE d.org_id = ? AND d.document_id = ?""",
                (subject.org_id, document_id)).fetchone()
            if row is None:
                return None
            record = self._record(row)
            if not authorize(subject, record.policy, ResourceAction.USE_FOR_GENERATION).allowed:
                return None
            if record.version != expected_version:
                raise OriginalVersionChanged
            if row["content"] is None:
                return None
            return record.file_type, row["content"]

    def create_upload(self, subject: Subject, *, title: str, filename: str, file_type: str,
                      area: str, visibility: Visibility, content: bytes, extracted_text: str) -> str:
        document_id = uuid4().hex
        classification = {
            Visibility.ORGANIZATION: Classification.INTERNAL,
            Visibility.DEPARTMENT: Classification.DEPARTMENT,
            Visibility.ALLOWLIST: Classification.CONFIDENTIAL,
        }[visibility]
        departments = sorted(subject.department_ids) if visibility == Visibility.DEPARTMENT else []
        allowed = [subject.user_id] if visibility == Visibility.ALLOWLIST else []
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (document_id, subject.org_id, title, file_type, area, "v1.0",
                 datetime.now(timezone.utc).date().isoformat(), subject.user_id, "正文", extracted_text[:500],
                 DocumentState.REVIEW.value, classification.name, visibility.value,
                 json.dumps(departments), json.dumps(allowed), "[]"),
            )
            self._db.execute("INSERT INTO document_files VALUES (?, ?, ?, ?)",
                             (document_id, filename, content, extracted_text))
            self._index_document(document_id, title, extracted_text, "正文")
            self._enqueue_index_event(document_id)
        return document_id

    def search_visible(self, subject: Subject, question: str, *, area: str | None = None,
                       limit: int = 3) -> list[SearchHit]:
        terms = search_terms(question, question=True)
        if not subject.active or not terms or limit < 1:
            return []
        cache_key = None
        if self.search_cache is not None:
            with self._lock:
                epoch = self._db.execute("SELECT version FROM search_policy_epoch LIMIT 1").fetchone()[0]
            identity = [subject.org_id, subject.user_id, subject.role.value,
                        subject.clearance.value, sorted(subject.department_ids),
                        area, sorted(terms), limit, epoch, 2]
            cache_key = self.search_cache.key_prefix + hashlib.sha256(
                json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            cached = self.search_cache.get(cache_key)
            if cached is not None:
                restored = self._restore_search_candidates(subject, cached, area, limit, terms, epoch)
                if restored is not None:
                    return restored
        placeholders = ", ".join("?" for _ in terms)
        params = (*sorted(terms), subject.org_id, subject.clearance.value, subject.user_id, subject.role.value,
                  subject.user_id, json.dumps(sorted(subject.department_ids)), subject.user_id,
                  area, area)
        with self._lock:
            rows = self._db.execute("""SELECT d.*, s.segment_id, s.location AS segment_location,
                   s.text AS segment_text, COUNT(DISTINCT t.term) AS match_count
                FROM document_segment_terms t INDEXED BY segment_terms_lookup
                CROSS JOIN document_segments s
                CROSS JOIN documents d
                WHERE t.term IN (""" + placeholders + """)
                AND s.segment_id = t.segment_id AND d.document_id = s.document_id
                AND d.org_id = ? AND d.state = 'published'
                AND (CASE d.classification WHEN 'INTERNAL' THEN 1 WHEN 'DEPARTMENT' THEN 2
                     WHEN 'CONFIDENTIAL' THEN 3 WHEN 'RESTRICTED' THEN 4 ELSE 99 END) <= ?
                AND NOT EXISTS (SELECT 1 FROM json_each(d.denied_user_ids) WHERE value = ?)
                AND (? != 'system_admin' OR EXISTS
                    (SELECT 1 FROM json_each(d.allowed_user_ids) WHERE value = ?))
                AND (d.visibility = 'organization'
                     OR (d.visibility = 'department' AND EXISTS
                         (SELECT 1 FROM json_each(d.department_ids) WHERE value IN (SELECT value FROM json_each(?))))
                     OR (d.visibility = 'allowlist' AND EXISTS
                         (SELECT 1 FROM json_each(d.allowed_user_ids) WHERE value = ?)))
                AND (? IS NULL OR d.area = ?)
                GROUP BY s.segment_id ORDER BY match_count DESC, s.segment_id LIMIT 50""", params).fetchall()
        hits: list[tuple[int, SearchHit, int]] = []
        for row in rows:
            record = self._record(row)
            if not authorize(subject, record.policy, ResourceAction.READ).allowed:
                continue
            score = row["match_count"]
            if score >= min(2, len(terms)):
                hits.append((score, SearchHit(record, row["segment_location"], row["segment_text"]),
                             row["segment_id"]))
        hits.sort(key=lambda item: -item[0])
        selected = hits[:limit]
        if cache_key is not None:
            with self._lock:
                current_epoch = self._db.execute(
                    "SELECT version FROM search_policy_epoch LIMIT 1").fetchone()[0]
            if current_epoch == epoch:
                self.search_cache.set(cache_key, [[segment_id, hit.document.document_id,
                                                   hit.document.version, score]
                                                  for score, hit, segment_id in selected])
        return [hit for _, hit, _ in selected]

    def resolve_search_candidate(self, subject: Subject, *, segment_id: int,
                                 document_id: str, version: str, text: str,
                                 area: str | None = None) -> SearchHit | None:
        """Recheck a shadow-index candidate against current authoritative data."""
        if (not subject.active or type(segment_id) is not int or segment_id < 1
                or not isinstance(document_id, str) or not isinstance(version, str)
                or not isinstance(text, str)):
            return None
        with self._lock:
            row = self._db.execute("""SELECT d.*, s.location AS segment_location,
                s.text AS segment_text FROM document_segments s
                JOIN documents d USING(document_id)
                WHERE s.segment_id = ? AND d.document_id = ? AND d.org_id = ?""",
                (segment_id, document_id, subject.org_id)).fetchone()
            if (row is None or row["version"] != version or row["segment_text"] != text
                    or (area is not None and row["area"] != area)):
                return None
            record = self._record(row)
            if not authorize(subject, record.policy, ResourceAction.READ).allowed:
                return None
            return SearchHit(record, row["segment_location"], row["segment_text"])

    def _restore_search_candidates(self, subject: Subject, cached: list,
                                   area: str | None, limit: int, terms: set[str],
                                   epoch: int) -> list[SearchHit] | None:
        if len(cached) > limit:
            return None
        restored = []
        with self._lock:
            if self._db.execute("SELECT version FROM search_policy_epoch LIMIT 1").fetchone()[0] != epoch:
                return None
            for entry in cached:
                if (not isinstance(entry, list) or len(entry) != 4 or
                        type(entry[0]) is not int or not isinstance(entry[1], str) or
                        not isinstance(entry[2], str) or type(entry[3]) is not int):
                    return None
                segment_id, document_id, version, score = entry
                if segment_id < 1 or score < 1:
                    return None
                row = self._db.execute("""SELECT d.*, s.location AS segment_location,
                    s.text AS segment_text FROM document_segments s
                    JOIN documents d USING(document_id)
                    WHERE s.segment_id = ? AND d.document_id = ? AND d.org_id = ?""",
                    (segment_id, document_id, subject.org_id)).fetchone()
                if row is None or row["version"] != version or (area is not None and row["area"] != area):
                    return None
                record = self._record(row)
                if not authorize(subject, record.policy, ResourceAction.READ).allowed:
                    return None
                actual_score = len(search_terms(f"{record.title} {row['segment_text']}") & terms)
                if actual_score != score or score < min(2, len(terms)):
                    return None
                restored.append(SearchHit(record, row["segment_location"], row["segment_text"]))
        return restored

    def _advance_search_epoch(self) -> None:
        self._db.execute("UPDATE search_policy_epoch SET version = version + 1")

    def _enqueue_index_event(self, document_id: str) -> None:
        if self.record_index_events:
            self._db.execute("""INSERT INTO search_index_outbox (document_id, created_at)
                VALUES (?, ?)""", (document_id, datetime.now(timezone.utc).isoformat()))

    def _queue_index_rebuild(self) -> int:
        document_ids = [row[0] for row in self._db.execute(
            "SELECT document_id FROM documents ORDER BY document_id")]
        for document_id in document_ids:
            self._enqueue_index_event(document_id)
        return len(document_ids)

    def queue_index_rebuild(self) -> int:
        if not self.record_index_events:
            raise RuntimeError("Search index outbox is disabled")
        with self._lock, self._db:
            return self._queue_index_rebuild()

    def pending_index_events(self, *, limit: int = 20, max_attempts: int = 3) -> list[dict]:
        if not self.record_index_events:
            return []
        with self._lock:
            rows = self._db.execute("""SELECT event_id, document_id, attempts
                FROM search_index_outbox WHERE processed_at IS NULL AND attempts < ?
                ORDER BY event_id LIMIT ?""", (max_attempts, max(0, limit))).fetchall()
        return [dict(row) for row in rows]

    def mark_index_event_done(self, event_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("""UPDATE search_index_outbox
                SET processed_at = ?, last_error = NULL
                WHERE event_id = ? AND processed_at IS NULL""",
                (datetime.now(timezone.utc).isoformat(), event_id))

    def mark_index_event_failed(self, event_id: int, error_code: str) -> None:
        with self._lock, self._db:
            self._db.execute("""UPDATE search_index_outbox
                SET attempts = attempts + 1, last_error = ?
                WHERE event_id = ? AND processed_at IS NULL""", (error_code[:80], event_id))

    def list_pending(self, subject: Subject) -> list[DocumentRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM documents WHERE org_id = ? AND state = ? ORDER BY updated_at DESC",
                (subject.org_id, DocumentState.REVIEW.value),
            ).fetchall()
        return [self._record(row) for row in rows if self._can_review(subject, row)]

    @staticmethod
    def _can_review(subject: Subject, row: sqlite3.Row) -> bool:
        if subject.role.value == "knowledge_admin":
            return bool(subject.department_ids.intersection(json.loads(row["department_ids"]))) or row["owner"] == subject.user_id
        if subject.role.value == "department_manager":
            return row["owner"] == subject.user_id or bool(subject.department_ids.intersection(json.loads(row["department_ids"])))
        return False

    def publish(self, subject: Subject, document_id: str) -> bool:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM documents WHERE org_id = ? AND document_id = ? AND state = ?",
                (subject.org_id, document_id, DocumentState.REVIEW.value),
            ).fetchone()
            if row is None or not self._can_review(subject, row):
                return False
            self._db.execute("UPDATE documents SET state = ? WHERE document_id = ?",
                             (DocumentState.PUBLISHED.value, document_id))
            self._advance_search_epoch()
            self._enqueue_index_event(document_id)
        return True

    def can_submit_revision(self, subject: Subject, document_id: str) -> bool:
        if subject.role.value not in ("department_manager", "knowledge_admin"):
            return False
        with self._lock:
            row = self._db.execute("""SELECT * FROM documents WHERE org_id = ? AND document_id = ?
                AND state = 'published' AND owner = ?""",
                (subject.org_id, document_id, subject.user_id)).fetchone()
        return row is not None and authorize(subject, self._record(row).policy, ResourceAction.READ).allowed

    def create_revision(self, subject: Subject, document_id: str, *, filename: str,
                        file_type: str, content: bytes, extracted_text: str) -> str | None:
        with self._lock, self._db:
            if not self.can_submit_revision(subject, document_id):
                return None
            row = self._db.execute("SELECT version FROM documents WHERE document_id = ?", (document_id,)).fetchone()
            if self._db.execute("SELECT 1 FROM document_revisions WHERE document_id = ? AND status = 'review'",
                                (document_id,)).fetchone():
                raise ValueError("REVISION_PENDING")
            prefix, dot, number = row["version"].rpartition(".")
            if not dot or not number.isdigit():
                raise ValueError("INVALID_DOCUMENT_VERSION")
            version = f"{prefix}.{int(number) + 1}"
            self._db.execute("""INSERT INTO document_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                             (document_id, version, subject.user_id, filename, file_type, content,
                              extracted_text, "review", datetime.now(timezone.utc).isoformat()))
        return version

    def list_pending_revisions(self, subject: Subject) -> list[dict]:
        if subject.role.value not in ("department_manager", "knowledge_admin"):
            return []
        with self._lock:
            rows = self._db.execute("""SELECT r.document_id, r.version, r.submitted_by, d.title,
                d.org_id, d.owner, d.department_ids FROM document_revisions r
                JOIN documents d USING(document_id) WHERE d.org_id = ? AND r.status = 'review'
                ORDER BY r.created_at DESC""", (subject.org_id,)).fetchall()
        return [{"document_id": row["document_id"], "version": row["version"], "title": row["title"]}
                for row in rows if (row["submitted_by"] == subject.user_id or row["owner"] == subject.user_id
                                    or subject.department_ids.intersection(json.loads(row["department_ids"])))
                and self.get_visible(subject, row["document_id"]) is not None]

    def publish_revision(self, subject: Subject, document_id: str) -> str | None:
        with self._lock, self._db:
            row = self._db.execute("""SELECT d.*, f.filename AS old_filename, f.content AS old_content,
                f.extracted_text AS old_text FROM documents d
                LEFT JOIN document_files f USING(document_id)
                WHERE d.org_id = ? AND d.document_id = ? AND d.state = 'published'""",
                (subject.org_id, document_id)).fetchone()
            if (row is None or not self._can_review(subject, row)
                    or not authorize(subject, self._record(row).policy, ResourceAction.READ).allowed):
                return None
            revision = self._db.execute("""SELECT * FROM document_revisions
                WHERE document_id = ? AND status = 'review'""", (document_id,)).fetchone()
            if revision is None:
                return None
            if row["old_content"] is not None:
                self._db.execute("""INSERT OR IGNORE INTO document_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                 (document_id, row["version"], row["owner"], row["old_filename"], row["file_type"],
                                  row["old_content"], row["old_text"], "archived", row["updated_at"]))
                self._db.execute("""UPDATE document_revisions SET status = 'archived'
                    WHERE document_id = ? AND version = ?""", (document_id, row["version"]))
            self._db.execute("""UPDATE documents SET version = ?, file_type = ?, updated_at = ?,
                excerpt = ? WHERE document_id = ?""",
                (revision["version"], revision["file_type"], datetime.now(timezone.utc).date().isoformat(),
                 revision["extracted_text"][:500], document_id))
            self._db.execute("""INSERT INTO document_files VALUES (?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET filename=excluded.filename,
                content=excluded.content, extracted_text=excluded.extracted_text""",
                (document_id, revision["filename"], revision["content"], revision["extracted_text"]))
            self._index_document(document_id, row["title"], revision["extracted_text"], "正文")
            self._advance_search_epoch()
            self._enqueue_index_event(document_id)
            self._db.execute("""UPDATE document_revisions SET status = 'published'
                WHERE document_id = ? AND version = ?""", (document_id, revision["version"]))
        return revision["version"]

    def text_for(self, subject: Subject, document_id: str, action: ResourceAction) -> tuple[DocumentRecord, str] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT d.*, f.extracted_text FROM documents d LEFT JOIN document_files f USING(document_id) "
                "WHERE d.org_id = ? AND d.document_id = ?", (subject.org_id, document_id),
            ).fetchone()
        if row is None:
            return None
        record = self._record(row)
        if not authorize(subject, record.policy, action).allowed:
            return None
        return record, row["extracted_text"] or record.excerpt

    def set_denied(self, org_id: str, document_id: str, user_id: str, denied: bool) -> bool:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT denied_user_ids FROM documents WHERE org_id = ? AND document_id = ?", (org_id, document_id)
            ).fetchone()
            if row is None:
                return False
            users = set(json.loads(row["denied_user_ids"]))
            if denied:
                users.add(user_id)
            else:
                users.discard(user_id)
            self._db.execute(
                "UPDATE documents SET denied_user_ids = ? WHERE org_id = ? AND document_id = ?",
                (json.dumps(sorted(users)), org_id, document_id),
            )
            self._advance_search_epoch()
            self._enqueue_index_event(document_id)
        return True

    def close(self) -> None:
        self._db.close()
