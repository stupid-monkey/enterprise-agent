import json

from app.llm.client import (
    create_llm_client,
    MODEL_NAME,
)


class MemoryExtractor:
    ALLOWED_KEYS = {
        "role",
        "field",
        "task",
        "research",
        "answer_preference",
    }

    def __init__(self):

        self.llm = create_llm_client()

    def extract(
        self,
        text: str,
    ) -> dict:

        text = text.strip()

        if not text:
            return {}

        if self._looks_like_question(text):
            return {}

        rule_result = self._rule_extract(text)

        if rule_result:
            return rule_result

        if not self._may_contain_memory(text):
            return {}

        return self._llm_extract(text)

    def _rule_extract(
        self,
        text: str,
    ) -> dict:

        result = {}

        # =========================
        # Role
        # =========================

        role_prefixes = [
            "我是一名",
            "我的职业是",
            "我的岗位是",
            "我是",
        ]

        for prefix in role_prefixes:
            if prefix in text:
                value = self._value_after(
                    text,
                    prefix,
                )

                if value:
                    result["role"] = value

                break

        # =========================
        # Task (优先级最高)
        # =========================

        task_prefixes = [
            "目前在负责",
            "现在在负责",
            "我正在负责",
            "我目前负责",
            "我现在负责",
            "最近负责",
            "我最近负责",
            "目前在做",
            "现在在做",
            "我正在做",
            "我最近在做",
            "负责一个",
            "负责",
        ]

        task_found = False

        for prefix in task_prefixes:
            if prefix in text:
                value = self._value_after(
                    text,
                    prefix,
                )

                if value:
                    result["task"] = value
                    task_found = True

                break
                # =========================
        # Research
        # =========================

        research_prefixes = [
            "我的研究方向是",
            "研究方向是",
            "我的研究是",
        ]

        for prefix in research_prefixes:
            if prefix in text:
                value = self._value_after(
                    text,
                    prefix,
                )

                if value:
                    result["research"] = value

                break

        # =========================
        # Field
        # =========================
        #
        # 注意：
        # 只有明确表达“方向/领域”
        # 才进入field
        #
        # 避免：
        # 我最近负责目标检测项目
        # 被误判成field
        #

        field_prefixes = [
            "我的方向是",
            "工作方向是",
            "专业方向是",
            "研究领域是",
            "负责方向是",
        ]

        for prefix in field_prefixes:
            if prefix in text:
                value = self._value_after(
                    text,
                    prefix,
                )

                if value:
                    result["field"] = value

                break

        # =========================
        # Answer Preference
        # =========================

        preference_signals = [
            "以后回答",
            "以后请回答",
            "回答尽量",
            "回答要",
            "回复尽量",
            "回复要",
            "回答风格",
        ]

        if any(signal in text for signal in preference_signals):
            result["answer_preference"] = text.strip(" ，。！？?")

        return result

    def _llm_extract(
        self,
        text: str,
    ) -> dict:

        system_prompt = """

You are a memory extraction component for an enterprise AI agent.

Your task is to determine whether a user statement contains
useful long-term memory.

Only save stable or repeatedly useful information.

Allowed memory keys:

role
- user's occupation or job role

field
- user's professional working field

task
- user's current ongoing project or task

research
- user's research direction

answer_preference
- persistent preference about how the assistant should answer

Do NOT save:

- ordinary questions
- temporary requests
- one-time calculations
- company knowledge
- passwords
- API keys
- access tokens
- credentials
- highly sensitive personal information
- temporary emotions
- random conversational details

Return JSON only.

If memory should be saved:

{
 "should_save": true,
 "memories": {
    "key":"value"
 }
}

If nothing should be saved:

{
 "should_save": false,
 "memories": {}
}

"""

        try:
            response = self.llm.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": text,
                    },
                ],
                temperature=0,
            )

            content = response.choices[0].message.content or ""

            data = self._parse_json(content)

            if not data.get("should_save", False):
                return {}

            memories = data.get("memories", {})

            if not isinstance(memories, dict):
                return {}

            cleaned = {}

            for key, value in memories.items():
                if key not in self.ALLOWED_KEYS:
                    continue

                if not isinstance(value, str):
                    continue

                value = value.strip()

                if value:
                    cleaned[key] = value

            return cleaned

        except Exception as error:
            print(f"[MemoryExtractor] LLM extraction failed: {error}")

            return {}

    def _may_contain_memory(
        self,
        text: str,
    ) -> bool:

        signals = [
            "我是",
            "我的",
            "我负责",
            "负责",
            "我正在",
            "我现在",
            "我最近",
            "以后",
            "记住",
            "我喜欢",
            "我更喜欢",
            "我希望",
            "我的偏好",
        ]

        return any(signal in text for signal in signals)

    def _looks_like_question(
        self,
        text: str,
    ) -> bool:

        question_words = [
            "什么",
            "多少",
            "哪里",
            "谁",
            "怎么",
            "如何",
            "为什么",
            "吗",
            "呢",
            "?",
            "？",
        ]

        return any(word in text for word in question_words)

    def _value_after(
        self,
        text: str,
        prefix: str,
    ) -> str:

        return text.split(prefix, 1)[1].strip(" ，。！？?")

    def _parse_json(
        self,
        content: str,
    ) -> dict:

        content = content.strip()

        if content.startswith("```json"):
            content = content[len("```json") :]

        elif content.startswith("```"):
            content = content[3:]

        if content.endswith("```"):
            content = content[:-3]

        content = content.strip()

        return json.loads(content)
