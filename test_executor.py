import json

from app.agent.planner import TaskPlanner

from app.agent.executor import TaskExecutor


def run_case(
    planner,
    executor,
    user_input,
):

    print("\n" + "=" * 70)

    print(f"User: {user_input}")

    # =========================
    # Planner
    # =========================

    plan = planner.plan(user_input)

    print("\nPlan:")

    print(
        json.dumps(
            plan,
            ensure_ascii=False,
            indent=2,
        )
    )

    # =========================
    # Executor
    # =========================

    execution = executor.execute_plan(plan)

    print("\nExecution:")

    print(
        json.dumps(
            execution,
            ensure_ascii=False,
            indent=2,
        )
    )


def main():

    planner = TaskPlanner()

    executor = TaskExecutor()

    test_cases = [
        ("帮我算一下25乘以8，然后告诉我公司报销多久到账？"),
        "公司年假有多少天？",
        "我现在负责什么项目？",
        "你好",
    ]

    for user_input in test_cases:
        run_case(
            planner,
            executor,
            user_input,
        )


if __name__ == "__main__":
    main()
