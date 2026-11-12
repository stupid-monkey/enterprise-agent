"""Pure resource policy; callers must obtain subjects from trusted authentication."""

from dataclasses import dataclass
from enum import Enum, IntEnum


class Classification(IntEnum):
    INTERNAL = 1
    DEPARTMENT = 2
    CONFIDENTIAL = 3
    RESTRICTED = 4


class UserRole(str, Enum):
    EMPLOYEE = "employee"
    DEPARTMENT_MANAGER = "department_manager"
    KNOWLEDGE_ADMIN = "knowledge_admin"
    SYSTEM_ADMIN = "system_admin"


class DocumentState(str, Enum):
    DRAFT = "draft"
    REVIEW = "review"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class Visibility(str, Enum):
    ORGANIZATION = "organization"
    DEPARTMENT = "department"
    ALLOWLIST = "allowlist"


class ResourceAction(str, Enum):
    READ = "read"
    DOWNLOAD = "download"
    USE_FOR_GENERATION = "use_for_generation"


@dataclass(frozen=True)
class Subject:
    user_id: str
    org_id: str
    department_ids: frozenset[str]
    clearance: Classification
    active: bool = True
    role: UserRole = UserRole.EMPLOYEE


@dataclass(frozen=True)
class DocumentPolicy:
    document_id: str
    org_id: str
    state: DocumentState
    classification: Classification
    visibility: Visibility
    department_ids: frozenset[str] = frozenset()
    allowed_user_ids: frozenset[str] = frozenset()
    denied_user_ids: frozenset[str] = frozenset()
    downloadable: bool = True
    usable_for_generation: bool = True


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def authorize(subject: Subject, document: DocumentPolicy, action: ResourceAction) -> Decision:
    """Decide one resource action; unknown actions fail closed."""
    if not subject.active or not subject.user_id or not subject.org_id:
        return Decision(False, "inactive_or_missing_identity")
    if subject.org_id != document.org_id:
        return Decision(False, "organization_mismatch")
    if subject.user_id in document.denied_user_ids:
        return Decision(False, "explicit_deny")
    if subject.role == UserRole.SYSTEM_ADMIN and subject.user_id not in document.allowed_user_ids:
        return Decision(False, "admin_requires_explicit_access")
    if document.state != DocumentState.PUBLISHED:
        return Decision(False, "not_published")
    if subject.clearance < document.classification:
        return Decision(False, "insufficient_clearance")

    if document.visibility == Visibility.DEPARTMENT:
        if not subject.department_ids.intersection(document.department_ids):
            return Decision(False, "outside_department")
    elif document.visibility == Visibility.ALLOWLIST:
        if subject.user_id not in document.allowed_user_ids:
            return Decision(False, "not_allowlisted")
    elif document.visibility != Visibility.ORGANIZATION:
        return Decision(False, "unknown_visibility")

    if action == ResourceAction.READ:
        return Decision(True, "allowed")
    if action == ResourceAction.DOWNLOAD and document.downloadable:
        return Decision(True, "allowed")
    if action == ResourceAction.USE_FOR_GENERATION and document.usable_for_generation:
        return Decision(True, "allowed")
    return Decision(False, "action_not_allowed")
