from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from .extraction import DirectionExtractor, normalize_name


def word_error_count(reference: str, hypothesis: str) -> tuple[int, int]:
    ref = normalize_name(reference).split()
    hyp = normalize_name(hypothesis).split()
    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + (ref_word != hyp_word),
                )
            )
        previous = current
    return previous[-1], len(ref)


def evaluate(rows: list[dict]) -> dict:
    extractor = DirectionExtractor()
    totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "word_errors": 0,
            "reference_words": 0,
            "landmark_hits": 0,
            "landmark_total": 0,
            "relation_hits": 0,
            "relation_total": 0,
            "route_successes": 0,
            "samples": 0,
            "latency_ms": 0,
        }
    )
    for row in rows:
        expected_landmarks = {normalize_name(item) for item in row.get("landmarks", [])}
        expected_relations = set(row.get("relations", []))
        for model, value in row.get("hypotheses", {}).items():
            if isinstance(value, str):
                hypothesis = value
                latency_ms = 0
            else:
                hypothesis = value.get("transcript", "")
                latency_ms = float(value.get("latency_ms", 0))
            errors, words = word_error_count(row.get("reference", ""), hypothesis)
            extraction = extractor.extract(hypothesis)
            actual_landmarks = {item.normalized_name for item in extraction.landmarks}
            actual_relations = {item.relation_type.value for item in extraction.relations}
            landmark_hits = len(expected_landmarks & actual_landmarks)
            relation_hits = len(expected_relations & actual_relations)
            route_success = (
                expected_landmarks <= actual_landmarks and expected_relations <= actual_relations
            )
            metric = totals[model]
            metric["word_errors"] += errors
            metric["reference_words"] += words
            metric["landmark_hits"] += landmark_hits
            metric["landmark_total"] += len(expected_landmarks)
            metric["relation_hits"] += relation_hits
            metric["relation_total"] += len(expected_relations)
            metric["route_successes"] += int(route_success)
            metric["samples"] += 1
            metric["latency_ms"] += latency_ms

    report = {"models": {}, "sample_count": len(rows)}
    for model, value in sorted(totals.items()):
        report["models"][model] = {
            "wer": round(value["word_errors"] / max(1, value["reference_words"]), 4),
            "landmark_recall": round(
                value["landmark_hits"] / max(1, value["landmark_total"]), 4
            ),
            "relation_accuracy": round(
                value["relation_hits"] / max(1, value["relation_total"]), 4
            ),
            "route_extraction_success": round(
                value["route_successes"] / max(1, value["samples"]), 4
            ),
            "mean_latency_ms": round(value["latency_ms"] / max(1, value["samples"]), 1),
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark STT output on Waymark route semantics")
    parser.add_argument("input", type=Path, help="JSONL benchmark corpus")
    parser.add_argument("--output", type=Path, help="write the JSON report to this path")
    args = parser.parse_args()
    rows = [
        json.loads(line)
        for line in args.input.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = json.dumps(evaluate(rows), indent=2)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()

