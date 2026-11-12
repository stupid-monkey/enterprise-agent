"""Time a synthetic answer-to-DOCX journey through the actual HTTP handlers."""

import argparse
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient

from app.api.artifacts import ArtifactRepository
from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.collaboration import OpenAICollaborationModel
from app.api.conversations import ConversationRepository
from app.api.documents import DocumentRepository
from app.api.intent_model import IntentDecision, OpenAIIntentClassifier
from app.api.main import create_app
from app.api.user_memories import UserMemoryRepository
from app.security.policy import Classification, Subject
from eval.evaluate_composition import SyntheticModel


QUESTION = "请把带薪休息要求备成给主管看的说明"


class OfflineIntent:
    def classify(self, question, previous_knowledge):
        return IntentDecision("knowledge", "员工年假休假管理办法", ("年假",), True)


class OfflineComposition(SyntheticModel):
    def complete(self, stage, payload):
        if stage == "compose_supervisor":
            candidate = payload["candidates"][0]
            return {"title": "员工休假说明", "source_ids": [candidate["document_id"]],
                    "headings": [candidate["title"]], "focus": [candidate["title"]]}
        return super().complete(stage, payload)


class TimedIntent:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = []

    def classify(self, question, previous_knowledge):
        started = time.perf_counter()
        try:
            return self.delegate.classify(question, previous_knowledge)
        finally:
            self.calls.append(round((time.perf_counter() - started) * 1000))


class TimedComposition:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = []

    def complete(self, stage, payload):
        started = time.perf_counter()
        try:
            return self.delegate.complete(stage, payload)
        finally:
            self.calls.append({"stage": stage,
                               "elapsed_ms": round((time.perf_counter() - started) * 1000)})


def run(live: bool) -> dict:
    documents = DocumentRepository(":memory:", seed_demo=True)
    artifacts = ArtifactRepository(":memory:")
    conversations = ConversationRepository(":memory:")
    memories = UserMemoryRepository(":memory:")
    subject = Subject("employee-001", "demo-org", frozenset({"general"}),
                      Classification.INTERNAL)
    auth = LocalAuth([LocalAccount("employee", hash_password("synthetic-password"), subject)])
    intent = TimedIntent(OpenAIIntentClassifier(timeout=15.0) if live else OfflineIntent())
    model = TimedComposition(OpenAICollaborationModel() if live else OfflineComposition())
    app = create_app(auth, documents, artifacts=artifacts, conversations=conversations,
                     memories=memories, intent_classifier=intent,
                     collaboration_model=model, auto_memory=False)
    app.state.auto_react_enabled = False
    app.state.auto_reflection_enabled = False
    row = {"case": "synthetic_leave_document", "mode": "live" if live else "offline",
           "answer_status": None, "document_requested": False, "citation_count": 0,
           "compose_status": None, "compose_error": None, "docx_valid": False}
    try:
        with TestClient(app) as client:
            token = client.post("/api/v1/auth/login", json={
                "username": "employee", "password": "synthetic-password"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            started = time.perf_counter()
            answer_response = client.post("/api/v1/conversations/answers", headers=headers,
                                          json={"question": QUESTION})
            row["answer_ms"] = round((time.perf_counter() - started) * 1000)
            if answer_response.status_code != 200:
                row["answer_error"] = answer_response.json().get("code")
                return row
            answer = answer_response.json()
            row["answer_status"] = answer["answer"]["status"]
            row["document_requested"] = answer["answer"].get("document_requested", False)
            row["citation_count"] = len(answer["answer"]["citations"])
            if (row["answer_status"] != "evidence_ready" or not row["document_requested"]
                    or not row["citation_count"]):
                return row
            started = time.perf_counter()
            composed = client.post("/api/v1/artifacts/compose-run", headers=headers, json={
                "question": QUESTION, "request_id": "journey-eval-001",
                "conversation_id": answer["conversation_id"], "turn_index": 0})
            row["compose_ms"] = round((time.perf_counter() - started) * 1000)
            if composed.status_code != 200:
                row["compose_error"] = composed.json().get("code")
                return row
            task = composed.json()
            row["compose_status"] = task["status"]
            row["selected_source_count"] = len(task["artifact"]["source_ids"])
            row["run_stage"] = task["run"]["stage"] if task["run"] else None
            row["run_error"] = task["run"]["error_code"] if task["run"] else None
            if task["status"] != "completed":
                return row
            item = task["artifact"]
            started = time.perf_counter()
            confirmed = client.post(f"/api/v1/artifacts/{item['id']}/confirm", headers=headers,
                                    json={"outline": item["outline"],
                                          "draft_token": item["draft_token"]})
            row["confirm_ms"] = round((time.perf_counter() - started) * 1000)
            row["confirm_status"] = confirmed.status_code
            if confirmed.status_code != 200:
                row["confirm_error"] = confirmed.json().get("code")
                return row
            started = time.perf_counter()
            downloaded = client.get(f"/api/v1/artifacts/{item['id']}/download",
                                    headers=headers)
            row["download_ms"] = round((time.perf_counter() - started) * 1000)
            row["docx_valid"] = (downloaded.status_code == 200 and
                                 bool(Document(io.BytesIO(downloaded.content)).paragraphs))
            return row
    finally:
        row["intent_calls_ms"] = intent.calls
        row["composition_calls_ms"] = model.calls
        documents.close()
        artifacts._db.close()
        conversations.close()
        memories.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Call the configured external model on synthetic demo materials")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    row = run(args.live)
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(),
              "dataset": "synthetic_seed_demo_single_journey",
              "model_calls_are_external": args.live, "result": row,
              "limitations": "One sequential synthetic HTTP journey; no concurrency or stable p95. The offline mode checks plumbing with deterministic fake models. The live mode does not prove answer or document quality; provider token accounting and cost are not measured."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
