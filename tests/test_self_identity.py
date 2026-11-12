import unittest

from app.api.self_identity import (account_question_kind, asks_own_identity,
                                   own_account_answer, own_identity_answer)
from app.security.policy import Classification, Subject, UserRole


class SelfIdentityTests(unittest.TestCase):
    def test_only_direct_self_identity_questions_match(self):
        for question in ("我是什么身份", "我的身份是什么？", "我是哪个角色？",
                         "我是什么岗位？", "我是谁？", "我在公司是什么身份？",
                         "我现在是什么身份？", "请问我这个账号是什么角色？"):
            with self.subTest(question=question):
                self.assertTrue(asks_own_identity(question))
        for question in ("员工身份认证制度是什么？", "我怎么验证身份？",
                         "我在公司的资料权限有哪些？", "公司经理是什么身份？"):
            with self.subTest(question=question):
                self.assertFalse(asks_own_identity(question))

    def test_account_role_is_distinct_from_remembered_job(self):
        subject = Subject("manager-001", "demo-org", frozenset({"product"}),
                          Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)
        answer = own_identity_answer(subject, [{"kind": "role", "value": "算法工程师"}])
        self.assertIn("系统身份**是：**部门负责人", answer)
        self.assertIn("岗位背景**是：算法工程师", answer)
        self.assertIn("不会改变权限", answer)

    def test_direct_account_questions_use_auth_context(self):
        expected = {
            "我的部门是什么？": "department",
            "我属于哪个部门": "department",
            "我现在登录的是哪个账号": "account",
            "我的用户ID是什么": "account",
            "我有什么权限": "access",
            "我在公司的资料权限有哪些": "access",
            "我能访问哪些资料": "access",
            "我有权看哪些文件": "access",
        }
        for question, kind in expected.items():
            with self.subTest(question=question):
                self.assertEqual(account_question_kind(question), kind)
        for question in ("我的权限申请流程是什么", "我怎么验证身份？",
                         "我在公司的资料权限制度有哪些", "我能查看产品发布检查清单吗",
                         "我可以访问哪些资料中提到的预算？"):
            with self.subTest(question=question):
                self.assertIsNone(account_question_kind(question))

    def test_access_answer_lists_only_authorized_titles_and_explains_limits(self):
        subject = Subject("employee-001", "demo-org", frozenset({"general"}),
                          Classification.INTERNAL)
        answer = own_account_answer(subject, "我能访问哪些资料", [],
                                    ["员工手册", "机密[点击](https://example.com)"])
        self.assertIn("**2** 份", answer)
        self.assertIn("员工手册", answer)
        self.assertIn(r"机密\[点击\]", answer)
        self.assertIn("下载和用于生成仍需另行授权", answer)
        self.assertIn("general", own_account_answer(subject, "我的部门是什么", []))
        self.assertIn("employee-001", own_account_answer(subject, "我的账号是什么", []))
