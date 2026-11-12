from app.memory.profile import UserProfileMemory

from app.memory.extractor import MemoryExtractor

from app.memory.summary import ConversationSummaryMemory


class MemoryManager:
    CONTENT_KEYS = {
        "role",
        "field",
        "task",
        "research",
    }

    BEHAVIOR_KEYS = {
        "answer_preference",
    }

    def __init__(self):

        self.profile = UserProfileMemory()

        self.extractor = MemoryExtractor()

        # conversation summary memory
        self.summary = ConversationSummaryMemory()

    def update_from_text(
        self,
        text: str,
    ) -> dict:
        """
        从用户输入中抽取长期记忆并持久化。
        """

        memories = self.extractor.extract(text)

        if not memories:
            print("[Memory] No long-term memory extracted.")

            return {}

        for key, value in memories.items():
            self.profile.update(
                key,
                value,
            )

            print(f"[Memory] {key}: {value}")

        return memories

    def get_context(
        self,
    ) -> dict:
        """
        获取全部长期记忆。
        """

        return self.profile.get_all()

    def get_content_context(
        self,
    ) -> dict:
        """
        获取事实类长期记忆。
        """

        profile = self.profile.get_all()

        content = {}

        for key in self.CONTENT_KEYS:
            value = profile.get(key)

            if value:
                content[key] = value

        return content

    def get_behavior_context(
        self,
    ) -> dict:
        """
        获取行为偏好类长期记忆。
        """

        profile = self.profile.get_all()

        behavior = {}

        for key in self.BEHAVIOR_KEYS:
            value = profile.get(key)

            if value:
                behavior[key] = value

        return behavior

    def get_relevant_content(
        self,
        query: str,
    ) -> dict:
        """
        根据当前问题获取相关用户信息。
        """

        profile = self.profile.get_all()

        relevant = {}

        query = query.strip()

        # role

        role_keywords = [
            "我是谁",
            "做什么工作",
            "做什么工作的",
            "工作是什么",
            "职业",
            "岗位",
            "身份",
        ]

        if any(keyword in query for keyword in role_keywords):
            value = profile.get("role")

            if value:
                relevant["role"] = value

        # research

        research_keywords = [
            "研究方向",
            "研究什么",
            "科研方向",
        ]

        is_research_query = any(keyword in query for keyword in research_keywords)

        if is_research_query:
            value = profile.get("research")

            if value:
                relevant["research"] = value

        # field

        if not is_research_query:
            field_keywords = [
                "工作方向",
                "负责什么方向",
                "我的方向",
                "我的领域",
                "专业领域",
                "工作领域",
            ]

            if any(keyword in query for keyword in field_keywords):
                value = profile.get("field")

                if value:
                    relevant["field"] = value

        # task

        task_keywords = [
            "项目",
            "任务",
            "当前负责",
            "现在负责",
            "目前负责",
            "正在做",
            "在做什么",
        ]

        if any(keyword in query for keyword in task_keywords):
            value = profile.get("task")

            if value:
                relevant["task"] = value

        # self introduction

        if "自我介绍" in query or "介绍一下我" in query:
            for key in [
                "role",
                "field",
                "task",
                "research",
            ]:
                value = profile.get(key)

                if value:
                    relevant[key] = value

        return relevant

    def get_query_memory(
        self,
        query: str,
    ) -> dict:
        """
        PROFILE_QUERY 使用。
        """

        result = self.get_relevant_content(query)

        preference_keywords = [
            "我的偏好",
            "回答偏好",
            "回答风格",
            "回复偏好",
            "回复风格",
            "喜欢怎么回答",
        ]

        if any(keyword in query for keyword in preference_keywords):
            behavior = self.get_behavior_context()

            result.update(behavior)

        return result

    def get(
        self,
        key: str,
        default=None,
    ):

        return self.profile.get(
            key,
            default,
        )

    # ===============================
    # Conversation Summary Memory
    # ===============================

    def get_summary(
        self,
    ):

        return self.summary.get()

    def update_summary(
        self,
        summary: str,
    ):

        self.summary.update(summary)
