import io
import unittest

from docx import Document
from fastapi.testclient import TestClient
from pptx import Presentation

from app.api.artifacts import ArtifactRepository
from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.main import create_app
from app.security.policy import Classification, Subject, UserRole


class EditModel:
    def __init__(self):
        self.calls = []
        self.output = None

    def complete(self, stage, payload):
        self.calls.append((stage, payload))
        return self.output or {"section_index": 0, "revised_text": "先核对审批责任，再提交申请。",
                               "source_quote": payload["sections"][0]["evidence_text"][:12]}


class ArtifactEditTests(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        self.artifacts = ArtifactRepository(":memory:")
        self.model = EditModel()
        accounts = [
            LocalAccount("employee", hash_password("password"), Subject(
                "employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)),
            LocalAccount("admin", hash_password("password"), Subject(
                "admin-001", "demo-org", frozenset({"it"}), Classification.RESTRICTED,
                role=UserRole.SYSTEM_ADMIN)),
        ]
        self.client = TestClient(create_app(LocalAuth(accounts), self.documents,
                                            artifacts=self.artifacts, collaboration_model=self.model))
        self.employee = self._login("employee")
        self.admin = self._login("admin")

    def _login(self, name):
        token = self.client.post("/api/v1/auth/login", json={
            "username": name, "password": "password"}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def _create(self, kind="docx"):
        response = self.client.post("/api/v1/artifacts", headers=self.employee, json={
            "kind": kind, "title": "差旅整理", "goal": "说明审批", "source_ids": ["1"],
            "outline": ["审批流程"],
        })
        self.assertEqual(response.status_code, 201)
        return response.json()

    def _propose(self, item, conversation_id=None):
        return self.client.post("/api/v1/conversations/artifact-edits", headers=self.employee,
                                json={"artifact_id": item["id"], "instruction": "把审批流程写得更简洁",
                                      "conversation_id": conversation_id})

    def test_preview_confirm_history_and_export(self):
        for kind in ("docx", "pptx"):
            with self.subTest(kind=kind):
                item = self._create(kind)
                response = self._propose(item)
                self.assertEqual(response.status_code, 200)
                body = response.json()
                proposal = body["proposal"]
                self.assertEqual(self.model.calls[-1][0], "artifact_edit")
                self.assertEqual(proposal["before_text"], item["section_drafts"][0]["text"])
                self.assertEqual(self.client.get(f"/api/v1/artifacts/{item['id']}",
                                                 headers=self.employee).json()["draft_token"], item["draft_token"])
                history = self.client.get(f"/api/v1/conversations/{body['conversation_id']}",
                                          headers=self.employee).json()
                self.assertEqual(history["turns"][0]["answer"]["edit"]["id"], proposal["id"])
                path = f"/api/v1/artifact-edit-proposals/{proposal['id']}/confirm"
                saved = self.client.post(path, headers=self.employee)
                self.assertEqual(saved.status_code, 200)
                self.assertEqual(saved.json()["section_drafts"][0]["ai_text"], proposal["revised_text"])
                self.assertEqual(self.client.post(path, headers=self.employee).status_code, 200)
                confirmed = self.client.post(f"/api/v1/artifacts/{item['id']}/confirm",
                                             headers=self.employee, json={"outline": item["outline"],
                                                                          "draft_token": saved.json()["draft_token"]})
                self.assertEqual(confirmed.status_code, 200)
                content = self.client.get(f"/api/v1/artifacts/{item['id']}/download",
                                          headers=self.employee).content
                if kind == "docx":
                    self.assertIn(proposal["revised_text"], " ".join(p.text for p in Document(io.BytesIO(content)).paragraphs))
                else:
                    self.assertIn(proposal["revised_text"], " ".join(
                        shape.text for slide in Presentation(io.BytesIO(content)).slides
                        for shape in slide.shapes if shape.has_text_frame))

    def test_owner_revocation_and_version_checks(self):
        item = self._create()
        self.assertEqual(self.client.post("/api/v1/conversations/artifact-edits", headers=self.admin,
                                         json={"artifact_id": item["id"], "instruction": "修改审批流程"}).status_code, 404)
        self.assertEqual(self.model.calls, [])
        result = self._propose(item).json()
        proposal_id = result["proposal"]["id"]
        self.assertEqual(self.client.post(f"/api/v1/artifact-edit-proposals/{proposal_id}/confirm",
                                          headers=self.admin).status_code, 404)
        self.documents._db.execute("UPDATE documents SET version = 'v99' WHERE document_id = '1'")
        self.documents._db.commit()
        self.assertEqual(self.client.post(f"/api/v1/artifact-edit-proposals/{proposal_id}/confirm",
                                          headers=self.employee).json()["code"], "SOURCE_VERSION_CHANGED")
        history = self.client.get(f"/api/v1/conversations/{result['conversation_id']}",
                                  headers=self.employee).json()
        self.assertIsNone(history["turns"][0]["answer"]["edit"])

    def test_invalid_model_output_and_concurrent_change(self):
        item = self._create()
        self.model.output = {"section_index": 0, "revised_text": "虚构内容", "source_quote": "不存在的引句"}
        self.assertEqual(self._propose(item).json()["code"], "INVALID_EDIT")
        self.model.output = None
        proposal = self._propose(item).json()["proposal"]
        changed = self.client.post(f"/api/v1/artifacts/{item['id']}/notes", headers=self.employee,
                                   json={"draft_token": item["draft_token"],
                                         "notes": [{"heading": "审批流程", "text": "人工核对"}]})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/artifact-edit-proposals/{proposal['id']}/confirm",
                                          headers=self.employee).json()["code"], "DRAFT_CHANGED")
        current = self.client.get(f"/api/v1/artifacts/{item['id']}", headers=self.employee).json()
        self.assertNotIn("ai_text", current["section_drafts"][0])

    def test_visible_source_creates_personal_copy(self):
        response = self.client.post("/api/v1/conversations/artifact-edits", headers=self.employee,
                                    json={"source_document_id": "1", "instruction": "把审批说明写短一些"})
        self.assertEqual(response.status_code, 200)
        proposal = response.json()["proposal"]
        self.assertIn("编辑副本", proposal["artifact_title"])
        copy = self.client.get(f"/api/v1/artifacts/{proposal['artifact_id']}", headers=self.employee).json()
        self.assertEqual(copy["source_ids"], ["1"])
        self.assertEqual(copy["status"], "pending")
        self.assertEqual(self.client.get(f"/api/v1/artifacts/{proposal['artifact_id']}",
                                         headers=self.admin).status_code, 404)

    def test_failed_source_edit_does_not_leave_a_copy(self):
        before = len(self.client.get("/api/v1/artifacts", headers=self.employee).json())
        self.model.output = {"section_index": 0, "revised_text": "虚构内容", "source_quote": "不存在"}
        response = self.client.post("/api/v1/conversations/artifact-edits", headers=self.employee,
                                    json={"source_document_id": "1", "instruction": "把审批说明写短一些"})
        self.assertEqual(response.json()["code"], "INVALID_EDIT")
        self.assertEqual(len(self.client.get("/api/v1/artifacts", headers=self.employee).json()), before)


if __name__ == "__main__":
    unittest.main()
