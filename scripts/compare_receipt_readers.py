"""Compare two frozen receipt readers on paired held-out measurements, without refitting."""

import argparse
import json
from collections import Counter
from pathlib import Path

from scripts.evaluate_verification_value import ARMS, ROUTES, digest, partition, score, validate_cases


CASE_FIELDS = ("id", "group", "split", "stratum", "truth", "budget_units", "deadline_ms")


def split_records(dataset, split):
    records = dataset.get("records") if isinstance(dataset, dict) else dataset
    if not isinstance(records, list) or any(not isinstance(row, dict) or row.get("split") != split for row in records):
        raise ValueError(f"expected a {split} measurement file")
    return records


def exclusions(metadata):
    rows = metadata.get("excluded", [])
    reasons = {row["id"]: row.get("reason") for row in rows}
    if len(reasons) != len(rows):
        raise ValueError("duplicate excluded document")
    return reasons


def paired_splits(old_calibration, new_calibration, old_holdout, new_holdout, protocol):
    old_records, new_records = [], []
    seen_excluded = set()
    for split, before, after in (("calibration", old_calibration, new_calibration),
                                 ("holdout", old_holdout, new_holdout)):
        old = split_records(before, split)
        new = split_records(after, split)
        validate_cases(old, protocol)
        validate_cases(new, protocol)
        old_metadata = before.get("metadata", {}) if isinstance(before, dict) else {}
        new_metadata = after.get("metadata", {}) if isinstance(after, dict) else {}
        skipped = exclusions(old_metadata)
        if skipped != exclusions(new_metadata) or seen_excluded.intersection(skipped):
            raise ValueError("excluded cohorts differ or overlap")
        seen_excluded.update(skipped)
        for field in ("revision", "source_sha256", "ocr", "eng_sha256"):
            if old_metadata.get(field) != new_metadata.get(field):
                raise ValueError(f"{split} readers differ in {field}")
        old_records.extend(old)
        new_records.extend(new)
    before, after = partition(old_records), partition(new_records)
    for split, rows in before.items():
        if len(rows) != len(after[split]) or any(
            a[key] != b[key] for a, b in zip(rows, after[split]) for key in CASE_FIELDS
        ):
            raise ValueError(f"{split} case identities, labels or operating conditions do not match")
    if any(row["id"] in seen_excluded for row in old_records):
        raise ValueError("document is both excluded and eligible")
    return before, after


def outcome(delivered, correct):
    if not delivered:
        return "abstained"
    return "correct" if correct else "wrong"


def raw_outcomes(cases, route):
    outcomes = []
    for row in cases:
        answer = row["actions"][route]["answer"]
        outcomes.append(outcome(answer is not None, answer == row["truth"]))
    return outcomes


def dispatch_outcomes(report, arm):
    results = (row["arms"][arm] for row in report["runs"])
    return [outcome(result["delivered"], result["correct"]) for result in results]


def transitions(old, new):
    cells = Counter(zip(old, new, strict=True))
    return {"correct_gained": cells["wrong", "correct"] + cells["abstained", "correct"],
            "correct_lost": cells["correct", "wrong"] + cells["correct", "abstained"],
            "wrong_avoided": cells["wrong", "correct"] + cells["wrong", "abstained"],
            "wrong_introduced": cells["correct", "wrong"] + cells["abstained", "wrong"],
            "wrong_to_correct": cells["wrong", "correct"], "correct_to_wrong": cells["correct", "wrong"]}


def summarize_reader(report, raw, protocol, frozen):
    n = len(report["runs"])
    arms = {}
    extraction = {}
    for arm in ARMS:
        quoted = sum(protocol["quotes"][op] * count for op, count in report["holdout"][arm]["operations"].items())
        arms[arm] = {**report["holdout"][arm], "total_quoted_units": quoted}
    for route, outcomes in raw.items():
        counts = Counter(outcomes)
        answered = n - counts["abstained"]
        extraction[route] = {"correct": counts["correct"], "wrong": counts["wrong"], "abstained": counts["abstained"],
                             "accuracy_per_task": counts["correct"] / n, "coverage_rate": answered / n,
                             "accuracy_per_answer": counts["correct"] / answered if answered else None}
    fixed = report["fixed_comparator"]
    return {"freeze_sha256": report["freeze_sha256"], "calibration_sha256": frozen["calibration_sha256"],
            "holdout_sha256": report["holdout_sha256"], "calibration_selected_fixed": fixed,
            "fixed_met_calibration_gates": report["calibration_fixed"][fixed]["qualifies"],
            "adaptive_vs_selected_fixed": report["verdict"], "raw_extraction": extraction,
            "holdout_arms": arms,
            "raw_oracle_correct": sum(any(raw[route][i] == "correct" for route in ROUTES) for i in range(n)),
            "admissible_oracle_on_time_correct":
                sum(any(row["arms"][route]["on_time_correct"] for route in ROUTES) for row in report["runs"])}


def compare(old_calibration, new_calibration, old_freeze, new_freeze, old_holdout, new_holdout, protocol):
    old, new = paired_splits(old_calibration, new_calibration, old_holdout, new_holdout, protocol)
    before = score(old, protocol, old_freeze)
    after = score(new, protocol, new_freeze)
    raw_old = {route: raw_outcomes(old["holdout"], route) for route in ROUTES}
    raw_new = {route: raw_outcomes(new["holdout"], route) for route in ROUTES}
    changes = {arm: transitions(dispatch_outcomes(before, arm), dispatch_outcomes(after, arm)) for arm in ARMS}
    changes["calibration_selected_fixed"] = transitions(dispatch_outcomes(before, before["fixed_comparator"]),
                                                        dispatch_outcomes(after, after["fixed_comparator"]))
    return {"version": "paired-receipt-readers-v1", "protocol_sha256": digest(protocol), "counts": before["counts"],
            "old": summarize_reader(before, raw_old, protocol, old_freeze),
            "new": summarize_reader(after, raw_new, protocol, new_freeze),
            "paired_raw_extraction": {name: transitions(raw_old[name], raw_new[name]) for name in ROUTES},
            "paired_runtime_delivery": changes}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("protocol", "old-calibration", "new-calibration", "old-freeze", "new-freeze",
                 "old-holdout", "new-holdout", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    paths = vars(parser.parse_args(argv))
    output = paths.pop("output")
    report = compare(**{name: json.loads(path.read_text(encoding="utf-8")) for name, path in paths.items()})
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"counts": report["counts"], "old_ceiling": report["old"]["admissible_oracle_on_time_correct"],
                      "new_ceiling": report["new"]["admissible_oracle_on_time_correct"]}))


if __name__ == "__main__":
    main()
