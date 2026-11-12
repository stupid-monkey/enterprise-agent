import json
import sys

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(
        0,
        str(PROJECT_ROOT),
    )


from app.rag.retriever import KnowledgeRetriever


EVAL_DIR = Path(__file__).resolve().parent


def load_cases():

    path = EVAL_DIR / "rag_cases.json"

    with open(
        path,
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def normalize_section(
    section: str,
) -> str:

    if not section:
        return ""

    return section.replace("：", "").replace(":", "").strip()


def section_matches(
    actual: str,
    expected: str,
) -> bool:

    actual = normalize_section(actual)

    expected = normalize_section(expected)

    return actual == expected or expected in actual or actual in expected


def collect_raw_results(
    retriever,
    cases,
):

    dataset = []

    print("\nCollecting raw retrieval results...")

    for index, case in enumerate(
        cases,
        start=1,
    ):
        query = case["input"]

        print("\n" + "-" * 70)

        print(f"[{index:02d}] {query}")

        results = retriever.search_raw(
            query=query,
            top_k=3,
        )

        dataset.append(
            {
                "input": query,
                "should_find": case["should_find"],
                "expected_section": case.get("expected_section"),
                "results": results,
            }
        )

    return dataset


def evaluate_threshold(
    dataset,
    threshold,
):

    positive_total = 0

    positive_hit = 0

    ood_total = 0

    ood_rejected = 0

    overall_correct = 0

    for case in dataset:
        raw_results = case["results"]

        accepted = [
            result for result in raw_results if (result["distance"] <= threshold)
        ]

        should_find = case["should_find"]

        # =========================
        # Positive
        # =========================

        if should_find:
            positive_total += 1

            expected = case["expected_section"]

            hit = False

            for result in accepted:
                metadata = result.get("metadata", {})

                section = metadata.get("section", "")

                if section_matches(
                    section,
                    expected,
                ):
                    hit = True
                    break

            if hit:
                positive_hit += 1

                overall_correct += 1

        # =========================
        # OOD
        # =========================

        else:
            ood_total += 1

            if not accepted:
                ood_rejected += 1

                overall_correct += 1

    positive_recall = positive_hit / positive_total if positive_total else 0.0

    ood_rejection = ood_rejected / ood_total if ood_total else 0.0

    overall_accuracy = overall_correct / len(dataset) if dataset else 0.0

    balanced_accuracy = (positive_recall + ood_rejection) / 2

    return {
        "threshold": threshold,
        "positive_recall": positive_recall,
        "ood_rejection": ood_rejection,
        "balanced_accuracy": balanced_accuracy,
        "overall_accuracy": overall_accuracy,
        "positive_hit": positive_hit,
        "positive_total": positive_total,
        "ood_rejected": ood_rejected,
        "ood_total": ood_total,
    }


def print_distance_analysis(
    dataset,
):

    print("\n" + "=" * 70)

    print("Top-1 Distance Analysis")

    print("=" * 70)

    positive_distances = []

    ood_distances = []

    for case in dataset:
        if not case["results"]:
            continue

        top1_distance = case["results"][0]["distance"]

        if case["should_find"]:
            positive_distances.append(top1_distance)

        else:
            ood_distances.append(top1_distance)

    if positive_distances:
        print("Positive Top-1:")

        print(f"  min = {min(positive_distances):.4f}")

        print(f"  max = {max(positive_distances):.4f}")

        print(f"  mean = {sum(positive_distances) / len(positive_distances):.4f}")

    if ood_distances:
        print("\nOOD Top-1:")

        print(f"  min = {min(ood_distances):.4f}")

        print(f"  max = {max(ood_distances):.4f}")

        print(f"  mean = {sum(ood_distances) / len(ood_distances):.4f}")


def main():

    cases = load_cases()

    retriever = KnowledgeRetriever()

    dataset = collect_raw_results(
        retriever,
        cases,
    )

    print_distance_analysis(dataset)

    sweep_results = []

    # =========================
    # Fine-grained sweep
    # 0.300 -> 0.600
    # step = 0.001
    # =========================

    start = 300

    end = 600

    for value in range(
        start,
        end + 1,
    ):
        threshold = value / 1000

        metrics = evaluate_threshold(
            dataset,
            threshold,
        )

        sweep_results.append(metrics)

    # =========================
    # Best Balanced Accuracy
    # =========================

    best_balanced = max(
        sweep_results,
        key=lambda item: (
            item["balanced_accuracy"],
            item["ood_rejection"],
            item["positive_recall"],
        ),
    )

    # =========================
    # Best Overall Accuracy
    # =========================

    best_overall = max(
        sweep_results,
        key=lambda item: (
            item["overall_accuracy"],
            item["ood_rejection"],
            item["positive_recall"],
        ),
    )

    print("\n" + "=" * 70)

    print("Threshold Sweep Result")

    print("=" * 70)

    print("\nBest Balanced Threshold")

    print(f"Threshold         : {best_balanced['threshold']:.3f}")

    print(f"Positive Recall   : {best_balanced['positive_recall']:.2%}")

    print(f"OOD Rejection     : {best_balanced['ood_rejection']:.2%}")

    print(f"Balanced Accuracy : {best_balanced['balanced_accuracy']:.2%}")

    print(f"Overall Accuracy  : {best_balanced['overall_accuracy']:.2%}")

    print("\nBest Overall Threshold")

    print(f"Threshold         : {best_overall['threshold']:.3f}")

    print(f"Positive Recall   : {best_overall['positive_recall']:.2%}")

    print(f"OOD Rejection     : {best_overall['ood_rejection']:.2%}")

    print(f"Balanced Accuracy : {best_overall['balanced_accuracy']:.2%}")

    print(f"Overall Accuracy  : {best_overall['overall_accuracy']:.2%}")

    output_path = EVAL_DIR / "threshold_sweep_results.json"

    report = {
        "best_balanced": best_balanced,
        "best_overall": best_overall,
        "all_thresholds": sweep_results,
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
