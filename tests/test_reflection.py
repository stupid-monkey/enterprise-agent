import unittest

from fastapi.testclient import TestClient

from app.api.artifacts import ArtifactRepository
from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.main import create_app
from app.security.policy import Classification, Subject


EMPLOYEE = Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)


class CollaborationFake:
    def complete(self, stage, payload):
        if stage == "supervisor":
            return {"focus": payload["headings"]}
        if stage == "knowledge":
            return {"quotes": [payload["excerpts"][0][:24]]}
        return {"drafts": ["原始建议：" + payload["quotes"][0]]}


class ReflectionFake:
    def __init__(self):
        self.calls = []
        self.payloads = []
        self.unsupported = False

    def complete(self, stage, payload):
        self.calls.append(stage)
        self.payloads.append((stage, payload.copy()))
        if stage == "critic":
            if self.unsupported:
                return {"issues": [{"section_index": 0, "issue_type": "unsupported_claim",
                                     "severity": "major", "reason": "无对应依据",
                                     "evidence_quote": "不存在的依据"}]}
            if len(self.calls) == 1:
                quote = payload["sections"][0]["evidence"][0]["text"][:6]
                return {"issues": [{"section_index": 0, "issue_type": "unclear_wording",
                                     "severity": "minor", "reason": "表述需要收敛",
                                     "evidence_quote": quote}]}
            return {"issues": []}
        issue = payload["issues"][0]
        return {"revisions": [{"section_index": issue["section_index"],
                                "text": issue["evidence_quote"] + "（请人工核对）"}]}


class ReflectionTests(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        self.artifacts = ArtifactRepository(":memory:")
        self.reflection = ReflectionFake()
        auth = LocalAuth([LocalAccount("employee", hash_password("password"), EMPLOYEE)])
        self.client = TestClient(create_app(
            auth, self.documents, artifacts=self.artifacts,
            collaboration_model=CollaborationFake(), reflection_model=self.reflection))
        self.client.app.state.auto_reflection_enabled = False
        token = self.client.post("/api/v1/auth/login", json={
            "username": "employee", "password": "password"}).json()["access_token"]
        self.headers = {"Authorization": f"Bearer {token}"}

    def tearDown(self):
        self.documents.close()

    def create_collaborated(self):
        item = self.client.post("/api/v1/artifacts", headers=self.headers, json={
            "kind": "docx", "title": "差旅整理", "goal": "说明审批",
            "source_ids": ["1"], "outline": ["审批流程"],
        }).json()
        result = self.client.post(f"/api/v1/artifacts/{item['id']}/collaborate",
                                  headers=self.headers,
                                  json={"draft_token": item["draft_token"], "request_id": "collab-001"})
        self.assertEqual(result.status_code, 200)
        return result.json()

    def test_critic_reviser_loop_is_grounded_and_idempotent(self):
        item = self.create_collaborated()
        result = self.client.post(f"/api/v1/artifacts/{item['id']}/reflect",
                                  headers=self.headers,
                                  json={"draft_token": item["draft_token"], "request_id": "reflect-001"})
        self.assertEqual(result.status_code, 200)
        saved = result.json()
        self.assertEqual(self.reflection.calls, ["critic", "reviser", "critic"])
        self.assertEqual(saved["reflection_trace"][-1]["reason"], "NO_ISSUES")
        self.assertIn("请人工核对", saved["section_drafts"][0]["ai_text"])
        self.assertEqual(saved["section_drafts"][0]["ai_quote"], item["section_drafts"][0]["ai_quote"])
        replay = self.client.post(f"/api/v1/artifacts/{item['id']}/reflect",
                                  headers=self.headers,
                                  json={"draft_token": item["draft_token"], "request_id": "reflect-001"})
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(len(self.reflection.calls), 3)

    def test_reflection_uses_only_bounded_presentation_style(self):
        self.client.put("/api/v1/me/memories/answer_preference", headers=self.headers,
                        json={"value": "以后请分点回答"})
        item = self.create_collaborated()
        response = self.client.post(f"/api/v1/artifacts/{item['id']}/reflect",
                                    headers=self.headers,
                                    json={"draft_token": item["draft_token"],
                                          "request_id": "reflect-style"})
        self.assertEqual(response.status_code, 200)
        reviser = next(payload for stage, payload in self.reflection.payloads if stage == "reviser")
        self.assertEqual(reviser["presentation_style"], "bullets")
        self.assertNotIn("以后请分点回答", str(reviser))

    def test_unsupported_critic_issue_fails_without_mutating_draft(self):
        item = self.create_collaborated()
        self.reflection.unsupported = True
        result = self.client.post(f"/api/v1/artifacts/{item['id']}/reflect",
                                  headers=self.headers,
                                  json={"draft_token": item["draft_token"], "request_id": "reflect-002"})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(result.json()["code"], "REFLECTION_ISSUE_UNSUPPORTED")
        current = self.client.get(f"/api/v1/artifacts/{item['id']}", headers=self.headers).json()
        self.assertEqual(current["draft_token"], item["draft_token"])
        self.assertEqual(current["reflection_trace"], [])

    def test_reflection_requires_login_and_capability(self):
        item = self.client.post("/api/v1/artifacts", headers=self.headers, json={
            "kind": "docx", "title": "差旅整理", "goal": "说明审批",
            "source_ids": ["1"], "outline": ["审批流程"],
        }).json()
        self.assertEqual(self.client.post(f"/api/v1/artifacts/{item['id']}/reflect",
                                          json={"draft_token": item["draft_token"],
                                                "request_id": "reflect-unauth"}).status_code, 401)
        self.assertTrue(self.client.get("/api/v1/capabilities", headers=self.headers).json()["reflection"])

    def test_collaboration_automatically_runs_reflection_when_enabled(self):
        self.client.app.state.auto_reflection_enabled = True
        item = self.create_collaborated()
        # create_collaborated's collaboration response already includes the automatic reflection result.
        self.assertEqual(self.reflection.calls, ["critic", "reviser", "critic"])
        self.assertTrue(item["reflection_trace"])
        self.assertIn("请人工核对", item["section_drafts"][0]["ai_text"])


if __name__ == "__main__":
    unittest.main()
