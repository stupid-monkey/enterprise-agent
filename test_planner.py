import json

from app.agent.planner import TaskPlanner


def test_case(
    planner,
    text,
):

    print("\n" + "=" * 60)

    print(f"User: {text}")

    plan = planner.plan(text)

    print("\nPlan:")

    print(
        json.dumps(
            plan,
            ensure_ascii=False,
            indent=2,
        )
    )


def main():

    planner = TaskPlanner()

    test_cases = [
        "帮我算一下25乘以8，然后告诉我公司报销多久到账？",
        "公司年假有多少天？",
        "我现在负责什么项目？",
        "你好",
    ]

    for text in test_cases:
        test_case(
            planner,
            text,
        )


if __name__ == "__main__":
    main()
