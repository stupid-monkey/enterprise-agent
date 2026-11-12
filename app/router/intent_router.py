import re


class IntentRouter:
    def route(
        self,
        user_input: str,
    ) -> str:

        text = user_input.strip()

        # =========================
        # 1. Profile Query
        # =========================

        profile_query_keywords = [
            "我是谁",
            "我的工作",
            "我的岗位",
            "我的职业",
            "我做什么工作",
            "我是做什么工作的",
            "我的方向",
            "我的领域",
            "我的专业",
            "我的项目",
            "我现在负责什么",
            "我目前负责什么",
            "我负责什么项目",
            "我现在负责什么项目",
            "我目前负责什么项目",
            "我现在在做什么项目",
            "我的偏好",
            "回答偏好",
            "回答风格",
        ]

        if any(keyword in text for keyword in profile_query_keywords):
            return "PROFILE_QUERY"

        # =========================
        # 2. Profile / Memory Update
        # =========================

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

        is_question = any(word in text for word in question_words)

        memory_update_keywords = [
            "我是一名",
            "我的职业是",
            "我的岗位是",
            "我是",
            "目前在负责",
            "现在在负责",
            "负责一个",
            "我负责",
            "负责",
            "我的研究方向是",
            "研究方向是",
            "我正在做",
            "我现在在做",
            "我最近在做",
            "最近在做",
            "以后回答",
            "回答尽量",
            "回答要",
            "我更喜欢",
            "我希望以后",
            "我的偏好是",
            "请记住",
        ]

        if not is_question and any(
            keyword in text for keyword in memory_update_keywords
        ):
            return "PROFILE_UPDATE"

        # =========================
        # 3. Calculator
        # =========================

        symbol_expression = re.search(
            r"\d+(?:\.\d+)?"
            r"\s*[\+\-\*/×÷]\s*"
            r"\d+(?:\.\d+)?",
            text,
        )

        chinese_expression = re.search(
            r"\d+(?:\.\d+)?\s*"
            r"(?:乘以|乘|除以|除|"
            r"加上|加|减去|减)"
            r"\s*\d+(?:\.\d+)?",
            text,
        )

        calculate_command = "计算" in text and bool(
            re.search(
                r"\d",
                text,
            )
        )

        if symbol_expression or chinese_expression or calculate_command:
            return "CALCULATOR"

        # =========================
        # 4. Chat
        # =========================

        chat_keywords = [
            "你好",
            "您好",
            "谢谢",
            "再见",
            "早上好",
            "下午好",
            "晚上好",
        ]

        if any(keyword in text for keyword in chat_keywords):
            return "CHAT"

        # =========================
        # 5. Default Knowledge
        # =========================

        return "KNOWLEDGE"
