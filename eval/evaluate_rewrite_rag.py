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
from app.rag.query_rewriter import QueryRewriter


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


def collect_results(
    retriever,
    rewriter,
    cases,
):

    dataset = []

    for index, case in enumerate(
        cases,
        start=1,
    ):
        query = case["input"]

        print("\n" + "=" * 70)

        print(f"[{index:02d}] {query}")

        # =========================
        # Raw Query
        # =========================

        raw_results = retriever.search_raw(
            query=query,
            top_k=3,
        )

        # =========================
        # Rewrite
        # =========================

        rewritten = rewriter.rewrite(query)

        print(f"[Rewrite] {rewritten}")

        combined_query = f"{query} {rewritten}"

        print(f"[Combined Query] {combined_query}")

        # =========================
        # Rewritten Retrieval
        # =========================

        rewritten_results = retriever.search_raw(
            query=combined_query,
            top_k=3,
        )

        raw_top1 = raw_results[0]["distance"] if raw_results else None

        rewritten_top1 = rewritten_results[0]["distance"] if rewritten_results else None

        print(f"[Distance] raw={raw_top1} | rewritten={rewritten_top1}")

        dataset.append(
            {
                "input": query,
                "should_find": case["should_find"],
                "expected_section": case.get("expected_section"),
                "rewritten": rewritten,
                "combined_query": combined_query,
                "raw_results": raw_results,
                "rewritten_results": rewritten_results,
            }
        )

    return dataset


def evaluate_threshold(
    dataset,
    threshold,
    result_key,
):

    positive_total = 0

    positive_hit = 0

    ood_total = 0

    ood_rejected = 0

    overall_correct = 0

    for case in dataset:
        raw_results = case[result_key]

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
                metadata = result.get(
                    "metadata",
                    {},
                )

                section = metadata.get(
                    "section",
                    "",
                )

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

    balanced_accuracy = (positive_recall + ood_rejection) / 2

    overall_accuracy = overall_correct / len(dataset) if dataset else 0.0

    return {
        "threshold": threshold,
        "positive_recall": positive_recall,
        "ood_rejection": ood_rejection,
        "balanced_accuracy": balanced_accuracy,
        "overall_accuracy": overall_accuracy,
    }


def find_best_threshold(
    dataset,
    result_key,
):

    results = []

    for value in range(
        300,
        601,
    ):
        threshold = value / 1000

        metrics = evaluate_threshold(
            dataset,
            threshold,
            result_key,
        )

        results.append(metrics)

    best = max(
        results,
        key=lambda item: (
            item["balanced_accuracy"],
            item["ood_rejection"],
            item["positive_recall"],
        ),
    )

    return best, results


def print_distance_summary(
    dataset,
    key,
    title,
):

    positive = []

    ood = []

    for case in dataset:
        results = case[key]

        if not results:
            continue

        distance = results[0]["distance"]

        if case["should_find"]:
            positive.append(distance)

        else:
            ood.append(distance)

    print("\n" + "=" * 70)

    print(title)

    print("=" * 70)

    if positive:
        print("Positive Top-1:")

        print(f"  min  = {min(positive):.4f}")

        print(f"  max  = {max(positive):.4f}")

        print(f"  mean = {sum(positive) / len(positive):.4f}")

    if ood:
        print("\nOOD Top-1:")

        print(f"  min  = {min(ood):.4f}")

        print(f"  max  = {max(ood):.4f}")

        print(f"  mean = {sum(ood) / len(ood):.4f}")


def compare_positive_distances(
    dataset,
):

    print("\n" + "=" * 70)

    print("Positive Query Distance Change")

    print("=" * 70)

    improved = 0
    worsened = 0
    unchanged = 0

    for case in dataset:
        if not case["should_find"]:
            continue

        raw_results = case["raw_results"]

        rewritten_results = case["rewritten_results"]

        if not raw_results or not rewritten_results:
            continue

        raw_distance = raw_results[0]["distance"]

        rewritten_distance = rewritten_results[0]["distance"]

        delta = rewritten_distance - raw_distance

        if delta < -0.001:
            status = "IMPROVED"
            improved += 1

        elif delta > 0.001:
            status = "WORSENED"
            worsened += 1

        else:
            status = "UNCHANGED"
            unchanged += 1

        print(f"[{status}] {case['input']}")

        print(
            f"    raw="
            f"{raw_distance:.4f}, "
            f"rewrite="
            f"{rewritten_distance:.4f}, "
            f"delta="
            f"{delta:+.4f}"
        )

    print("\nPositive Summary:")

    print(f"Improved : {improved}")

    print(f"Worsened : {worsened}")

    print(f"Unchanged: {unchanged}")


def main():

    cases = load_cases()

    retriever = KnowledgeRetriever()

    rewriter = QueryRewriter()

    dataset = collect_results(
        retriever,
        rewriter,
        cases,
    )

    # =========================
    # Distance Distributions
    # =========================

    print_distance_summary(
        dataset,
        "raw_results",
        "Raw Query Distance Analysis",
    )

    print_distance_summary(
        dataset,
        "rewritten_results",
        "Rewritten Query Distance Analysis",
    )

    compare_positive_distances(dataset)

    # =========================
    # Threshold Sweep
    # =========================

    raw_best, raw_all = find_best_threshold(
        dataset,
        "raw_results",
    )

    rewritten_best, rewritten_all = find_best_threshold(
        dataset,
        "rewritten_results",
    )

    print("\n" + "=" * 70)

    print("Raw vs Rewritten Threshold Result")

    print("=" * 70)

    print("\nRAW QUERY")

    print(f"Threshold         : {raw_best['threshold']:.3f}")

    print(f"Positive Recall   : {raw_best['positive_recall']:.2%}")

    print(f"OOD Rejection     : {raw_best['ood_rejection']:.2%}")

    print(f"Balanced Accuracy : {raw_best['balanced_accuracy']:.2%}")

    print(f"Overall Accuracy  : {raw_best['overall_accuracy']:.2%}")

    print("\nREWRITTEN QUERY")

    print(f"Threshold         : {rewritten_best['threshold']:.3f}")

    print(f"Positive Recall   : {rewritten_best['positive_recall']:.2%}")

    print(f"OOD Rejection     : {rewritten_best['ood_rejection']:.2%}")

    print(f"Balanced Accuracy : {rewritten_best['balanced_accuracy']:.2%}")

    print(f"Overall Accuracy  : {rewritten_best['overall_accuracy']:.2%}")

    # =========================
    # Save
    # =========================

    output_path = EVAL_DIR / "rewrite_rag_results.json"

    report = {
        "raw_best": raw_best,
        "rewritten_best": rewritten_best,
        "cases": dataset,
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
