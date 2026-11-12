"""Server-owned question-to-document collaboration with recoverable partial results."""

import hashlib
import json
import threading
import time
from contextlib import contextmanager

from app.api.artifacts import (ArtifactRepository, CompositionNoEvidence, DraftChanged,
                               SourceVersionChanged)
from app.api.collaboration import CollaborationInvalid, CollaborationModel
from app.api.collaboration_runs import CollaborationRunRepository
from app.api.documents import DocumentRepository
from app.api.content import split_segments
from app.security.policy import ResourceAction, Subject


_request_locks_guard = threading.Lock()
_request_locks: dict[tuple[int, str, str, str], tuple[threading.Lock, int]] = {}


@contextmanager
def _request_lock(key: tuple[int, str, str, str]):
    with _request_locks_guard:
        lock, users = _request_locks.get(key, (threading.Lock(), 0))
        _request_locks[key] = (lock, users + 1)
    try:
        with lock:
            yield
    finally:
        with _request_locks_guard:
            _, users = _request_locks[key]
            if users == 1:
                del _request_locks[key]
            else:
                _request_locks[key] = (lock, users - 1)


def _line(value: object, limit: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise CollaborationInvalid("COMPOSITION_PLAN_INVALID")
    return value.strip()


def authorized_candidates(question: str, subject: Subject,
                          documents: DocumentRepository,
                          source_hints: list[dict] | None = None) -> list[dict]:
    """Return the same bounded, version-checked candidate pool for composition."""
    if source_hints is not None:
        if not 1 <= len(source_hints) <= 6:
            raise CompositionNoEvidence()
        candidates = []
        seen = set()
        for hint in source_hints:
            document_id = hint["document_id"]
            if document_id in seen:
                continue
            source = documents.text_for(subject, document_id, ResourceAction.USE_FOR_GENERATION)
            if source is None:
                raise CompositionNoEvidence()
            record, content = source
            if record.version != hint["version"]:
                raise SourceVersionChanged()
            location = hint["location"]
            excerpt = next((segment for index, segment in enumerate(split_segments(content), start=1)
                            if location == (f"{record.location} · 片段 {index}" if record.location != "正文"
                                            else f"正文 · 片段 {index}")), None)
            if excerpt is None:
                raise SourceVersionChanged()
            candidates.append({"document_id": document_id, "title": record.title,
                               "version": record.version, "area": record.area,
                               "excerpt": excerpt[:450]})
            seen.add(document_id)
        if not candidates:
            raise CompositionNoEvidence()
        return candidates
    candidates = []
    seen = set()
    for hit in documents.search_visible(subject, question, limit=12):
        document_id = hit.document.document_id
        if document_id in seen:
            continue
        current = documents.text_for(subject, document_id, ResourceAction.USE_FOR_GENERATION)
        if current is None:
            continue
        record, _ = current
        if record.version != hit.document.version:
            raise SourceVersionChanged()
        candidates.append({"document_id": document_id, "title": record.title,
                           "version": record.version, "area": record.area,
                           "excerpt": hit.text[:450]})
        seen.add(document_id)
        if len(candidates) == 6:
            break
    if not candidates:
        raise CompositionNoEvidence()
    return candidates


def _plan(question: str, subject: Subject, documents: DocumentRepository,
          model: CollaborationModel,
          source_hints: list[dict] | None = None) -> tuple[str, list[str], list[str], dict, dict[str, str]]:
    candidates = authorized_candidates(question, subject, documents, source_hints)

    payload = {"question": question, "kind": "docx", "candidates": candidates}
    if len(json.dumps(payload, ensure_ascii=False)) > 8000:
        raise CollaborationInvalid("COMPOSITION_INPUT_TOO_LARGE")
    started = time.monotonic()
    output = model.complete("compose_supervisor", payload)
    elapsed = time.monotonic() - started
    if elapsed > 25:
        raise CollaborationInvalid("COMPOSITION_TIME_BUDGET_EXCEEDED")
    if not isinstance(output, dict):
        raise CollaborationInvalid("COMPOSITION_PLAN_INVALID")
    title = _line(output.get("title"), 120)
    source_ids = output.get("source_ids")
    allowed = {candidate["document_id"] for candidate in candidates}
    if (not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 3
            or any(not isinstance(source_id, str) or source_id not in allowed
                   for source_id in source_ids) or len(set(source_ids)) != len(source_ids)):
        raise CollaborationInvalid("COMPOSITION_PLAN_INVALID")
    headings = output.get("headings")
    focus = output.get("focus")
    if (not isinstance(headings, list) or not 1 <= len(headings) <= 5
            or not isinstance(focus, list) or len(focus) != len(headings)):
        raise CollaborationInvalid("COMPOSITION_PLAN_INVALID")
    outline = [_line(heading, 100) for heading in headings]
    focus = [_line(value, 100) for value in focus]
    versions = {candidate["document_id"]: candidate["version"] for candidate in candidates
                if candidate["document_id"] in source_ids}
    plan = {"focus": focus, "candidate_count": len(candidates),
            "elapsed_ms": round(elapsed * 1000)}
    return title, source_ids, outline, plan, versions


def _compose_and_collaborate_locked(subject: Subject, documents: DocumentRepository,
                                    artifacts: ArtifactRepository, runs: CollaborationRunRepository,
                                    model: CollaborationModel, *, question: str, request_id: str,
                                    presentation_style: str | None = None,
                                    source_hints: list[dict] | None = None,
                                    answer_reference: str | None = None) -> dict:
    """Replay never calls the model again; a failed run resumes via /collaborate."""
    input_hash = None
    if source_hints is not None:
        payload = {"question": question.strip(), "source_hints": source_hints,
                   "answer_reference": answer_reference}
        input_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                               separators=(",", ":")).encode("utf-8")).hexdigest()
    item = artifacts.composed_for(subject, documents, question=question,
                                  request_id=request_id, input_hash=input_hash)
    if item is None:
        title, source_ids, outline, plan, versions = _plan(
            question, subject, documents, model, source_hints)
        item = artifacts.create_planned_from_question(
            subject, documents, question=question, request_id=request_id,
            title=title, source_ids=source_ids, outline=outline,
            plan=plan, source_versions=versions, input_hash=input_hash)
    created = item["composition_created"]
    run = runs.get_latest(subject, item["id"], artifacts, documents)
    if item["collaboration_trace"] or run is not None or item["status"] != "pending":
        return {"artifact": item, "run": run,
                "status": run["status"] if run else ("completed" if item["collaboration_trace"] else "not_run")}

    try:
        item = runs.execute(subject, item["id"], artifacts, documents, model,
                            item["draft_token"], request_id, presentation_style)
    except (SourceVersionChanged, DraftChanged):
        raise
    except Exception:
        # The run repository records the failed node before re-raising. Keep the
        # reviewable task visible to the caller without exposing model details.
        current = artifacts.get(subject, item["id"], documents)
        failed = runs.get_latest(subject, item["id"], artifacts, documents)
        if current is None or failed is None or failed["status"] != "failed":
            raise
        return {"artifact": current | {"composition_created": created},
                "run": failed, "status": "failed"}

    run = runs.get_latest(subject, item["id"], artifacts, documents)
    if run is None or run["status"] != "completed":
        raise RuntimeError("COLLABORATION_RUN_NOT_SAVED")
    return {"artifact": item | {"composition_created": created},
            "run": run, "status": "completed"}


def compose_and_collaborate(subject: Subject, documents: DocumentRepository,
                            artifacts: ArtifactRepository, runs: CollaborationRunRepository,
                            model: CollaborationModel, *, question: str, request_id: str,
                            presentation_style: str | None = None,
                            source_hints: list[dict] | None = None,
                            answer_reference: str | None = None) -> dict:
    key = (id(artifacts), subject.org_id, subject.user_id, request_id)
    with _request_lock(key):
        return _compose_and_collaborate_locked(
            subject, documents, artifacts, runs, model,
            question=question, request_id=request_id,
            presentation_style=presentation_style, source_hints=source_hints,
            answer_reference=answer_reference)
