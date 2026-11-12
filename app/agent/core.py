import json

from app.agent.validator import AnswerValidator
from app.agent.runtime import RuntimeGuard

from app.agent.planner import TaskPlanner
from app.agent.executor import TaskExecutor
from app.agent.synthesizer import FinalSynthesizer

from app.llm.client import (
    create_llm_client,
    MODEL_NAME,
)

from app.tools.calculator import calculator
from app.tools.knowledge import KnowledgeSearchTool

from app.memory.conversation import ConversationMemory
from app.memory.manager import MemoryManager

from app.router.intent_router import IntentRouter


class EnterpriseAgent:
    def __init__(self):

        # =========================
        # LLM
        # =========================

        self.llm = create_llm_client()

        # =========================
        # Runtime Guard
        # =========================

        self.runtime = RuntimeGuard()

        # =========================
        # Validator
        # =========================

        self.validator = AnswerValidator()

        # =========================
        # Long-term Memory
        # =========================

        self.memory_manager = MemoryManager()

        # =========================
        # Knowledge Tool
        # =========================

        self.knowledge_tool = KnowledgeSearchTool()

        # =========================
        # Tool Schemas
        # =========================

        self.tools = self._build_tools()

        # =========================
        # Short-term Memory
        # =========================

        self.memory = ConversationMemory(
            system_prompt=self._build_system_prompt(),
            max_turns=5,
        )

        # =========================
        # Fast Router
        # =========================

        self.router = IntentRouter()

        # =========================
        # Planning Pipeline
        # =========================

        self.planner = TaskPlanner()

        self.executor = TaskExecutor(
            knowledge_tool=self.knowledge_tool,
            memory_manager=self.memory_manager,
        )

        self.synthesizer = FinalSynthesizer(
            memory_manager=self.memory_manager,
        )

    def _build_system_prompt(
        self,
        memory_context=None,
        behavior_context=None,
    ):

        if memory_context is None:
            memory_context = {}

        if behavior_context is None:
            behavior_context = {}

        memory_text = ""

        behavior_text = ""

        if memory_context:
            memory_text = f"""

Relevant long-term user facts:

{
                json.dumps(
                    memory_context,
                    ensure_ascii=False,
                    indent=2,
                )
            }

Use these facts only when relevant.

Do not mention internal memory implementation.

"""

        if behavior_context:
            behavior_text = f"""

User response preferences:

{
                json.dumps(
                    behavior_context,
                    ensure_ascii=False,
                    indent=2,
                )
            }

Follow these preferences.

Do not mention them unless
the user explicitly asks.

"""

        return f"""
You are an enterprise internal AI assistant.

Rules:

1. Mathematical calculations must use calculator.

2. Company internal information must use knowledge_search.

3. Never invent company internal information.

4. If knowledge_search returns no reliable information,
say that reliable information was not found.

5. Company answers must be grounded in retrieved evidence.

6. When using knowledge_search,
include source and section.

7. Answer in the same language as the user.

8. Long-term user facts come from information
previously provided by the user.

9. Only use long-term user facts when relevant.

10. Response preferences control how you answer.

11. Never expose internal memory implementation details.

{memory_text}

{behavior_text}
"""

    def _build_tools(self):

        return [
            {
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": ("Perform basic mathematical calculations."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "a": {
                                "type": "number",
                            },
                            "b": {
                                "type": "number",
                            },
                            "operation": {
                                "type": "string",
                                "enum": [
                                    "add",
                                    "subtract",
                                    "multiply",
                                    "divide",
                                ],
                            },
                        },
                        "required": [
                            "a",
                            "b",
                            "operation",
                        ],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "knowledge_search",
                    "description": ("Search enterprise internal knowledge base."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                            },
                        },
                        "required": [
                            "query",
                        ],
                    },
                },
            },
        ]

    def _get_tools_for_intent(
        self,
        intent: str,
    ):

        if intent == "CALCULATOR":
            return [
                tool
                for tool in self.tools
                if (tool["function"]["name"] == "calculator")
            ]

        if intent == "KNOWLEDGE":
            return [
                tool
                for tool in self.tools
                if (tool["function"]["name"] == "knowledge_search")
            ]

        return []

    def _execute_tool(
        self,
        tool_name,
        arguments,
    ):

        if tool_name == "calculator":
            return calculator(
                a=arguments["a"],
                b=arguments["b"],
                operation=arguments["operation"],
            )

        if tool_name == "knowledge_search":
            return self.knowledge_tool.search(query=arguments["query"])

        raise ValueError(f"Unknown tool: {tool_name}")

    def _build_profile_answer(
        self,
        context: dict,
    ):

        if not context:
            return "目前没有找到与这个问题相关的长期用户信息。"

        return "根据已记录的用户信息：\n" + json.dumps(
            context,
            ensure_ascii=False,
            indent=2,
        )

    def _call_llm(
        self,
        **kwargs,
    ):

        return self.runtime.call_llm(
            self.llm,
            **kwargs,
        )

    def _should_use_planner(
        self,
        user_input: str,
    ) -> bool:
        """
        判断是否明显包含多个子任务。

        简单请求继续走 Fast Path，
        复合请求进入 Planner。
        """

        multi_step_signals = [
            "然后",
            "并且",
            "同时",
            "再帮我",
            "再告诉我",
            "顺便",
            "另外",
            "接着",
            "之后再",
            "并告诉我",
            "还要",
            "以及帮我",
            "；",
            ";",
        ]

        return any(signal in user_input for signal in multi_step_signals)

    def _run_planning_pipeline(
        self,
        user_input: str,
    ):

        print("[Planning] Multi-step request detected.")

        # =========================
        # Planner
        # =========================

        plan = self.planner.plan(user_input)

        print("[Plan]")

        print(
            json.dumps(
                plan,
                ensure_ascii=False,
                indent=2,
            )
        )

        # Planner 最终认为只有一步，
        # 则交回 Fast Path。
        if not plan.get(
            "is_multistep",
            False,
        ):
            print("[Planning] Single-step plan, fallback to fast path.")

            return None

        # =========================
        # Executor
        # =========================

        execution = self.executor.execute_plan(plan)

        print("[Execution]")

        print(
            json.dumps(
                execution,
                ensure_ascii=False,
                indent=2,
            )
        )

        # =========================
        # Final Synthesizer
        # =========================

        answer = self.synthesizer.synthesize(
            user_input=user_input,
            plan=plan,
            execution=execution,
        )

        # Planning 路径也进入短期记忆
        self.memory.add_user(user_input)

        self.memory.add_assistant(answer)

        return answer

    def run(
        self,
        user_input,
    ):

        retrieved_evidence = None

        last_tool = None

        # =========================
        # 1. Fast Router
        # =========================

        intent = self.router.route(user_input)

        print(f"[Intent] {intent}")

        # =========================
        # 2. PROFILE UPDATE
        # =========================
        # Profile Update 优先处理。
        #
        # Planner 当前还没有
        # profile_update action，
        # 所以用户主动提供长期信息时
        # 先直接写入 Memory。
        # =========================

        if intent == "PROFILE_UPDATE":
            updated = self.memory_manager.update_from_text(user_input)

            self.memory.add_user(user_input)

            if updated:
                answer = "好的，我已经记录您的信息。"

            else:
                answer = "这条信息暂时没有识别出需要长期保存的内容。"

            self.memory.add_assistant(answer)

            return answer

        # =========================
        # 3. MULTI-STEP CHECK
        # =========================
        #
        # 关键修改就在这里：
        #
        # 必须在 PROFILE_QUERY
        # 提前 return 之前判断多步骤任务。
        # =========================

        if self._should_use_planner(user_input):
            planning_answer = self._run_planning_pipeline(user_input)

            if planning_answer is not None:
                return planning_answer

        # =========================
        # 4. PROFILE QUERY
        # =========================
        # 只有确定不是复合任务之后，
        # 才直接走 Profile Fast Path。
        # =========================

        if intent == "PROFILE_QUERY":
            query_memory = self.memory_manager.get_query_memory(user_input)

            print(f"[Memory Retrieval] {query_memory}")

            answer = self._build_profile_answer(query_memory)

            self.memory.add_user(user_input)

            self.memory.add_assistant(answer)

            return answer

        # =========================
        # 5. Content Memory
        # =========================

        relevant_memory = self.memory_manager.get_relevant_content(user_input)

        if relevant_memory:
            print(f"[Memory Retrieval] {relevant_memory}")

        # =========================
        # 6. Behavior Memory
        # =========================

        behavior_memory = self.memory_manager.get_behavior_context()

        if behavior_memory:
            print(f"[Behavior Memory] {behavior_memory}")

        # =========================
        # 7. Build System Prompt
        # =========================

        self.memory.messages[0]["content"] = self._build_system_prompt(
            memory_context=relevant_memory,
            behavior_context=behavior_memory,
        )

        # =========================
        # 8. Save User Input
        # =========================

        self.memory.add_user(user_input)

        # =========================
        # 9. Fast Path Tool Selection
        # =========================

        available_tools = self._get_tools_for_intent(intent)

        # =========================
        # 10. Normal CHAT
        # =========================

        if not available_tools:
            success, response = self._call_llm(
                model=MODEL_NAME,
                messages=self.memory.messages,
            )

            if not success:
                answer = response

                self.memory.add_assistant(answer)

                return answer

            answer = response.choices[0].message.content or ""

            self.memory.add_assistant(answer)

            return answer

        # =========================
        # 11. Fast Tool Agent Loop
        # =========================

        while True:
            success, response = self._call_llm(
                model=MODEL_NAME,
                messages=self.memory.messages,
                tools=available_tools,
                tool_choice="auto",
            )

            if not success:
                answer = response

                self.memory.add_assistant(answer)

                return answer

            message = response.choices[0].message

            # =====================
            # Final Answer
            # =====================

            if not message.tool_calls:
                answer = message.content or ""

                valid, checked_answer = self.validator.validate(
                    answer,
                    retrieved_evidence,
                    last_tool,
                )

                if not valid:
                    answer = checked_answer

                self.memory.add_assistant(answer)

                return answer

            # =====================
            # Save Tool Call
            # =====================

            self.memory.add_raw(message.model_dump(exclude_none=True))

            # =====================
            # Execute Tool
            # =====================

            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name

                last_tool = tool_name

                try:
                    arguments = json.loads(tool_call.function.arguments)

                except Exception as error:
                    print(f"[Error][ToolArguments] {error}")

                    answer = "工具调用参数异常，暂时无法完成该请求。"

                    self.memory.add_assistant(answer)

                    return answer

                print(f"[Tool] {tool_name}")

                tool_success, result = self.runtime.call_tool(
                    tool_name,
                    self._execute_tool,
                    tool_name,
                    arguments,
                )

                if not tool_success:
                    print(f"[Tool Error] {result}")

                    answer = str(result)

                    self.memory.add_assistant(answer)

                    return answer

                print(f"[Result] {result}")

                if tool_name == "knowledge_search":
                    retrieved_evidence = result

                if isinstance(
                    result,
                    (dict, list),
                ):
                    tool_content = json.dumps(
                        result,
                        ensure_ascii=False,
                    )

                else:
                    tool_content = str(result)

                self.memory.add_raw(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_content,
                    }
                )

    def reset(self):

        self.memory.reset()
