"""Three-path composition comparison; offline by default, optional live model."""

import argparse
import hashlib
import io
import json
import os
import random
import secrets
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from docx import Document

from app.api.artifacts import ArtifactRepository, CompositionNoEvidence
from app.api.collaboration import (CollaborationInvalid, CollaborationModel,
                                   OpenAICollaborationModel)
from app.api.collaboration_runs import CollaborationRunRepository
from app.api.compound_tasks import authorized_candidates, compose_and_collaborate
from app.api.content import split_segments
from app.api.documents import DocumentRepository
from app.llm.client import MODEL_NAME
from app.security.policy import ResourceAction, Subject
from eval.evaluate_authorized_search import SUBJECTS


CASES = Path(__file__).with_name("composition_cases.json")
VARIANTS = ("single_agent", "fixed_workflow", "collaboration")


class CompositionRunFailed(CollaborationInvalid):
    def __init__(self, code: str, stage: str | None):
        super().__init__(code)
        self.stage = stage


class SyntheticModel:
    """A deterministic adapter for plumbing checks, not a quality judge."""

    def __init__(self):
        self.calls: list[str] = []

    def complete(self, stage: str, payload: dict) -> dict:
        self.calls.append(stage)
        if stage in ("compose_supervisor", "single_agent"):
            candidates = payload["candidates"]
            count = 2 if "知识检索权限治理和文档生成" in payload["question"] else 1
            selected = candidates[:count]
            result = {"title": payload["question"][:80] + " · 资料整理",
                      "source_ids": [source["document_id"] for source in selected],
                      "headings": [payload["question"][:100]],
                      "focus": [payload["question"][:100]]}
            if stage == "single_agent":
                quotes = [source["excerpt"] for source in selected]
                result.update({"source_quotes": quotes, "draft": "；".join(quotes)[:400]})
            return result
        if stage == "supervisor":
            return {"focus": payload["headings"]}
        if stage == "knowledge":
            if "evidence_sections" in payload:
                return {"source_quotes": [[source["text"] for source in sources]
                                          for sources in payload["evidence_sections"]]}
            return {"quotes": payload["excerpts"]}
        if stage == "document":
            if "evidence_sections" in payload:
                return {"drafts": ["；".join(source["quote"] for source in sources)[:400]
                                   for sources in payload["evidence_sections"]]}
            return {"drafts": [quote[:400] for quote in payload["quotes"]]}
        raise ValueError("UNKNOWN_STAGE")


class ObservedModel:
    def __init__(self, delegate: CollaborationModel):
        self.delegate = delegate
        self.calls: list[str] = []
        self.usage_records: list[dict] = []

    def complete(self, stage: str, payload: dict) -> dict:
        self.calls.append(stage)
        try:
            return self.delegate.complete(stage, payload)
        finally:
            usage = getattr(self.delegate, "last_usage", None)
            if usage is not None:
                self.usage_records.append({"stage": stage, **usage})


