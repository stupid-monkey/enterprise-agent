import unittest

from app.security.policy import (
    Classification,
    DocumentPolicy,
    DocumentState,
    ResourceAction,
    Subject,
    Visibility,
    authorize,
)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.subject = Subject(
            user_id="employee-1",
            org_id="org-a",
            department_ids=frozenset({"finance"}),
            clearance=Classification.DEPARTMENT,
        )
        self.document = DocumentPolicy(
            document_id="policy-1",
            org_id="org-a",
            state=DocumentState.PUBLISHED,
            classification=Classification.DEPARTMENT,
            visibility=Visibility.DEPARTMENT,
            department_ids=frozenset({"finance"}),
        )

    def test_matching_department_can_read(self):
        self.assertTrue(authorize(self.subject, self.document, ResourceAction.READ).allowed)

    def test_cross_organization_is_denied(self):
        subject = Subject("employee-1", "org-b", frozenset({"finance"}), Classification.RESTRICTED)
        self.assertEqual(authorize(subject, self.document, ResourceAction.READ).reason, "organization_mismatch")

    def test_explicit_deny_overrides_department_membership(self):
        document = DocumentPolicy(**{**self.document.__dict__, "denied_user_ids": frozenset({"employee-1"})})
        self.assertEqual(authorize(self.subject, document, ResourceAction.READ).reason, "explicit_deny")

    def test_draft_is_not_visible(self):
        document = DocumentPolicy(**{**self.document.__dict__, "state": DocumentState.DRAFT})
        self.assertEqual(authorize(self.subject, document, ResourceAction.READ).reason, "not_published")

    def test_insufficient_clearance_is_denied(self):
        document = DocumentPolicy(**{**self.document.__dict__, "classification": Classification.CONFIDENTIAL})
        self.assertEqual(authorize(self.subject, document, ResourceAction.READ).reason, "insufficient_clearance")

    def test_other_department_is_denied(self):
        document = DocumentPolicy(**{**self.document.__dict__, "department_ids": frozenset({"product"})})
        self.assertEqual(authorize(self.subject, document, ResourceAction.READ).reason, "outside_department")

    def test_allowlist_and_inactive_subject(self):
        document = DocumentPolicy(**{**self.document.__dict__, "visibility": Visibility.ALLOWLIST, "allowed_user_ids": frozenset({"employee-2"})})
        self.assertEqual(authorize(self.subject, document, ResourceAction.READ).reason, "not_allowlisted")
        inactive = Subject("employee-1", "org-a", frozenset({"finance"}), Classification.DEPARTMENT, active=False)
        self.assertEqual(authorize(inactive, self.document, ResourceAction.READ).reason, "inactive_or_missing_identity")

    def test_read_does_not_imply_download_or_generation(self):
        document = DocumentPolicy(**{**self.document.__dict__, "downloadable": False, "usable_for_generation": False})
        self.assertTrue(authorize(self.subject, document, ResourceAction.READ).allowed)
        self.assertFalse(authorize(self.subject, document, ResourceAction.DOWNLOAD).allowed)
        self.assertFalse(authorize(self.subject, document, ResourceAction.USE_FOR_GENERATION).allowed)

    def test_unknown_action_fails_closed(self):
        self.assertFalse(authorize(self.subject, self.document, "share").allowed)

    def test_system_admin_needs_explicit_document_allowlist(self):
        from app.security.policy import UserRole

        admin = Subject("admin-1", "org-a", frozenset({"finance"}), Classification.RESTRICTED, role=UserRole.SYSTEM_ADMIN)
        self.assertEqual(authorize(admin, self.document, ResourceAction.READ).reason, "admin_requires_explicit_access")
        allowed = DocumentPolicy(**{**self.document.__dict__, "allowed_user_ids": frozenset({"admin-1"})})
        self.assertTrue(authorize(admin, allowed, ResourceAction.READ).allowed)


if __name__ == "__main__":
    unittest.main()
