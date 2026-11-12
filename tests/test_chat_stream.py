import json
import unittest

from fastapi.testclient import TestClient

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.general_chat import OpenAIGeneralResponder
from app.api.intent import route_intent
from app.api.main import create_app
from app.security.policy import Classification, Subject, UserRole


class FakeGeneralResponder:
    def __init__(self):
        self.calls = []

    def stream(self, question, history, profile):
        self.calls.append((question, history, profile))
        yield "Python "
        yield "是一种编程语言。"


class ChatStreamTests(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        accounts = LocalAuth([
            LocalAccount("manager", hash_password("password"), Subject(
                "manager-001", "demo-org", frozenset({"product"}), Classification.CONFIDENTIAL,
                role=UserRole.DEPARTMENT_MANAGER)),
            LocalAccount("employee", hash_password("password"), Subject(
                "employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)),
        ])
        self.responder = FakeGeneralResponder()
        self.client = TestClient(create_app(accounts, self.documents, general_responder=self.responder))

    def tearDown(self):
        self.documents.close()

    def headers(self, user="manager"):
        token = self.client.post("/api/v1/auth/login", json={
            "username": user, "password": "password",
        }).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def stream(self, headers, question, conversation_id=None, workflow_run_id=None, area=None):
        response = self.client.post("/api/v1/conversations/answers/stream", headers=headers, json={
            "question": question, "mode": "extract", "conversation_id": conversation_id,
            "workflow_run_id": workflow_run_id, "area": area,
        })
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        return response, events

    def test_greeting_and_general_followup_share_one_conversation(self):
        headers = self.headers()
        response, first = self.stream(headers, "你好")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["type"] for event in first], ["meta", "delta", "done"])
        self.assertEqual(first[-1]["answer"]["status"], "general")
        conversation_id = first[-1]["conversation_id"]
        _, second = self.stream(headers, "Python 是什么？", conversation_id)
        self.assertEqual(second[-1]["conversation_id"], conversation_id)
        self.assertEqual("".join(item["text"] for item in second if item["type"] == "delta"),
                         second[-1]["answer"]["paragraphs"][0])
        detail = self.client.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()
        self.assertEqual([turn["mode"] for turn in detail["turns"]], ["general", "general"])
        self.assertEqual(len(self.client.get("/api/v1/conversations", headers=headers).json()), 1)
        self.assertEqual(self.responder.calls[0][1][0][0], "你好")

    def test_self_introduction_is_general_and_stays_in_conversation_without_model(self):
        headers = self.headers()
        _, greeting = self.stream(headers, "你好")
        conversation_id = greeting[-1]["conversation_id"]
        _, introduction = self.stream(headers, "我是此公司的算法工程师", conversation_id)
        self.assertEqual(introduction[0]["intent"], "general")
        self.assertEqual(introduction[-1]["type"], "done")
        self.assertEqual(introduction[-1]["conversation_id"], conversation_id)
        self.assertEqual(introduction[-1]["answer"]["status"], "general")
        self.assertIn("岗位背景", introduction[-1]["answer"]["paragraphs"][0])
        self.assertEqual(self.responder.calls, [])
        detail = self.client.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()
        self.assertEqual([turn["mode"] for turn in detail["turns"]], ["general", "general"])

    def test_knowledge_stays_authorized_and_general_does_not_get_sources(self):
        headers = self.headers()
        _, knowledge = self.stream(headers, "产品发布前有哪些检查项？")
        self.assertEqual(knowledge[-1]["answer"]["citations"][0]["document_id"], "5")
        conversation_id = knowledge[-1]["conversation_id"]
        _, general = self.stream(headers, "你好", conversation_id)
        self.assertEqual(general[-1]["answer"]["status"], "general")
        self.assertEqual(general[-1]["answer"]["citations"], [])
        _, unrelated = self.stream(headers, "病假证明怎么办？", conversation_id)
        self.assertEqual(unrelated[-1]["answer"]["status"], "no_evidence")
        self.assertEqual(self.responder.calls, [])

    def test_product_release_paraphrases_use_local_knowledge(self):
        for question in ("我要是想发布一个产品我需要检查什么",
                         "一个产品上线之前要核对哪些项目",
                         "发布新品前需检查哪些事项"):
            with self.subTest(question=question):
                _, events = self.stream(self.headers(), question)
                self.assertEqual(events[0]["intent"], "knowledge")
                self.assertEqual(events[-1]["type"], "done")
                self.assertEqual(events[-1]["answer"]["citations"][0]["document_id"], "5")
                self.assertIn("功能验收", events[-1]["answer"]["paragraphs"][0])
        _, restricted = self.stream(self.headers("employee"), "发布新品前需检查哪些事项")
        self.assertEqual(restricted[0]["intent"], "knowledge")
        self.assertEqual(restricted[-1]["answer"]["status"], "no_evidence")
        self.assertEqual(restricted[-1]["answer"]["citations"], [])
        _, title_matched = self.stream(self.headers(), "季度路线图的重点是什么？")
        self.assertEqual(title_matched[0]["intent"], "knowledge")
        self.assertEqual(self.responder.calls, [])

    def test_other_user_cannot_append_to_conversation(self):
        manager = self.headers()
        conversation_id = self.stream(manager, "你好")[1][-1]["conversation_id"]
        employee = self.headers("employee")
        response, _ = self.stream(employee, "你好", conversation_id)
        self.assertEqual(response.status_code, 404)
        detail = self.client.get(f"/api/v1/conversations/{conversation_id}", headers=manager).json()
        self.assertEqual(len(detail["turns"]), 1)

    def test_failed_knowledge_stream_can_resume_without_duplicate_turn(self):
        headers = self.headers()
        question = "产品发布前有哪些检查项？"
        repository = self.client.app.state.workflow_runs
        if repository is None:
            from app.api.workflow_runs import WorkflowRunRepository
            repository = WorkflowRunRepository(":memory:")
            self.client.app.state.workflow_runs = repository
        original_resume = repository.resume
        def fail_once(*args):
            repository.resume = original_resume
            raise RuntimeError("temporary failure")
        repository.resume = fail_once
        response, failed = self.stream(headers, question)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["type"] for event in failed], ["meta", "error"])
        run_id = failed[0]["workflow_run_id"]
        self.assertTrue(run_id)
        self.assertEqual(self.client.get("/api/v1/conversations", headers=headers).json(), [])
        run_url = f"/api/v1/conversations/workflow-runs/{run_id}"
        self.assertEqual(self.client.get(run_url, headers=headers).json(), {"conversation_id": None})

        _, resumed = self.stream(headers, question, workflow_run_id=run_id)
        self.assertEqual(resumed[-1]["type"], "done")
        conversation_id = resumed[-1]["conversation_id"]
        self.assertEqual(self.client.get(run_url, headers=headers).json(), {"conversation_id": conversation_id})
        _, replayed = self.stream(headers, question, workflow_run_id=run_id)
        self.assertEqual(replayed[-1]["conversation_id"], conversation_id)
        detail = self.client.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()
        self.assertEqual(len(detail["turns"]), 1)

        employee = self.headers("employee")
        self.assertEqual(self.client.get(run_url, headers=employee).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/conversations/workflow-runs/missing", headers=headers).status_code, 404)
        self.assertEqual(self.stream(employee, question, workflow_run_id=run_id)[0].status_code, 404)
        self.assertEqual(self.stream(headers, "不同问题", workflow_run_id=run_id)[0].status_code, 404)
        self.assertEqual(self.stream(headers, question, workflow_run_id=run_id, area="产品资料")[0].status_code, 404)
        self.assertEqual(self.stream(headers, question, conversation_id=conversation_id,
                                     workflow_run_id=run_id)[0].status_code, 404)

        self.documents.set_denied("demo-org", "5", "manager-001", True)
        _, revoked = self.stream(headers, question, workflow_run_id=run_id)
        self.assertEqual(revoked[-1]["answer"]["status"], "no_evidence")
        self.assertEqual(revoked[-1]["answer"]["citations"], [])
        detail = self.client.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()
        self.assertEqual(len(detail["turns"]), 1)
        self.assertEqual(detail["turns"][0]["answer"]["status"], "source_unavailable")

    def test_model_auth_failure_is_reported_without_saving_partial_turn(self):
        class AuthenticationError(Exception):
            pass

        class BrokenResponder:
            def stream(self, question, history, profile):
                raise AuthenticationError("secret diagnostic")
                yield ""

        self.client.app.state.general_responder = BrokenResponder()
        headers = self.headers()
        response, events = self.stream(headers, "Python 是什么？")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(events[-1], {"type": "error", "code": "MODEL_AUTH_FAILED"})
        self.assertNotIn("secret diagnostic", response.text)
        self.assertEqual(self.client.get("/api/v1/conversations", headers=headers).json(), [])

    def test_intent_uses_explicit_context_only(self):
        self.assertEqual(route_intent("你好"), "general")
        self.assertEqual(route_intent("你好", area="产品资料"), "general")
        self.assertEqual(route_intent("我是此公司的算法工程师"), "general")
        self.assertEqual(route_intent("我是本公司的算法工程师", area="产品资料"), "general")
        self.assertEqual(route_intent("什么是项目管理？"), "general")
        self.assertEqual(route_intent("公司法是什么？"), "general")
        self.assertEqual(route_intent("怎么写一份文档？"), "general")
        self.assertEqual(route_intent("如何用 Python 发布软件包？",
                                      visible_titles=("产品发布检查清单",)), "general")
        self.assertEqual(route_intent("季度路线图的重点是什么？",
                                      visible_titles=("产品 Q4 迭代路线图",)), "knowledge")
        self.assertEqual(route_intent("发布新品前需检查哪些事项？",
                                      visible_titles=("产品发布检查清单",)), "knowledge")
        self.assertEqual(route_intent("这份文档里的发布流程是什么？"), "knowledge")
        self.assertEqual(route_intent("病假证明怎么办？", previous_knowledge="产品发布前有哪些检查项？"), "knowledge")
        self.assertEqual(route_intent("那需要谁审批？", previous_knowledge="出差报销需要哪些审批？"), "knowledge")

    def test_openai_adapter_streams_text_without_enterprise_history(self):
        class FakeResponses:
            def create(self, **kwargs):
                self.request = kwargs
                return [type("Event", (), {"type": "response.output_text.delta", "delta": "回答"})()]

        class FakeClient:
            responses = FakeResponses()

        client = FakeClient()
        self.assertEqual(list(OpenAIGeneralResponder(client, "test-model").stream("你好", [])), ["回答"])
        self.assertTrue(client.responses.request["stream"])
        self.assertFalse(client.responses.request["store"])
        self.assertEqual(client.responses.request["input"][-1]["content"], "你好")

    def test_openai_adapter_sends_explicit_profile_as_user_context(self):
        class FakeResponses:
            def create(self, **kwargs):
                self.request = kwargs
                return [type("Event", (), {"type": "response.output_text.delta", "delta": "回答"})()]

        class FakeClient:
            responses = FakeResponses()

        client = FakeClient()
        profile = {"role": "设计师", "answer_preference": "简短回答"}
        list(OpenAIGeneralResponder(client, "test-model").stream("我做什么工作？", [], profile))
        messages = client.responses.request["input"]
        self.assertEqual([item["role"] for item in messages], ["system", "user", "user"])
        self.assertIn("设计师", messages[1]["content"])
        self.assertNotIn("设计师", messages[0]["content"])

    def test_compatible_chat_completions_adapter_streams_text(self):
        class FakeCompletions:
            def create(self, **kwargs):
                self.request = kwargs
                return [
                    type("Chunk", (), {"choices": [type("Choice", (), {
                        "delta": type("Delta", (), {"content": text})(),
                    })()]})()
                    for text in ("兼容", "回答")
                ]

        class FakeClient:
            chat = type("Chat", (), {"completions": FakeCompletions()})()

        client = FakeClient()
        responder = OpenAIGeneralResponder(client, "provider-model", "chat_completions")
        self.assertEqual(list(responder.stream("解释单元测试", [("上文", "回复")])), ["兼容", "回答"])
        self.assertEqual(client.chat.completions.request["model"], "provider-model")
        self.assertTrue(client.chat.completions.request["stream"])
        self.assertEqual([item["role"] for item in client.chat.completions.request["messages"]],
                         ["system", "user", "assistant", "user"])
        with self.assertRaises(ValueError):
            OpenAIGeneralResponder(client, "provider-model", "unknown")


if __name__ == "__main__":
    unittest.main()
