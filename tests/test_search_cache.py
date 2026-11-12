"""Redis candidate cache must never become an authorization source."""

import json
import unittest

from redis.exceptions import ConnectionError

from app.api.documents import DocumentRepository
from app.api.search_cache import RedisSearchCache
from app.security.policy import Classification, Subject, UserRole, Visibility


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.fail = False

    def get(self, key):
        if self.fail:
            raise ConnectionError("offline")
        return self.values.get(key)

    def set(self, key, value, ex):
        if self.fail:
            raise ConnectionError("offline")
        self.values[key] = value


class SearchCacheTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.cache = RedisSearchCache("redis://unused", client=self.redis)
        self.docs = DocumentRepository(":memory:", seed_demo=True, search_cache=self.cache)
        self.employee = Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)
        self.manager = Subject("manager-001", "demo-org", frozenset({"product"}),
                               Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)

    def tearDown(self):
        self.docs.close()

    def test_hot_hit_contains_only_metadata_and_reloads_text(self):
        cold = self.docs.search_visible(self.employee, "差旅报销审批流程")
        hot = self.docs.search_visible(self.employee, "差旅报销审批流程")
        self.assertEqual([(x.document.document_id, x.text) for x in cold],
                         [(x.document.document_id, x.text) for x in hot])
        self.assertEqual(self.cache.stats()["hits"], 1)
        key, value = next(iter(self.redis.values.items()))
        self.assertNotIn("差旅报销审批流程", key)
        self.assertNotIn("员工提交报销申请", value)
        self.assertEqual(len(json.loads(value)[0]), 4)

    def test_cross_user_and_revocation_do_not_reuse_old_result(self):
        self.docs.search_visible(self.manager, "产品迭代路线图知识检索")
        self.assertFalse(self.docs.search_visible(self.employee, "产品迭代路线图知识检索"))
        self.assertEqual(len(self.redis.values), 2)
        self.docs.search_visible(self.employee, "差旅报销审批流程")
        self.assertTrue(self.docs.set_denied("demo-org", "1", self.employee.user_id, True))
        self.assertFalse(self.docs.search_visible(self.employee, "差旅报销审批流程"))

    def test_publish_and_revision_advance_epoch(self):
        before = self.docs._db.execute("SELECT version FROM search_policy_epoch").fetchone()[0]
        content = b"New project launch is planned."
        identifier = self.docs.create_upload(self.manager, title="New project", filename="new.txt",
                                             file_type="TXT", area="产品资料",
                                             visibility=Visibility.ORGANIZATION, content=content,
                                             extracted_text=content.decode())
        self.assertTrue(self.docs.publish(self.manager, identifier))
        self.assertEqual(self.docs._db.execute("SELECT version FROM search_policy_epoch").fetchone()[0], before + 1)
        revision = self.docs.create_revision(self.manager, identifier, filename="new.txt",
                                             file_type="TXT", content=b"Updated project plan.",
                                             extracted_text="Updated project plan.")
        self.assertEqual(self.docs.publish_revision(self.manager, identifier), revision)
        self.assertEqual(self.docs._db.execute("SELECT version FROM search_policy_epoch").fetchone()[0], before + 2)

    def test_tampered_candidate_and_redis_outage_fall_back(self):
        expected = self.docs.search_visible(self.employee, "差旅报销审批流程")
        key = next(iter(self.redis.values))
        self.redis.values[key] = json.dumps([[999999, "5", "v1.4", 99]])
        self.assertEqual(self.docs.search_visible(self.employee, "差旅报销审批流程"), expected)
        self.redis.fail = True
        self.assertEqual(self.docs.search_visible(self.employee, "差旅报销审批流程"), expected)
        self.assertGreaterEqual(self.cache.stats()["errors"], 1)


if __name__ == "__main__":
    unittest.main()
