import json
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.file_access_questions import parse_file_access_query
from app.api.main import create_app
from app.security.policy import Classification, Subject, UserRole


class FileAccessQuestionTests(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        self.client = TestClient(create_app(LocalAuth([
            LocalAccount("employee", hash_password("password"), Subject(
                "employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)),
            LocalAccount("manager", hash_password("password"), Subject(
                "manager-001", "demo-org", frozenset({"product"}),
                Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)),
        ]), self.documents))
        self.client.app.state.general_enabled = False

    def tearDown(self):
        self.documents.close()

    def headers(self, username):
        token = self.client.post("/api/v1/auth/login", json={
            "username": username, "password": "password"}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def ask(self, username, question, *, selected=None, stream=False):
        suffix = "/stream" if stream else ""
        body = {"question": question, "mode": "extract"}
        if selected is not None:
            body["source_document_id"] = selected
        response = self.client.post(f"/api/v1/conversations/answers{suffix}",
                                    headers=self.headers(username), json=body)
        if response.status_code != 200:
            return response, None
        if stream:
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ")]
            self.assertEqual(events[0]["intent"], "file_access")
            self.assertEqual(events[-1]["type"], "done")
            return response, events[-1]
        return response, response.json()

    def test_parser_matches_permission_questions_only(self):
        for question in ("我能查看这份文件吗？", "我有权限下载这个文档吗",
                         "我能否下载《差旅报销管理制度》？", "《差旅报销管理制度》可以下载吗？"):
            with self.subTest(question=question):
                self.assertIsNotNone(parse_file_access_query(question))
        for question in ("我怎么申请下载权限？", "下载这份文件的流程是什么？",
                         "我能查看这份文件中的审批规则吗？", "请总结这份文件",
                         "我能访问哪些资料？"):
            with self.subTest(question=question):
                self.assertIsNone(parse_file_access_query(question))

    def test_selected_read_and_download_without_original(self):
        for stream in (False, True):
            _, viewed = self.ask("employee", "我能查看这份文件吗？", selected="1", stream=stream)
            self.assertIn("可以查看", viewed["answer"]["paragraphs"][0])
            self.assertEqual(viewed["answer"]["citations"], [])
            _, downloaded = self.ask("employee", "我能否下载这份文件？", selected="1", stream=stream)
            self.assertIn("没有可下载的原件", downloaded["answer"]["paragraphs"][0])

    def test_title_lookup_never_reveals_hidden_document(self):
        hidden = "产品发布检查清单"
        for stream in (False, True):
            response, result = self.ask("employee", f"我能查看《{hidden}》吗？", stream=stream)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(hidden, result["answer"]["paragraphs"][0])
            self.assertIn("当前可见", result["answer"]["paragraphs"][0])
            response, result = self.ask("employee", "我能查看这份文件吗？", selected="5", stream=stream)
            self.assertEqual(response.status_code, 404)
            self.assertIsNone(result)

    def test_download_authorization_and_revocation(self):
        manager = self.headers("manager")
        created = self.client.post("/api/v1/documents/upload", headers=manager,
            data={"title": "权限检查样本", "area": "制度规范", "visibility": "内部公开"},
            files={"file": ("sample.txt", b"sample source", "text/plain")})
        self.assertEqual(created.status_code, 201)
        document_id = created.json()["id"]
        self.assertEqual(self.client.post(f"/api/v1/documents/{document_id}/publish",
                                          headers=manager).status_code, 204)
        for stream in (False, True):
            _, result = self.ask("employee", "我能下载《权限检查样本》吗？", stream=stream)
            self.assertIn("可以下载", result["answer"]["paragraphs"][0])
        self.assertTrue(self.documents.set_denied("demo-org", document_id, "employee-001", True))
        response, result = self.ask("employee", "我能下载这份文件吗？", selected=document_id)
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(result)
        _, hidden = self.ask("employee", "我能下载《权限检查样本》吗？")
        self.assertNotIn("权限检查样本", hidden["answer"]["paragraphs"][0])

    def test_stream_stops_if_access_is_revoked_before_output(self):
        original_get = self.documents.get_visible

        def revoke_after_first_read(subject, document_id):
            record = original_get(subject, document_id)
            if record is not None:
                self.documents.set_denied("demo-org", document_id, "employee-001", True)
            return record

        with mock.patch.object(self.documents, "get_visible", side_effect=revoke_after_first_read):
            response = self.client.post("/api/v1/conversations/answers/stream",
                headers=self.headers("employee"), json={
                    "question": "我能查看这份文件吗？", "mode": "extract", "source_document_id": "1"})
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ")]
        self.assertEqual([event["type"] for event in events], ["meta", "error"])
        self.assertEqual(events[-1]["code"], "SOURCE_UNAVAILABLE")
        self.assertEqual(self.client.get("/api/v1/conversations",
                                         headers=self.headers("employee")).json(), [])

    def test_history_masks_selected_file_permission_after_revocation(self):
        _, response = self.ask("employee", "我能查看这份文件吗？", selected="1")
        conversation_id = response["conversation_id"]
        self.assertEqual(response["answer"]["status"], "file_access")
        self.assertTrue(self.documents.set_denied("demo-org", "1", "employee-001", True))
        history = self.client.get(f"/api/v1/conversations/{conversation_id}",
                                  headers=self.headers("employee")).json()
        self.assertEqual(history["turns"][0]["answer"]["status"], "source_unavailable")
        self.assertNotIn("差旅报销管理制度", str(history))


if __name__ == "__main__":
    unittest.main()
