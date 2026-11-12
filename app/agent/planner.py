import json

from app.agent.runtime import RuntimeGuard

from app.llm.client import (
    create_llm_client,
    MODEL_NAME,
)


class TaskPlanner:
    ALLOWED_ACTIONS = {
        "calculator",
        "knowledge_search",
        "profile_query",
        "chat",
    }

    def __init__(self):

        self.llm = create_llm_client()

        self.runtime = RuntimeGuard()

    def plan(
        self,
        user_input: str,
    ) -> dict:
        """
        将用户请求拆解成一个或多个可执行步骤。
        """

        system_prompt = """
You are the task planner of an enterprise AI agent.

Your job is to analyze the user's request and generate
an execution plan.

Available actions:

1. calculator
Use for mathematical calculations.

2. knowledge_search
Use for company internal knowledge, policies,
rules, reimbursement, annual leave, benefits, etc.

3. profile_query
Use for retrieving the user's stored profile,
current role, field, project, research direction, etc.

4. chat
Use for ordinary conversation or general generation
that does not require another action.

Rules:

- Do not answer the user's question.
- Only create an execution plan.
- Keep the plan minimal.
- Do not create unnecessary steps.
- If only one action is required, create one step.
- If multiple independent actions are required,
create multiple ordered steps.
- Maximum 5 steps.
- Only use the allowed actions.
- Keep each step input specific to that step.
- Return JSON only.

Return exactly this structure:

{
  "is_multistep": true,
  "goal": "overall user goal",
  "steps": [
    {
      "id": 1,
      "action": "calculator",
      "input": "25乘以8",
      "reason": "需要完成数学计算"
    },
    {
      "id": 2,
      "action": "knowledge_search",
      "input": "公司报销多久到账",
      "reason": "需要查询公司内部报销制度"
    }
  ]
}

If only one step is needed:

{
  "is_multistep": false,
  "goal": "overall user goal",
  "steps": [
    {
      "id": 1,
      "action": "knowledge_search",
      "input": "公司报销多久到账",
      "reason": "需要查询公司内部制度"
    }
  ]
}
"""

        success, response = self.runtime.call_llm(
            self.llm,
            model=MODEL_NAME,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_input,
                },
            ],
            temperature=0,
        )

        if not success:
            print(f"[Planner Error] {response}")

            return self._fallback_plan(user_input)

        content = response.choices[0].message.content or ""

        try:
            data = self._parse_json(content)

            return self._validate_plan(
                data,
                user_input,
            )

        except Exception as error:
            print(f"[Planner Error] {type(error).__name__}: {error}")

            return self._fallback_plan(user_input)

    def _validate_plan(
        self,
        data: dict,
        user_input: str,
    ) -> dict:

        if not isinstance(
            data,
            dict,
        ):
            return self._fallback_plan(user_input)

        steps = data.get(
            "steps",
            [],
        )

        if not isinstance(
            steps,
            list,
        ):
            return self._fallback_plan(user_input)

        cleaned_steps = []

        for step in steps[:5]:
            if not isinstance(
                step,
                dict,
            ):
                continue

            action = step.get("action")

            step_input = step.get("input")

            reason = step.get(
                "reason",
                "",
            )

            if action not in self.ALLOWED_ACTIONS:
                continue

            if not isinstance(
                step_input,
                str,
            ):
                continue

            step_input = step_input.strip()

            if not step_input:
                continue

            cleaned_steps.append(
                {
                    "id": len(cleaned_steps) + 1,
                    "action": action,
                    "input": step_input,
                    "reason": str(reason),
                }
            )

        if not cleaned_steps:
            return self._fallback_plan(user_input)

        goal = data.get(
            "goal",
            user_input,
        )

        if not isinstance(
            goal,
            str,
        ):
            goal = user_input

        return {
            "is_multistep": len(cleaned_steps) > 1,
            "goal": goal,
            "steps": cleaned_steps,
        }

    def _fallback_plan(
        self,
        user_input: str,
    ) -> dict:
        """
        Planner 出错时也不能让整个 Agent 崩掉。
        """

        return {
            "is_multistep": False,
            "goal": user_input,
            "steps": [
                {
                    "id": 1,
                    "action": "chat",
                    "input": user_input,
                    "reason": "Planner fallback",
                }
            ],
        }

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
