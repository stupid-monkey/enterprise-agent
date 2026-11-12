"""Conservative routing between company knowledge and general conversation."""

import re
from typing import Literal

from app.api.content import search_terms
from app.api.general_chat import local_reply


Intent = Literal["knowledge", "general"]

FILE_EDIT_TERMS = ("修改", "改成", "改为", "替换", "删除", "删掉", "补充", "新增", "添加", "润色", "重写", "调整", "精简", "扩写", "修订", "编辑")


def route_file_intent(question: str) -> Literal["answer", "edit"]:
    """Conservative routing: an ambiguous file request remains read-only."""
    text = question.strip().lower()
    return "edit" if any(term in text for term in FILE_EDIT_TERMS) else "answer"

KNOWLEDGE_TERMS = (
    "我们公司", "本公司", "我司", "公司内部", "企业内部", "内部资料", "授权资料",
    "知识库", "工作台", "资料中", "资料里", "文档中", "文档里", "这份文档",
    "制度", "规定", "政策", "审批", "报销", "年假", "请假", "病假", "产品发布", "发布前",
)
FOLLOWUP_PREFIXES = ("那", "这个", "这些", "上述", "刚才", "它", "还要", "还有", "具体")
POTENTIAL_DELIVERABLE = re.compile(
    r"(?:整理|准备|梳理|汇总|形成|产出|变成|转成|提炼|写成|整出)"
    r"|(?:底稿|稿子|可直接发|拿去汇报|用于汇报)"
)
FACT_QUESTION = re.compile(
    r"(?:什么|哪些|多少|几天|几次|何时|谁|如何|怎么|能否|是否|有没有|吗|多久|哪天|哪位|由谁|在哪)"
)
KNOWLEDGE_ALIASES = (("上线", "发布"), ("发版", "发布"), ("核对", "检查"),
                     ("带薪假", "年假"))


def normalize_knowledge_terms(value: str) -> str:
    text = value.strip()
    for phrase, canonical in KNOWLEDGE_ALIASES:
        text = text.replace(phrase, canonical)
    return text


def _matches_visible_title(question: str, titles: tuple[str, ...]) -> bool:
    question_terms = search_terms(question, question=True)
    if len(question_terms) < 2:
        return False
    return any(len(question_terms & search_terms(title)) >= 2 for title in titles)


def route_intent(question: str, *, area: str | None = None, previous_knowledge: str | None = None,
                 visible_titles: tuple[str, ...] = ()) -> Intent:
    text = normalize_knowledge_terms(question).lower()
    if local_reply(question) is not None:
        return "general"
    # A known business concept is independent of the order in which users mention its words.
    if "产品" in text and "发布" in text:
        return "knowledge"
    if "发布" in text and any(term in text for term in ("检查", "验收", "审批", "回滚")):
        return "knowledge"
    if area is not None or any(term in text for term in KNOWLEDGE_TERMS):
        return "knowledge"
    if previous_knowledge and text.startswith(FOLLOWUP_PREFIXES):
        return "knowledge"
    if _matches_visible_title(text, visible_titles):
        return "knowledge"
    return "general"


def high_confidence_intent(question: str, *, area: str | None = None,
                           previous_knowledge: str | None = None) -> Intent | None:
    """Only explicit context bypasses semantic classification when it is enabled."""
    if local_reply(question) is not None:
        return "general"
    text = question.strip().lower()
    if (not any(term in text for term in ("国家规定", "法律规定", "法定", "全国"))
            and any(term in text for term in ("我能", "我可以", "我每年", "我最多", "我的"))
            and any(term in text for term in ("享受", "能休", "能领", "可以领", "能报销"))
            and any(term in text for term in ("多少", "几天", "几次", "额度"))):
        return "knowledge"
    if area is not None or any(phrase in text for phrase in (
            "我们公司", "本公司", "我司", "公司内部", "企业内部", "内部资料",
            "授权资料", "知识库", "资料中", "资料里", "这份文档")):
        return "knowledge"
    if previous_knowledge and text.startswith(FOLLOWUP_PREFIXES):
        return "knowledge"
    return None


def needs_deliverable_classification(question: str) -> bool:
    """Route work requests to the model while preserving explicit factual questions' fast path."""
    return bool(POTENTIAL_DELIVERABLE.search(question)) or not FACT_QUESTION.search(question)


def knowledge_query(question: str, previous_knowledge: str | None) -> str:
    text = question.strip()
    if previous_knowledge and text.startswith(FOLLOWUP_PREFIXES):
        text = f"{previous_knowledge} {text}"[:1000]
    return normalize_knowledge_terms(text)
