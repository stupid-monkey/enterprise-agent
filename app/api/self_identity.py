"""Answer direct account questions from authentication and current ACL decisions."""

import re
from typing import Literal

from app.security.policy import Classification, Subject, UserRole


AccountQuestion = Literal["identity", "department", "account", "access"]


ROLE_LABELS = {
    UserRole.EMPLOYEE: "普通员工",
    UserRole.DEPARTMENT_MANAGER: "部门负责人",
    UserRole.KNOWLEDGE_ADMIN: "知识管理员",
    UserRole.SYSTEM_ADMIN: "系统管理员",
}

CLEARANCE_LABELS = {
    Classification.INTERNAL: "内部",
    Classification.DEPARTMENT: "部门",
    Classification.CONFIDENTIAL: "机密",
    Classification.RESTRICTED: "受限",
}


def account_question_kind(question: str) -> AccountQuestion | None:
    """Match only standalone questions about the current login, never policy or files."""
    text = re.sub(r"[\s，。！？?！,.]", "", question).lower()
    if len(text) > 40 or not any(term in text for term in ("我", "本人", "自己")):
        return None
    if any(term in text for term in ("制度", "规定", "流程", "认证", "验证", "申请", "文档中", "资料中", "公司经理")):
        return None
    prefix = r"(?:请问)?(?:我|本人|我自己|我的|我在公司|我在系统里|我在这个系统里|我这个账号|我的账号|我的账户)"
    timing = r"(?:现在|目前|当前)?"
    if re.fullmatch(prefix + timing + r"(?:是|属于|当前是|现在是|的)?(?:什么|哪个|哪种|啥)?"
                    r"(?:身份|角色|岗位|职位|职务|用户类型|账号角色)(?:是什么|是哪个|是啥|吗)?", text) or text in (
                        "我是谁", "我在公司是谁", "我在系统里是谁"):
        return "identity"
    if re.fullmatch(prefix + timing + r"(?:是|属于|所在|在哪个|在哪|的)?(?:什么|哪个|哪一个)?"
                    r"部门(?:是什么|是哪个|吗)?", text):
        return "department"
    if re.fullmatch(prefix + timing + r"(?:登录的|登录的是|的)?(?:账号|账户|用户id|用户编号)"
                    r"(?:是什么|是哪个|是多少|吗)?", text) or text in (
                        "我现在登录的是哪个账号", "我目前登录的是哪个账号"):
        return "account"
    if re.fullmatch(prefix + timing + r"(?:有|拥有|具备|的)?(?:什么|哪些)?"
                    r"(?:资料|文件|知识库)?(?:访问|查看|阅读)?权限(?:是什么|有哪些|吗)?", text) or re.fullmatch(
                        prefix + timing + r"(?:能|可以|有权)(?:访问|查看|阅读|看)(?:哪些|什么)(?:资料|文件|文档)", text):
        return "access"
    return None


def asks_own_identity(question: str) -> bool:
    return account_question_kind(question) == "identity"


def asks_own_account(question: str) -> bool:
    return account_question_kind(question) is not None


def own_identity_answer(subject: Subject, memories: list[dict]) -> str:
    role = ROLE_LABELS[subject.role]
    lines = [f"你当前登录的**系统身份**是：**{role}**。"]
    saved_role = next((item["value"] for item in memories if item["kind"] == "role"), None)
    if saved_role:
        lines.append(f"你在聊天中保存的**岗位背景**是：{saved_role}。")
    else:
        lines.append("目前没有保存你的岗位背景。")
    lines.append("系统身份决定资料访问权限；聊天中的岗位背景不会改变权限。")
    return "\n\n".join(lines)


def own_account_answer(subject: Subject, question: str, memories: list[dict],
                       visible_document_titles: list[str] | None = None) -> str:
    kind = account_question_kind(question)
    if kind == "identity":
        return own_identity_answer(subject, memories)
    if kind == "department":
        departments = "、".join(sorted(subject.department_ids))
        return (f"你当前账号所属的部门编号：**{departments}**。" if departments
                else "当前账号没有配置所属部门。")
    if kind == "account":
        return (f"你当前登录的用户 ID：**{subject.user_id}**。\n\n"
                f"所属组织 ID：**{subject.org_id}**。系统暂未提供可核实的姓名或登录用户名。")
    if kind == "access":
        if visible_document_titles is None:
            raise ValueError("VISIBLE_DOCUMENTS_REQUIRED")
        departments = "、".join(sorted(subject.department_ids)) or "未配置"
        header = (f"你当前的系统角色是**{ROLE_LABELS[subject.role]}**，资料密级上限为"
                  f"**{CLEARANCE_LABELS[subject.clearance]}**，所属部门编号为**{departments}**。\n\n"
                  f"按当前逐份授权检查，你可查看知识库中的 **{len(visible_document_titles)}** 份已发布资料。")
        shown = []
        for title in visible_document_titles[:8]:
            safe_title = re.sub(r"\s+", " ", title).strip()
            safe_title = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", safe_title)
            shown.append(f"- {safe_title}")
        details = "\n\n" + "\n".join(shown) if shown else ""
        if len(visible_document_titles) > len(shown):
            details += f"\n\n其余 {len(visible_document_titles) - len(shown)} 份可在资料库查看。"
        return (header + details + "\n\n可见范围会随资料状态、部门、名单和单份拒绝规则变化；"
                "下载和用于生成仍需另行授权。")
    raise ValueError("NOT_ACCOUNT_QUESTION")
