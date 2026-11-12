import re

from app.agent.runtime import RuntimeGuard

from app.llm.client import (
    create_llm_client,
    MODEL_NAME,
)

from app.tools.calculator import calculator
from app.tools.knowledge import KnowledgeSearchTool

from app.memory.manager import MemoryManager


class TaskExecutor:
    def __init__(
        self,
        knowledge_tool=None,
        memory_manager=None,
    ):

        self.llm = create_llm_client()

        self.runtime = RuntimeGuard()

        # 支持依赖注入
        # EnterpriseAgent 中会复用已经创建好的对象
        self.knowledge_tool = (
            knowledge_tool if knowledge_tool is not None else KnowledgeSearchTool()
        )

        self.memory_manager = (
            memory_manager if memory_manager is not None else MemoryManager()
        )

    def execute_plan(
        self,
        plan: dict,
    ) -> dict:

        steps = plan.get(
            "steps",
            [],
        )

        results = []

        for step in steps:
            step_id = step.get("id")

            action = step.get("action")

            step_input = step.get(
                "input",
                "",
            )

            print(f"[Executor] Step {step_id}: {action}")

            result = self.execute_step(
                action=action,
                step_input=step_input,
            )

            results.append(
                {
                    "step_id": step_id,
                    "action": action,
                    "input": step_input,
                    "success": result["success"],
                    "output": result["output"],
                }
            )

        all_success = all(item["success"] for item in results)

        return {
            "success": all_success,
            "goal": plan.get(
                "goal",
                "",
            ),
            "results": results,
        }

    def execute_step(
        self,
        action: str,
        step_input: str,
    ) -> dict:

        if action == "calculator":
            return self._execute_calculator(step_input)

        if action == "knowledge_search":
            return self._execute_knowledge(step_input)

        if action == "profile_query":
            return self._execute_profile(step_input)

        if action == "chat":
            return self._execute_chat(step_input)

        return {
            "success": False,
            "output": (f"Unsupported action: {action}"),
        }

    def _execute_calculator(
        self,
        text: str,
    ) -> dict:

        parsed = self._parse_calculation(text)

        if parsed is None:
            return {
                "success": False,
                "output": "无法解析数学表达式。",
            }

        success, result = self.runtime.call_tool(
            "calculator",
            calculator,
            a=parsed["a"],
            b=parsed["b"],
            operation=parsed["operation"],
        )

        return {
            "success": success,
            "output": result,
        }

    def _execute_knowledge(
        self,
        query: str,
    ) -> dict:

        success, result = self.runtime.call_tool(
            "knowledge_search",
            self.knowledge_tool.search,
            query=query,
        )

        if not success:
            return {
                "success": False,
                "output": result,
            }

        if not isinstance(
            result,
            dict,
        ):
            return {
                "success": False,
                "output": "知识库返回格式异常。",
            }

        if result.get("status") != "SUCCESS":
            return {
                "success": False,
                "output": result,
            }

        if not result.get("evidence"):
            return {
                "success": False,
                "output": result,
            }

        return {
            "success": True,
            "output": result,
        }

    def _execute_profile(
        self,
        query: str,
    ) -> dict:

        memory = self.memory_manager.get_query_memory(query)

        if not memory:
            return {
                "success": False,
                "output": ("没有找到相关的长期用户信息。"),
            }

        return {
            "success": True,
            "output": memory,
        }

    def _execute_chat(
        self,
        text: str,
    ) -> dict:

        behavior_memory = self.memory_manager.get_behavior_context()

        system_prompt = """
You are a helpful enterprise AI assistant.

Answer naturally.

Answer in the same language as the user.
"""

        if behavior_memory:
            system_prompt += "\nUser response preferences:\n" + str(behavior_memory)

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
                    "content": text,
                },
            ],
        )

        if not success:
            return {
                "success": False,
                "output": response,
            }

        answer = response.choices[0].message.content or ""

        return {
            "success": True,
            "output": answer,
        }

    def _parse_calculation(
        self,
        text: str,
    ):

        pattern = (
            r"(-?\d+(?:\.\d+)?)"
            r"\s*"
            r"(乘以|乘|×|\*|"
            r"除以|除|÷|/|"
            r"加上|加|\+|"
            r"减去|减|-)"
            r"\s*"
            r"(-?\d+(?:\.\d+)?)"
        )

        match = re.search(
            pattern,
            text,
        )

        if not match:
            return None

        a = float(match.group(1))

        operator = match.group(2)

        b = float(match.group(3))

        operation_map = {
            "乘以": "multiply",
            "乘": "multiply",
            "×": "multiply",
            "*": "multiply",
            "除以": "divide",
            "除": "divide",
            "÷": "divide",
            "/": "divide",
            "加上": "add",
            "加": "add",
            "+": "add",
            "减去": "subtract",
            "减": "subtract",
            "-": "subtract",
        }

        operation = operation_map.get(operator)

        if operation is None:
            return None

        return {
            "a": a,
            "b": b,
            "operation": operation,
        }