def _single_agent(question: str, subject: Subject, documents: DocumentRepository,
                  artifacts: ArtifactRepository, model: CollaborationModel) -> dict:
    candidates = authorized_candidates(question, subject, documents)
    output = model.complete("single_agent", {"question": question, "kind": "docx",
                                             "candidates": candidates})
    allowed = {source["document_id"]: source for source in candidates}
    ids = output.get("source_ids")
    headings = output.get("headings")
    quotes = output.get("source_quotes")
    title = output.get("title")
    draft_text = output.get("draft")
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 2 or len(set(ids)) != len(ids)
            or any(not isinstance(i, str) or i not in allowed for i in ids)
            or not isinstance(headings, list) or len(headings) != 1
            or not isinstance(headings[0], str) or not 0 < len(headings[0].strip()) <= 100
            or not isinstance(title, str) or not 0 < len(title.strip()) <= 120
            or not isinstance(quotes, list) or len(quotes) != len(ids)
            or any(not isinstance(q, str) or not q.strip() or len(q) > 450
                   or q not in allowed[i]["excerpt"] for i, q in zip(ids, quotes))
            or not isinstance(draft_text, str) or not 0 < len(draft_text.strip()) <= 400):
        raise CollaborationInvalid("SINGLE_AGENT_OUTPUT_INVALID")
    versions = {i: allowed[i]["version"] for i in ids}
    item = artifacts.create(subject, documents, kind="docx", title=title, goal=question,
                            source_ids=ids, outline=headings, expected_versions=versions)
    if item is None:
        raise CompositionNoEvidence()
    section = item["section_drafts"][0]
    sources = section.get("supporting_sources") or [
        {"text": section["text"], "citation": section["citation"]}]
    by_id = {source["citation"]["document_id"]: source for source in sources
             if source["citation"] is not None}
    if any(i not in by_id or q not in by_id[i]["text"] or
           by_id[i]["citation"]["version"] != versions[i]
           for i, q in zip(ids, quotes)):
        raise CollaborationInvalid("SINGLE_AGENT_UNSUPPORTED_QUOTE")
    updated = dict(section, ai_quote=quotes[0], ai_text=draft_text,
                   ai_evidence=[{"quote": q, "citation": by_id[i]["citation"]}
                                for i, q in zip(ids, quotes)])
    result = artifacts.commit_collaboration(subject, item["id"], documents, [updated],
                                            [{"agent": "single_agent", "elapsed_ms": 0,
                                              "input_sections": 1,
                                              "output": {"source_ids": ids}},
                                             {"agent": "validator", "elapsed_ms": 0,
                                              "input_sections": 1,
                                              "output": {"valid": True, "supported_quotes": len(quotes)}}],
                                            item["draft_token"], "offline-single-agent")
    if result is None:
        raise CompositionNoEvidence()
    return result


def _run_variant(variant: str, case: dict, model: CollaborationModel,
                 subject: Subject, documents: DocumentRepository,
                 artifacts: ArtifactRepository, runs: CollaborationRunRepository) -> dict:
    question = case["question"]
    if variant == "single_agent":
        return _single_agent(question, subject, documents, artifacts, model)
    if variant == "fixed_workflow":
        candidates = authorized_candidates(question, subject, documents)
        selected = candidates[:2]
        ids = [source["document_id"] for source in selected]
        item = artifacts.create(subject, documents, kind="docx",
                                title=question[:80] + " · 资料整理", goal=question,
                                source_ids=ids, outline=[question[:100]],
                                expected_versions={source["document_id"]: source["version"]
                                                   for source in selected})
        if item is None:
            raise CompositionNoEvidence()
        result = runs.execute(subject, item["id"], artifacts, documents, model,
                              item["draft_token"], "offline-fixed")
        if result is None:
            raise CompositionNoEvidence()
        return result
    if variant == "collaboration":
        result = compose_and_collaborate(subject, documents, artifacts, runs, model,
                                         question=question, request_id="offline-collaboration")
        if result["status"] != "completed":
            run = result.get("run") or {}
            raise CompositionRunFailed(run.get("error_code") or "COMPOSITION_INCOMPLETE",
                                       run.get("stage"))
        return result["artifact"]
    raise ValueError("UNKNOWN_VARIANT")


def _evidence_valid(item: dict, subject: Subject, documents: DocumentRepository) -> bool:
    if item["source_status"] != "current":
        return False
    for draft in item["section_drafts"]:
        evidence = draft.get("ai_evidence") or ([{"quote": draft.get("ai_quote"),
                                                  "citation": draft.get("citation")}]
                                                if draft.get("ai_quote") else [])
        if not evidence:
            return False
        for entry in evidence:
            citation = entry["citation"]
            if not citation or citation["document_id"] not in item["source_ids"]:
                return False
            source = documents.text_for(subject, citation["document_id"],
                                        ResourceAction.USE_FOR_GENERATION)
            if (source is None or source[0].version != citation["version"]
                    or source[0].title != citation["title"]):
                return False
            base = source[0].location
            segments = ((f"{base} · 片段 {index}" if base != "正文" else f"正文 · 片段 {index}",
                         segment)
                        for index, segment in enumerate(split_segments(source[1]), start=1))
            if not any(location == citation["location"] and
                       "".join(entry["quote"].split()) in "".join(segment.split())
                       for location, segment in segments):
                return False
    return True


