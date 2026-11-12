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


def get_section(
    result: dict,
) -> str:

    metadata = result.get(
        "metadata",
        {},
    )

    return metadata.get(
        "section",
        "",
    )


def section_matches(
    actual: str,
    expected: str,
) -> bool:

    actual = normalize_section(actual)

    expected = normalize_section(expected)

    return expected in actual or actual in expected


def evaluate():

    print("\n" + "=" * 70)

    print("RAG Retrieval Evaluation")

    print("=" * 70)

    retriever = KnowledgeRetriever()

    cases = load_cases()

    positive_total = 0
    positive_top1_correct = 0
    positive_hit_k_correct = 0

    ood_total = 0
    ood_rejected = 0

    overall_correct = 0

    failures = []

    for index, case in enumerate(
        cases,
        start=1,
    ):
        query = case["input"]

        should_find = case["should_find"]

        expected_section = case.get("expected_section")

        print("\n" + "-" * 70)

        print(f"[{index:02d}] {query}")

        try:
            results = retriever.search(
                query=query,
                top_k=3,
            )

        except Exception as error:
            print(f"[ERROR] {type(error).__name__}: {error}")

            failures.append(
                {
                    "input": query,
                    "error": (f"{type(error).__name__}: {error}"),
                }
            )

            continue

        # =========================
        # Print retrieval results
        # =========================

        if results:
            for rank, result in enumerate(
                results,
                start=1,
            ):
                section = get_section(result)

                distance = result.get("distance")

                print(f"Rank {rank}: section={section}, distance={distance}")

        else:
            print("No result passed threshold.")

        # =========================
        # Positive Case
        # =========================

        if should_find:
            positive_total += 1

            if not results:
                print("[FAIL] Expected relevant result, but nothing was retrieved.")

                failures.append(
                    {
                        "input": query,
                        "type": "false_rejection",
                        "expected_section": expected_section,
                        "actual_sections": [],
                    }
                )

                continue

            actual_sections = [get_section(result) for result in results]

            top1_match = section_matches(
                actual_sections[0],
                expected_section,
            )

            hit_k = any(
                section_matches(
                    section,
                    expected_section,
                )
                for section in actual_sections
            )

            if top1_match:
                positive_top1_correct += 1

            if hit_k:
                positive_hit_k_correct += 1

            if hit_k:
                overall_correct += 1

                print("[PASS] Relevant section retrieved.")

            else:
                print("[FAIL] Wrong section retrieved.")

                failures.append(
                    {
                        "input": query,
                        "type": "wrong_retrieval",
                        "expected_section": expected_section,
                        "actual_sections": actual_sections,
                    }
                )

        # =========================
        # OOD Case
        # =========================

        else:
            ood_total += 1

            if not results:
                ood_rejected += 1

                overall_correct += 1

                print("[PASS] OOD query correctly rejected.")

            else:
                actual_sections = [get_section(result) for result in results]

                print("[FAIL] OOD query retrieved knowledge unexpectedly.")

                failures.append(
                    {
                        "input": query,
                        "type": "false_accept",
                        "actual_sections": actual_sections,
                        "distances": [result.get("distance") for result in results],
                    }
                )

    # =========================
    # Metrics
    # =========================

    top1_accuracy = positive_top1_correct / positive_total if positive_total else 0.0

    recall_at_k = positive_hit_k_correct / positive_total if positive_total else 0.0

    ood_rejection_rate = ood_rejected / ood_total if ood_total else 0.0

    overall_accuracy = overall_correct / len(cases) if cases else 0.0

    print("\n" + "=" * 70)

    print("RAG Evaluation Result")

    print("=" * 70)

    print(f"Positive Queries     : {positive_total}")

    print(f"Top-1 Correct        : {positive_top1_correct}/{positive_total}")

    print(f"Top-1 Accuracy       : {top1_accuracy:.2%}")

    print(f"Hit@3               : {positive_hit_k_correct}/{positive_total}")

    print(f"Recall@3             : {recall_at_k:.2%}")

    print(f"OOD Queries          : {ood_total}")

    print(f"OOD Rejected         : {ood_rejected}/{ood_total}")

    print(f"OOD Rejection Rate   : {ood_rejection_rate:.2%}")

    print(f"Overall Success Rate : {overall_accuracy:.2%}")

    report = {
        "positive_total": positive_total,
        "top1_correct": positive_top1_correct,
        "top1_accuracy": top1_accuracy,
        "hit_at_3": positive_hit_k_correct,
        "recall_at_3": recall_at_k,
        "ood_total": ood_total,
        "ood_rejected": ood_rejected,
        "ood_rejection_rate": ood_rejection_rate,
        "overall_success_rate": overall_accuracy,
        "failures": failures,
    }

    output_path = EVAL_DIR / "rag_evaluation_results.json"

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
    evaluate()
