"""A saved answer can seed a document without trusting client-supplied sources."""

import io
import unittest

from docx import Document
from fastapi.testclient import TestClient

from app.api.artifacts import ArtifactRepository
from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.intent_model import IntentDecision
from app.api.main import create_app
from app.security.policy import Classification, Subject
from tests.test_artifact_composition import FakeCollaborationModel


QUESTION = "请把带薪休息要求备成给主管看的说明"


class JourneyIntentClassifier:
    def classify(self, question, previous_knowledge):
        return IntentDecision("knowledge", "员工年假休假管理办法", ("年假",), True)


class JourneyModel(FakeCollaborationModel):
    def __init__(self, fail_once=False):
        super().__init__()
        self.fail_once = fail_once

    def complete(self, stage, payload):
        if stage == "compose_supervisor":
            self.calls.append(stage)
            candidate = payload["candidates"][0]
            return {"title": "员工休假说明", "source_ids": [candidate["document_id"]],
                    "headings": [candidate["title"]], "focus": [candidate["title"]]}
        if stage == "knowledge" and self.fail_once:
            self.calls.append(stage)
            self.fail_once = False
            raise RuntimeError("synthetic failure")
        return super().complete(stage, payload)


class AnswerDocumentJourneyTests(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        self.artifacts = ArtifactRepository(":memory:")
        self.model = JourneyModel()
        self.employee = Subject("employee-001", "demo-org", frozenset({"general"}),
                                Classification.INTERNAL)
        auth = LocalAuth([LocalAccount("employee", hash_password("password"), self.employee)])
        self.client = TestClient(create_app(auth, self.documents, artifacts=self.artifacts,
                                            intent_classifier=JourneyIntentClassifier(),
                                            collaboration_model=self.model, auto_memory=False))
        self.client.app.state.auto_react_enabled = False
        token = self.client.post("/api/v1/auth/login", json={
            "username": "employee", "password": "password"}).json()["access_token"]
        self.headers = {"Authorization": f"Bearer {token}"}

    def tearDown(self):
        self.documents.close()
        self.artifacts._db.close()

    def answer(self):
        response = self.client.post("/api/v1/conversations/answers", headers=self.headers,
                                    json={"question": QUESTION})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def compose(self, answer, **overrides):
        body = {"question": QUESTION, "request_id": "journey-001",
                "conversation_id": answer["conversation_id"], "turn_index": 0}
        body.update(overrides)
        return self.client.post("/api/v1/artifacts/compose-run", headers=self.headers, json=body)

    def test_rewritten_answer_produces_reviewable_docx_from_same_source(self):
        self.assertEqual(self.documents.search_visible(self.employee, QUESTION, limit=12), [])
        answered = self.answer()
        self.assertEqual(answered["answer"]["status"], "evidence_ready")
        self.assertTrue(answered["answer"]["document_requested"])
        self.assertEqual([item["document_id"] for item in answered["answer"]["citations"]], ["2"])

        result = self.compose(answered)
        self.assertEqual(result.status_code, 200)
        task = result.json()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["artifact"]["source_ids"], ["2"])
        self.assertEqual([step["agent"] for step in task["run"]["trace"]],
                         ["supervisor", "knowledge", "document", "validator"])
        self.assertTrue(task["artifact"]["section_drafts"][0]["ai_text"])
        replay = self.compose(answered).json()
        self.assertEqual(replay["artifact"]["id"], task["artifact"]["id"])
        self.assertEqual(self.model.calls, ["compose_supervisor", "knowledge", "document"])
        another_answer = self.answer()
        self.assertEqual(self.compose(another_answer).json()["code"], "COMPOSITION_REQUEST_CONFLICT")

        item = task["artifact"]
        confirmed = self.client.post(f"/api/v1/artifacts/{item['id']}/confirm",
                                     headers=self.headers,
                                     json={"outline": item["outline"],
                                           "draft_token": item["draft_token"]})
        self.assertEqual(confirmed.status_code, 200)
        download = self.client.get(f"/api/v1/artifacts/{item['id']}/download",
                                   headers=self.headers)
        self.assertEqual(download.status_code, 200)
        self.assertTrue(Document(io.BytesIO(download.content)).paragraphs)

    def test_reference_must_match_owned_saved_answer(self):
        answered = self.answer()
        for overrides, status in (({"conversation_id": "not-owned"}, 404),
                                  ({"question": "员工年假标准"}, 409),
                                  ({"turn_index": 1}, 404),
                                  ({"conversation_id": None}, 422)):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.compose(answered, **overrides).status_code, status)
        self.assertEqual(self.model.calls, [])

    def test_answer_without_evidence_cannot_seed_document(self):
        self.documents.set_denied("demo-org", "2", self.employee.user_id, True)
        answered = self.answer()
        self.assertEqual(answered["answer"]["status"], "no_evidence")
        self.assertEqual(answered["answer"]["citations"], [])
        response = self.compose(answered)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "COMPOSITION_NO_EVIDENCE")
        self.assertEqual(self.model.calls, [])

    def test_revocation_or_new_version_blocks_answer_handoff(self):
        for change in ("deny", "version"):
            with self.subTest(change=change):
                answered = self.answer()
                if change == "deny":
                    self.documents.set_denied("demo-org", "2", self.employee.user_id, True)
                else:
                    self.documents._db.execute(
                        "UPDATE documents SET version = 'v99' WHERE document_id = '2'")
                    self.documents._db.commit()
                response = self.compose(answered)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "COMPOSITION_NO_EVIDENCE")
                self.assertEqual(self.model.calls, [])
                if change == "deny":
                    self.documents.set_denied("demo-org", "2", self.employee.user_id, False)
                else:
                    self.documents._db.execute(
                        "UPDATE documents SET version = 'v1' WHERE document_id = '2'")
                    self.documents._db.commit()

    def test_failed_run_keeps_draft_and_resumes_explicitly(self):
        self.model.fail_once = True
        answered = self.answer()
        response = self.compose(answered)
        self.assertEqual(response.status_code, 200)
        task = response.json()
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["run"]["stage"], "knowledge")
        self.assertEqual(task["artifact"]["status"], "pending")
        item = task["artifact"]
        resumed = self.client.post(f"/api/v1/artifacts/{item['id']}/collaborate",
                                   headers=self.headers,
                                   json={"draft_token": item["draft_token"],
                                         "request_id": "journey-001"})
        self.assertEqual(resumed.status_code, 200)
        self.assertTrue(resumed.json()["section_drafts"][0]["ai_text"])


if __name__ == "__main__":
    unittest.main()