def run_case(variant: str, case: dict, model: CollaborationModel | None = None,
             *, include_review_text: bool = False) -> dict:
    observed = ObservedModel(model if model is not None else SyntheticModel())
    documents = DocumentRepository(":memory:", seed_demo=True)
    artifacts = ArtifactRepository(":memory:")
    runs = CollaborationRunRepository(":memory:")
    subject = SUBJECTS[case["role"]]
    if case.get("deny_id"):
        documents.set_denied(subject.org_id, case["deny_id"], subject.user_id, True)
    start = time.perf_counter()
    error_code = None
    error_stage = None
    try:
        try:
            item = _run_variant(variant, case, observed, subject, documents, artifacts, runs)
        except CompositionNoEvidence:
            status, item = "no_evidence", None
        except Exception as exc:
            status, item = exc.__class__.__name__, None
            error_code = str(exc) if isinstance(exc, CollaborationInvalid) else None
            error_stage = exc.stage if isinstance(exc, CompositionRunFailed) else None
        else:
            status = "completed"
        expected = set(case["expected_ids"])
        if item is None:
            passed = not expected and status == "no_evidence" and not observed.calls
            return {"case": case["name"], "variant": variant, "status": status,
                    "error_code": error_code, "error_stage": error_stage, "passed": passed,
                    "model_calls": len(observed.calls),
                    "reported_input_tokens": sum(x["input_tokens"] for x in observed.usage_records),
                    "reported_output_tokens": sum(x["output_tokens"] for x in observed.usage_records),
                    "usage_reported_calls": len(observed.usage_records),
                    "latency_ms": round((time.perf_counter() - start) * 1000, 2)}
        selected = set(item["source_ids"])
        supported = _evidence_valid(item, subject, documents)
        fact = "".join(case["expected_fact"].split())
        fact_covered = any(fact in "".join(draft.get("ai_text", "").split())
                           for draft in item["section_drafts"])
        # Automated confirmation only checks that the same reviewed draft can become a real DOCX.
        confirmed = artifacts.confirm(subject, item["id"], documents,
                                      item["outline"], item["draft_token"])
        download = artifacts.download(subject, item["id"], documents) if confirmed else None
        file_valid = bool(download and download[1][:2] == b"PK" and
                          Document(io.BytesIO(download[1])).paragraphs)
        passed = bool(expected and expected <= selected and supported and fact_covered and file_valid)
        result = {"case": case["name"], "variant": variant, "status": status,
                "selected_ids": sorted(selected), "expected_ids_found": expected <= selected,
                "citation_supported": supported, "expected_fact_present": fact_covered,
                "docx_valid": file_valid, "passed": passed, "model_calls": len(observed.calls),
                "reported_input_tokens": sum(x["input_tokens"] for x in observed.usage_records),
                "reported_output_tokens": sum(x["output_tokens"] for x in observed.usage_records),
                "usage_reported_calls": len(observed.usage_records),
                "latency_ms": round((time.perf_counter() - start) * 1000, 2)}
        if include_review_text:
            result["review_text"] = "\n\n".join(
                f"{draft['heading']}\n{draft.get('ai_text', '')}" for draft in item["section_drafts"])
        return result
    finally:
        runs.close()
        artifacts._db.close()
        documents.close()


