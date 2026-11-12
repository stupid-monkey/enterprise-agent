import json
import re
import sys
import time

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


from app.agent.core import EnterpriseAgent


EVAL_DIR = Path(__file__).resolve().parent

PROFILE_PATH = PROJECT_ROOT / "data" / "user_profile.json"


# =========================
# Controlled Test Profile
# =========================

TEST_PROFILE = {
    "role": "算法工程师",
    "field": "计算机视觉方向",
    "task": "图像分割项目",
    "answer_preference": "以后回答尽量简洁一点",
}


def load_cases():

    path = EVAL_DIR / "e2e_cases.json"

    with open(
        path,
        "r",
        encoding="utf-8",
    ) as file:
        cases = json.load(file)

        print("Loaded cases:", len(cases))
        return cases


def backup_profile():

    if not PROFILE_PATH.exists():
        return None

    return PROFILE_PATH.read_text(encoding="utf-8")


def install_test_profile():

    PROFILE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open(
        PROFILE_PATH,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            TEST_PROFILE,
            file,
            ensure_ascii=False,
            indent=2,
        )


def restore_profile(
    original_content,
):

    if original_content is None:
        if PROFILE_PATH.exists():
            PROFILE_PATH.unlink()

        return

    PROFILE_PATH.write_text(
        original_content,
        encoding="utf-8",
    )


def normalize_text(
    text: str,
) -> str:
    """
    去掉空格和换行。

    例如：

    5 个工作日
    5个工作日

    在测试时视为相同。
    """

    text = text.lower()

    text = re.sub(
        r"\s+",
        "",
        text,
    )

    return text


def contains(
    answer: str,
    keyword: str,
) -> bool:

    return normalize_text(keyword) in normalize_text(answer)


def check_case(
    case: dict,
    answer: str,
) -> dict:

    must_all = case.get(
        "must_contain_all",
        [],
    )

    must_any = case.get(
        "must_contain_any",
        [],
    )

    must_not = case.get(
        "must_not_contain",
        [],
    )

    # =========================
    # Must Contain All
    # =========================

    missing_required = [
        keyword
        for keyword in must_all
        if not contains(
            answer,
            keyword,
        )
    ]

    all_pass = len(missing_required) == 0

    # =========================
    # Must Contain Any
    # =========================

    if must_any:
        matched_any = [
            keyword
            for keyword in must_any
            if contains(
                answer,
                keyword,
            )
        ]

        any_pass = bool(matched_any)

    else:
        matched_any = []

        any_pass = True

    # =========================
    # Forbidden Content
    # =========================

    forbidden_found = [
        keyword
        for keyword in must_not
        if contains(
            answer,
            keyword,
        )
    ]

    forbidden_pass = len(forbidden_found) == 0

    passed = all_pass and any_pass and forbidden_pass

    return {
        "passed": passed,
        "missing_required": missing_required,
        "matched_any": matched_any,
        "forbidden_found": forbidden_found,
    }


def calculate_category_metrics(
    results,
):

    categories = {}

    for item in results:
        category = item["category"]

        if category not in categories:
            categories[category] = {
                "correct": 0,
                "total": 0,
            }

        categories[category]["total"] += 1

        if item["passed"]:
            categories[category]["correct"] += 1

    for category, metrics in categories.items():
        total = metrics["total"]

        correct = metrics["correct"]

        metrics["accuracy"] = correct / total if total else 0.0

    return categories


def main():

    cases = load_cases()

    original_profile = backup_profile()

    results = []

    agent = None

    try:
        # =========================
        # Controlled Memory Fixture
        # =========================

        install_test_profile()

        print("\n" + "=" * 70)

        print("End-to-End Agent Evaluation")

        print("=" * 70)

        print("\nControlled test profile:")

        print(
            json.dumps(
                TEST_PROFILE,
                ensure_ascii=False,
                indent=2,
            )
        )

        print("\nInitializing EnterpriseAgent...")

        agent = EnterpriseAgent()

        # =========================
        # Run Cases
        # =========================

        for index, case in enumerate(
            cases,
            start=1,
        ):
            print("\n" + "-" * 70)

            print(f"[{index:02d}] {case['id']}")

            print(f"Category: {case['category']}")

            print(f"User: {case['input']}")

            # 每个 case 独立，
            # 防止短期对话历史污染结果。
            agent.reset()

            start_time = time.perf_counter()

            try:
                answer = agent.run(case["input"])

                error = None

            except Exception as exc:
                answer = ""

                error = f"{type(exc).__name__}: {exc}"

            elapsed = time.perf_counter() - start_time

            if error is None:
                check = check_case(
                    case,
                    answer,
                )

                passed = check["passed"]

            else:
                check = {
                    "passed": False,
                    "missing_required": [],
                    "matched_any": [],
                    "forbidden_found": [],
                }

                passed = False

            status = "PASS" if passed else "FAIL"

            print(f"\n[{status}]")

            print(f"Latency: {elapsed:.2f}s")

            if error:
                print(f"Error: {error}")

            else:
                print("\nAgent Answer:")

                print(answer)

            if check["missing_required"]:
                print("\nMissing required:")

                print(check["missing_required"])

            if case.get("must_contain_any") and not check["matched_any"]:
                print("\nNo expected rejection phrase matched.")

            if check["forbidden_found"]:
                print("\nForbidden content found:")

                print(check["forbidden_found"])

            results.append(
                {
                    "id": case["id"],
                    "category": case["category"],
                    "input": case["input"],
                    "passed": passed,
                    "latency_seconds": round(
                        elapsed,
                        4,
                    ),
                    "answer": answer,
                    "error": error,
                    "check": check,
                }
            )

    finally:
        # =========================
        # Restore Real Profile
        # =========================

        restore_profile(original_profile)

        print("\nOriginal user profile restored.")

    # =========================
    # Metrics
    # =========================

    total = len(results)

    correct = sum(1 for item in results if item["passed"])

    overall_accuracy = correct / total if total else 0.0

    category_metrics = calculate_category_metrics(results)

    latencies = [item["latency_seconds"] for item in results]

    average_latency = sum(latencies) / len(latencies) if latencies else 0.0

    print("\n" + "=" * 70)

    print("End-to-End Evaluation Result")

    print("=" * 70)

    print(f"Overall Correct      : {correct}/{total}")

    print(f"Overall Success Rate : {overall_accuracy:.2%}")

    print(f"Average Latency      : {average_latency:.2f}s")

    for category, metrics in category_metrics.items():
        print(
            f"{category.capitalize():<20}: "
            f"{metrics['correct']}"
            f"/{metrics['total']} "
            f"({metrics['accuracy']:.2%})"
        )

    # =========================
    # Failed Cases
    # =========================

    failures = [item for item in results if not item["passed"]]

    if failures:
        print("\nFailed Cases:")

        for item in failures:
            print(f"- {item['id']}: {item['input']}")

    # =========================
    # Save Report
    # =========================

    output_path = EVAL_DIR / "e2e_evaluation_results.json"

    report = {
        "overall": {
            "correct": correct,
            "total": total,
            "success_rate": overall_accuracy,
            "average_latency_seconds": average_latency,
        },
        "categories": category_metrics,
        "results": results,
    }

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

    print("\nReport saved:")

    print(output_path)


if __name__ == "__main__":
    main()
