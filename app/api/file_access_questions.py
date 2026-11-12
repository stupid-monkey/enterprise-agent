"""Narrow, model-free permission questions about one knowledge-base document."""

import re
from dataclasses import dataclass
from typing import Protocol

from app.api.documents import DocumentRecord
from app.security.policy import ResourceAction, Subject, authorize


@dataclass(frozen=True)
class FileAccessQuery:
    action: ResourceAction
    title: str | None


def parse_file_access_query(question: str) -> FileAccessQuery | None:
    text = re.sub(r"\s+", "", question).rstrip("？?。！!")
    if not text or len(text) > 160:
        return None
    if text.endswith(("吗", "呢")):
        text = text[:-1]
    verb = r"(查看|阅读|访问|看一下|看|打开|预览|浏览|读取|检视|读|下载|获取)"
    patterns = (
        # Subject + modal + action + target.
        (r"(?:请问)?(?:我|本人)(?:现在|目前|当前)?(?:是否|能否|有没有|可不可以|能不能)?"
         r"(?:有权|有权限)?(?:可以|能|可|可否)?" + verb + r"(.+)", "verb_first"),
        # Modal + action + target, with no personal subject.
        (r"(?:请问)?(?:能不能|可不可以|是否可以|可以|能否|可否|能)" + verb + r"(.+)", "verb_first"),
        # Selected file or quoted title precedes the actor and action.
        (r"(?:请问)?(.+?)(?:我)?(?:是否|能否|可以|能|有权|有权限|可不可以|能不能|可否)"
         + verb, "target_first"),
        (r"(?:请问)?(.+?)(?:是否)?允许我" + verb, "target_first"),
        (r"(?:请问)?我(?:可以|能|可)把(.+?)下载下来", "download_target"),
        (r"(?:请问)?我对(.+?)有" + verb + r"权限", "target_first"),
        (r"(?:请问)?我有(.+?)的" + verb + r"权限", "target_first"),
    )
    for pattern, order in patterns:
        match = re.fullmatch(pattern, text)
        if match is None:
            continue
        if order == "verb_first":
            action_word, target = match.groups()
        elif order == "download_target":
            (target,) = match.groups()
            action_word = "下载"
        else:
            target, action_word = match.groups()
        if action_word == "获取":
            if not target.endswith("原件"):
                return None
            target = target[:-2]
        title = _target_title(target, allow_plain=order == "verb_first")
        if title is False:
            return None
        action = (ResourceAction.DOWNLOAD if action_word in ("下载", "获取")
                  else ResourceAction.READ)
        return FileAccessQuery(action, title)
    return None


def _target_title(target: str, *, allow_plain: bool) -> str | None | bool:
    if target in ("这份文件", "这份文档", "这份资料", "这个文件", "这个文档", "这个资料",
                  "该文件", "该文档", "该资料"):
        return None
    if target.startswith(("这份", "这个", "该", "哪些", "什么", "多少", "这些", "所有",
                          "其他", "谁", "哪里", "哪份", "哪种", "到它", "它", "它们")):
        return False
    if target.startswith("《") and target.endswith("》") and target.count("《") == target.count("》") == 1:
        title = target[1:-1]
    elif allow_plain:
        title = target
    else:
        return False
    if (not title or len(title) > 120 or any(char in title for char in "《》？?！!。")
            or title in ("文件", "文档", "资料")
            or any(phrase in title for phrase in ("是什么", "的流程", "中的", "里的", "之后", "然后", "后帮"))):
        return False
    return title


def model_permission_fallback_allowed(question: str) -> bool:
    """Do not turn a plural, workflow or content request into a single-file verdict."""
    outside_titles = re.sub(r"《[^《》]+》", "", question)
    if any(term in outside_titles for term in (
            "这些", "哪些", "所有", "全部", "其他", "多个", "申请", "流程",
            "为什么", "怎么", "帮我", "请把", "中的", "里的", "并", "然后")):
        return False
    return (bool(re.search(r"我|本人|这份|这个|该", outside_titles))
            and bool(re.search(r"能|可以|可否|权限|有权|允许|是否|有没有", outside_titles)))


@dataclass(frozen=True)
class FileAccessResult:
    text: str
    document_id: str | None = None
    version: str | None = None


class DocumentAccessRepository(Protocol):
    def get_visible(self, subject: Subject, document_id: str) -> DocumentRecord | None: ...
    def list_visible(self, subject: Subject) -> list[DocumentRecord]: ...


def answer_file_access(query: FileAccessQuery, subject: Subject,
                       documents: DocumentAccessRepository,
                       selected_document_id: str | None = None) -> FileAccessResult | None:
    """Return None only when a selected source is no longer readable."""
    record: DocumentRecord | None = None
    if selected_document_id:
        record = documents.get_visible(subject, selected_document_id)
        if record is None:
            return None
        if query.title is not None and query.title != re.sub(r"\s+", "", record.title):
            return FileAccessResult("你选中的文件与问题中的名称不一致。请重新选择要检查的文件。")
    elif query.title is not None:
        matches = [item for item in documents.list_visible(subject)
                   if re.sub(r"\s+", "", item.title) == query.title]
        if len(matches) != 1:
            return FileAccessResult("未能在你当前可见的资料中唯一确认该文件。请在资料库中选择文件后再问。")
        record = matches[0]
    else:
        return FileAccessResult("请先从资料库中选择要检查的文件。")

    safe_title = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", record.title)
    if query.action == ResourceAction.READ:
        text = f"按当前账号的授权检查，你可以查看《{safe_title}》。"
    elif authorize(subject, record.policy, ResourceAction.DOWNLOAD).allowed and record.has_original:
        text = f"按当前账号的授权检查，你可以下载《{safe_title}》的当前原件。"
    elif not record.has_original:
        text = f"你可以查看《{safe_title}》，但当前没有可下载的原件。"
    else:
        text = f"你可以查看《{safe_title}》，但当前没有下载权限。"
    return FileAccessResult(text + "资料权限或版本变化后，操作时会重新检查。",
                            record.document_id, record.version)