def evaluate(cases: list[dict] | None = None, *, variants: tuple[str, ...] = VARIANTS,
             model_factory: Callable[[], CollaborationModel] = SyntheticModel,
             include_review_text: bool = False) -> dict:
    cases = cases if cases is not None else json.loads(CASES.read_text(encoding="utf-8"))
    if not cases or not variants:
        raise ValueError("EMPTY_EVALUATION_SET")
    results = [run_case(variant, case, model_factory(), include_review_text=include_review_text)
               for case in cases for variant in variants]
    summary = {}
    for variant in variants:
        rows = [row for row in results if row["variant"] == variant]
        elapsed = [row["latency_ms"] for row in rows]
        summary[variant] = {"passed": sum(row["passed"] for row in rows), "cases": len(rows),
                            "model_calls": sum(row["model_calls"] for row in rows),
                            "reported_input_tokens": sum(row["reported_input_tokens"] for row in rows),
                            "reported_output_tokens": sum(row["reported_output_tokens"] for row in rows),
                            "usage_reported_calls": sum(row["usage_reported_calls"] for row in rows),
                            "median_local_ms": round(statistics.median(elapsed), 2)}
    live = model_factory is OpenAICollaborationModel
    return {"dataset": CASES.name, "dataset_version": "m5.5-synthetic-v1",
            "dataset_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "case_names": [case["name"] for case in cases], "variants": list(variants),
            "model": "configured_endpoint" if live else "deterministic_fake",
            "model_id": MODEL_NAME if live else None,
            "api_style": (os.getenv("COLLABORATION_API_STYLE",
                                    os.getenv("GENERAL_API_STYLE", "responses")) if live else None),
            "real_model_execution": live, "real_model_quality_measured": False,
            "cost_measured": False, "performance_benchmark": False,
            "token_counts_are_provider_reported": live,
            "note": ("Live calls on synthetic data only; exact phrase checks are not semantic review. "
                     "Reported token usage may be missing; prices and controlled p95 are not measured."
                     if live else
                     "Local fake-model regression only; tiny sample and no network/token cost."),
            "summary": summary, "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional report path; otherwise print JSON")
    parser.add_argument("--live", action="store_true", help="Call the configured paid model on synthetic data")
    parser.add_argument("--case", action="append", help="Select a case by name (repeatable)")
    parser.add_argument("--all-cases", action="store_true", help="Explicitly include every case in live mode")
    parser.add_argument("--variant", action="append", choices=VARIANTS,
                        help="Select a variant (repeatable); default is all three")
    parser.add_argument("--blind-review-dir", type=Path,
                        help="Write randomized review cards and a separate answer key for synthetic cases")
    args = parser.parse_args()
    if args.live and not (args.case or args.all_cases):
        parser.error("--live requires --case NAME or --all-cases")
    if args.case and args.all_cases:
        parser.error("use --case or --all-cases, not both")
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    if args.case:
        unknown = set(args.case) - {case["name"] for case in cases}
        if unknown:
            parser.error("unknown case: " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case["name"] in set(args.case)]
    report = evaluate(cases, variants=tuple(args.variant or VARIANTS),
                      model_factory=OpenAICollaborationModel if args.live else SyntheticModel,
                      include_review_text=bool(args.blind_review_dir))
    if args.blind_review_dir:
        args.blind_review_dir.mkdir(parents=True, exist_ok=True)
        case_by_name = {case["name"]: case for case in cases}
        cards, key = [], {}
        for row in report["results"]:
            review_text = row.pop("review_text", None)
            if review_text is None:
                continue
            card_id = secrets.token_hex(5)
            cards.append({"id": card_id, "case": row["case"],
                          "question": case_by_name[row["case"]]["question"],
                          "draft": review_text,
                          "review": {"factual_support": None, "completeness": None,
                                     "readability": None, "notes": ""}})
            key[card_id] = {"case": row["case"], "variant": row["variant"]}
        random.SystemRandom().shuffle(cards)
        (args.blind_review_dir / "cards.json").write_text(
            json.dumps(cards, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.blind_review_dir / "answer_key.json").write_text(
            json.dumps(key, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    else:
        print(serialized)
    raise SystemExit(0 if all(row["passed"] for row in report["results"]) else 1)
