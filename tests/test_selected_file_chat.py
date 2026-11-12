import json
import unittest

from fastapi.testclient import TestClient

from app.api.artifacts import ArtifactRepository
from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.main import create_app
from app.security.policy import Classification, Subject


class FileModel:
    def __init__(self):
        self.calls = []

    def complete(self, stage, payload):
        self.calls.append((stage, payload))
        if stage == "file_intent":
            if payload["question"] in ("我是否能看到它？", "我能阅读这些文件吗？"):
                return {"intent": "permission_read"}
            if payload["question"] == "我能不能取得它的原件？":
                return {"intent": "permission_download"}
            return {"intent": "edit" if "修改" in payload["question"] else "answer"}
        if stage == "file_answer":
            quote = payload["text"][:12]
            return {"answer": quote, "quote": quote}
        if stage == "artifact_edit":
            return {"section_index": 0, "revised_text": "请先核对审批责任，再提交申请。",
                    "source_quote": payload["sections"][0]["evidence_text"][:12]}
        raise AssertionError(stage)


class SelectedFileChatTests(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        self.artifacts = ArtifactRepository(":memory:")
        self.model = FileModel()
        auth = LocalAuth([
            LocalAccount("employee", hash_password("password"), Subject(
                "employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)),
            LocalAccount("other", hash_password("password"), Subject(
                "other-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)),
        ])
        self.client = TestClient(create_app(auth, self.documents, artifacts=self.artifacts,
                                            collaboration_model=self.model))
        self.employee = self.login("employee")
        self.other = self.login("other")

    def login(self, username):
        token = self.client.post("/api/v1/auth/login", json={
            "username": username, "password": "password"}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def stream(self, question, target, headers=None, conversation_id=None):
        response = self.client.post("/api/v1/conversations/answers/stream",
                                    headers=headers or self.employee,
                                    json={"question": question, "conversation_id": conversation_id,
                                          "mode": "extract", **target})
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ")]
        return response, events

    def test_selected_document_only_and_followup_same_conversation(self):
        response, first = self.stream("请概括这份文件", {"source_document_id": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(first[0]["intent"], "file_answer")
        self.assertEqual(first[-1]["answer"]["citations"][0]["document_id"], "1")
        self.assertEqual(self.model.calls[1][0], "file_answer")
        self.assertNotIn("source_ids", self.model.calls[1][1])
        self.assertNotIn("年假", self.model.calls[1][1]["text"])
        self.assertEqual(first[-1]["answer"]["paragraphs"][0],
                         "".join(event["text"] for event in first if event["type"] == "delta"))
        conversation_id = first[-1]["conversation_id"]
        _, second = self.stream("请总结主要内容", {"source_document_id": "1"},
                                conversation_id=conversation_id)
        self.assertEqual(second[-1]["conversation_id"], conversation_id)
        history = self.client.get(f"/api/v1/conversations/{conversation_id}", headers=self.employee).json()
        self.assertEqual([turn["mode"] for turn in history["turns"]], ["file_answer", "file_answer"])

    def test_denied_target_never_reaches_model(self):
        response, _ = self.stream("请概括这份文件", {"source_document_id": "5"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.model.calls, [])

    def test_model_paraphrase_fallback_only_classifies_acl_action(self):
        _, read = self.stream("我是否能看到它？", {"source_document_id": "1"})
        self.assertEqual(read[0]["intent"], "file_access")
        self.assertIn("可以查看", read[-1]["answer"]["paragraphs"][0])
        _, download = self.stream("我能不能取得它的原件？", {"source_document_id": "1"})
        self.assertIn("没有可下载的原件", download[-1]["answer"]["paragraphs"][0])
        self.assertEqual([stage for stage, _ in self.model.calls],
                         ["file_intent", "file_intent"])

    def test_model_cannot_turn_plural_question_into_single_file_permission(self):
        _, events = self.stream("我能阅读这些文件吗？", {"source_document_id": "1"})
        self.assertEqual(events[0]["intent"], "file_answer")
        self.assertEqual(events[-1]["answer"]["status"], "no_evidence")

    def test_selected_file_receives_only_current_users_bounded_style(self):
        path = "/api/v1/me/memories/answer_preference"
        self.client.put(path, headers=self.employee, json={"value": "以后请分点回答"})
        self.stream("请概括这份文件", {"source_document_id": "1"})
        payload = next(payload for stage, payload in self.model.calls if stage == "file_answer")
        self.assertEqual(payload["presentation_style"], "bullets")
        self.assertNotIn("以后请分点回答", str(payload))
        self.model.calls.clear()
        self.stream("请概括这份文件", {"source_document_id": "1"}, headers=self.other)
        payload = next(payload for stage, payload in self.model.calls if stage == "file_answer")
        self.assertNotIn("presentation_style", payload)

    def test_edit_still_requires_confirmation(self):
        response, events = self.stream("请修改审批流程，使表述更简洁", {"source_document_id": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(events[0]["intent"], "edit")
        proposal = events[-1]["answer"]["edit"]
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(self.model.calls[-1][0], "artifact_edit")
        self.assertEqual(self.client.post(
            f"/api/v1/artifact-edit-proposals/{proposal['id']}/confirm",
            headers=self.other).status_code, 404)

    def test_revocation_masks_history(self):
        _, events = self.stream("请概括这份文件", {"source_document_id": "1"})
        conversation_id = events[-1]["conversation_id"]
        self.documents.set_denied("demo-org", "1", "employee-001", True)
        history = self.client.get(f"/api/v1/conversations/{conversation_id}", headers=self.employee).json()
        self.assertEqual(history["turns"][0]["answer"]["status"], "source_unavailable")
        self.assertEqual(self.stream("请概括这份文件", {"source_document_id": "1"})[0].status_code, 404)

    def test_draft_answer_is_hidden_after_draft_changes(self):
        created = self.client.post("/api/v1/artifacts", headers=self.employee, json={
            "kind": "docx", "title": "差旅整理", "goal": "说明审批",
            "source_ids": ["1"], "outline": ["审批流程"],
        }).json()
        target = {"artifact_id": created["id"]}
        _, answered = self.stream("请概括这份文件", target)
        conversation_id = answered[-1]["conversation_id"]
        self.assertEqual(answered[-1]["answer"]["target_artifact_id"], created["id"])
        _, edited = self.stream("请修改审批流程", target, conversation_id=conversation_id)
        proposal_id = edited[-1]["answer"]["edit"]["id"]
        self.assertEqual(self.client.post(
            f"/api/v1/artifact-edit-proposals/{proposal_id}/confirm",
            headers=self.employee).status_code, 200)
        history = self.client.get(f"/api/v1/conversations/{conversation_id}", headers=self.employee).json()
        self.assertEqual(history["turns"][0]["answer"]["status"], "source_unavailable")


if __name__ == "__main__":
    unittest.main()
