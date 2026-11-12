import json

from app.agent.planner import TaskPlanner

from app.agent.executor import TaskExecutor

from app.agent.synthesizer import FinalSynthesizer


def run_case(
    planner,
    executor,
    synthesizer,
    user_input,
):

    print("\n" + "=" * 70)

    print(f"User: {user_input}")

    # =========================
    # 1. Planner
    # =========================

    plan = planner.plan(user_input)

    print("\n[Plan]")

    print(
        json.dumps(
            plan,
            ensure_ascii=False,
            indent=2,
        )
    )

    # =========================
    # 2. Executor
    # =========================

    execution = executor.execute_plan(plan)

    print("\n[Execution]")

    print(
        json.dumps(
            execution,
            ensure_ascii=False,
            indent=2,
        )
    )

    # =========================
    # 3. Final Synthesizer
    # =========================

    answer = synthesizer.synthesize(
        user_input=user_input,
        plan=plan,
        execution=execution,
    )

    print("\n[Final Answer]")

    print(answer)


def main():

    planner = TaskPlanner()

    executor = TaskExecutor()

    synthesizer = FinalSynthesizer()

    # 先测试最重要的复合任务，
    # 避免一次消耗太多 API 调用。

    user_input = "帮我算一下25乘以8，然后告诉我公司报销多久到账？"

    run_case(
        planner,
        executor,
        synthesizer,
        user_input,
    )


if __name__ == "__main__":
    main()
