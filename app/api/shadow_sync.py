"""Opt-in SQLite outbox consumer for the local pgvector shadow index."""

import json
from typing import Protocol

from app.api.documents import DocumentRepository


class Encoder(Protocol):
    def encode(self, sentences, *, normalize_embeddings: bool, show_progress_bar: bool): ...


class IndexSink(Protocol):
    def upsert_document(self, document: dict, segments: list[dict],
                        *, event_id: int) -> bool: ...


def document_snapshot(repository: DocumentRepository, encoder: Encoder,
                      document_id: str) -> tuple[dict, list[dict]]:
    with repository._lock:
        source = repository._db.execute(
            "SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()
        segments = [dict(row) for row in repository._db.execute(
            "SELECT * FROM document_segments WHERE document_id = ? ORDER BY segment_id",
            (document_id,))]
    if source is None:
        raise ValueError("Document no longer exists in the source catalog")
    row = dict(source)
    vectors = encoder.encode([row["title"] + " " + item["text"] for item in segments],
                             normalize_embeddings=True, show_progress_bar=False)
    document = {"document_id": row["document_id"], "org_id": row["org_id"],
                "title": row["title"], "area": row["area"], "version": row["version"],
                "state": row["state"],
                "classification": {"INTERNAL": 1, "DEPARTMENT": 2,
                                   "CONFIDENTIAL": 3, "RESTRICTED": 4}[row["classification"]],
                "visibility": row["visibility"],
                "department_ids": json.loads(row["department_ids"]),
                "allowed_user_ids": json.loads(row["allowed_user_ids"]),
                "denied_user_ids": json.loads(row["denied_user_ids"])}
    indexed = [{"segment_id": item["segment_id"], "document_id": document_id,
                "location": item["location"], "body": item["text"],
                "embedding": "[" + ",".join(str(float(value)) for value in vector) + "]"}
               for item, vector in zip(segments, vectors, strict=True)]
    return document, indexed


def drain_index_outbox(repository: DocumentRepository, sink: IndexSink,
                       encoder: Encoder, *, max_events: int = 20,
                       max_attempts: int = 3) -> dict[str, int]:
    processed = 0
    skipped_old = 0
    failed = 0
    for event in repository.pending_index_events(limit=max_events, max_attempts=max_attempts):
        try:
            document, segments = document_snapshot(repository, encoder, event["document_id"])
            applied = sink.upsert_document(document, segments, event_id=event["event_id"])
            repository.mark_index_event_done(event["event_id"])
            processed += 1
            skipped_old += not applied
        except Exception as exc:
            repository.mark_index_event_failed(event["event_id"], type(exc).__name__)
            failed += 1
    return {"processed": processed, "skipped_old": skipped_old, "failed": failed}
