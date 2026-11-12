import json

import sys

from pathlib import Path


# =========================
# Project Root
# =========================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(
        0,
        str(PROJECT_ROOT),
    )


from app.router.intent_router import IntentRouter

from app.agent.planner import TaskPlanner


EVAL_DIR = Path(__file__).resolve().parent


def load_json(
    filename: str,
):

    path = EVAL_DIR / filename

    with open(
        path,
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def evaluate_router():

    print("\n" + "=" * 70)

    print("Router Evaluation")

    print("=" * 70)

    router = IntentRouter()

    cases = load_json("router_cases.json")

    total = len(cases)

    correct = 0

    failures = []

    for index, case in enumerate(
        cases,
        start=1,
    ):
        user_input = case["input"]

        expected = case["expected"]

        actual = router.route(user_input)

        passed = actual == expected

        if passed:
            correct += 1

            status = "PASS"

        else:
            status = "FAIL"

            failures.append(
                {
                    "input": user_input,
                    "expected": expected,
                    "actual": actual,
                }
            )

        print(f"[{status}] {index:02d} {user_input}")

        if not passed:
            print(f"       expected={expected}")

            print(f"       actual={actual}")

    accuracy = correct / total if total else 0.0

    print("\nRouter Result")

    print(f"Correct : {correct}")

    print(f"Total   : {total}")

    print(f"Accuracy: {accuracy:.2%}")

    return {
        "name": "router",
        "correct": correct,
        "total": total,
        "accuracy": accuracy,
        "failures": failures,
    }


def extract_actions(
    plan: dict,
):

    steps = plan.get(
        "steps",
        [],
    )

    actions = []

    for step in steps:
        if not isinstance(
            step,
            dict,
        ):
            continue

        action = step.get("action")

        if action:
            actions.append(action)

    return actions


def evaluate_planner():

    print("\n" + "=" * 70)

    print("Planner Evaluation")

    print("=" * 70)

    print("注意：Planner Evaluation 会调用 DeepSeek API。\n")

    planner = TaskPlanner()

    cases = load_json("planner_cases.json")

    total = len(cases)

    correct = 0

    action_correct = 0

    multistep_correct = 0

    failures = []

    for index, case in enumerate(
        cases,
        start=1,
    ):
        user_input = case["input"]

        expected_actions = case["expected_actions"]

        expected_multistep = case["expected_multistep"]

        try:
            plan = planner.plan(user_input)

            actual_actions = extract_actions(plan)

            actual_multistep = bool(
                plan.get(
                    "is_multistep",
                    False,
                )
            )

            actions_pass = actual_actions == expected_actions

            multistep_pass = actual_multistep == expected_multistep

            passed = actions_pass and multistep_pass

            if actions_pass:
                action_correct += 1

            if multistep_pass:
                multistep_correct += 1

            if passed:
                correct += 1

                status = "PASS"

            else:
                status = "FAIL"

                failures.append(
                    {
                        "input": user_input,
                        "expected_actions": expected_actions,
                        "actual_actions": actual_actions,
                        "expected_multistep": expected_multistep,
                        "actual_multistep": actual_multistep,
                    }
                )

            print(f"[{status}] {index:02d} {user_input}")

            print(f"       expected actions : {expected_actions}")

            print(f"       actual actions   : {actual_actions}")

            print(f"       expected multi   : {expected_multistep}")

            print(f"       actual multi     : {actual_multistep}")

        except Exception as error:
            failures.append(
                {
                    "input": user_input,
                    "error": (f"{type(error).__name__}: {error}"),
                }
            )

            print(f"[ERROR] {index:02d} {user_input}")

            print(f"        {type(error).__name__}: {error}")

    accuracy = correct / total if total else 0.0

    action_accuracy = action_correct / total if total else 0.0

    multistep_accuracy = multistep_correct / total if total else 0.0

    print("\nPlanner Result")

    print(f"Exact Correct      : {correct}/{total}")

    print(f"Exact Accuracy     : {accuracy:.2%}")

    print(f"Action Accuracy    : {action_accuracy:.2%}")

    print(f"Multistep Accuracy : {multistep_accuracy:.2%}")

    return {
        "name": "planner",
        "correct": correct,
        "total": total,
        "accuracy": accuracy,
        "action_accuracy": action_accuracy,
        "multistep_accuracy": multistep_accuracy,
        "failures": failures,
    }


def save_report(
    report: dict,
):

    output_path = EVAL_DIR / "evaluation_results.json"

    with open(
        output_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            report,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print("\nEvaluation report saved:")

    print(output_path)


def main():

    mode = "all"

    if len(sys.argv) >= 2:
        mode = sys.argv[1].strip().lower()

    results = {}

    if mode in {
        "router",
        "all",
    }:
        results["router"] = evaluate_router()

    if mode in {
        "planner",
        "all",
    }:
        results["planner"] = evaluate_planner()

    if mode not in {
        "router",
        "planner",
        "all",
    }:
        print("Usage:")

        print("python eval/evaluate_agent.py router")

        print("python eval/evaluate_agent.py planner")

        print("python eval/evaluate_agent.py all")

        return

    save_report(results)


if __name__ == "__main__":
    main()
