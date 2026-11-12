class AnswerValidator:
    def validate(
        self,
        answer: str,
        evidence,
        tool_type=None,
    ):
        """
        Validate the final answer.

        Current strategy:

        1. Non-knowledge tools do not require evidence validation.
        2. knowledge_search must have reliable evidence.
        3. If reliable evidence exists, allow the grounded answer.
        4. If no reliable evidence exists, block the answer.

        This intentionally avoids brittle string matching.
        """

        # =========================
        # Non-RAG answers
        # =========================

        if tool_type != "knowledge_search":
            return True, answer

        # =========================
        # Evidence must exist
        # =========================

        if not evidence:
            return (
                False,
                "知识库中没有找到可靠信息，无法回答该问题。",
            )

        # =========================
        # Evidence format check
        # =========================

        if not isinstance(
            evidence,
            dict,
        ):
            return (
                False,
                "知识库返回的证据格式异常，暂时无法可靠回答。",
            )

        # =========================
        # Retrieval status check
        # =========================

        status = evidence.get("status")

        if status != "SUCCESS":
            return (
                False,
                "知识库中没有找到可靠信息，无法回答该问题。",
            )

        # =========================
        # Evidence list check
        # =========================

        evidence_items = evidence.get(
            "evidence",
            [],
        )

        if not evidence_items:
            return (
                False,
                "知识库中没有找到可靠信息，无法回答该问题。",
            )

        # =========================
        # Valid grounded response
        # =========================

        return True, answer
