"""Freeze a verification policy on paired calibration records and score unseen documents."""

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from costweave.kernel.contracts import Job, Operation, canonical_json
from costweave.kernel.ports import ExecutionResult, default_registry
from costweave.kernel.runtime import Runtime
from costweave.kernel.store import KernelStore
from costweave.kernel.verification_value import fit_verification_model


ROUTES = ("direct", "check", "original")
ARMS = (*ROUTES, "adaptive")
VERSION = "paired-document-replay-v1"


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def check_integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def check_protocol(protocol):
    for key in ("min_on_time_correct_rate", "max_false_delivery_rate", "max_error_rate", "alpha"):
        value = protocol.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{key} must be a probability")
    if not 0 < protocol["alpha"] < 1:
        raise ValueError("alpha must be strictly between zero and one")
    check_integer(protocol.get("min_support"), "min_support", 1)
    if protocol.get("fallback_operation") not in ROUTES:
        raise ValueError("fallback_operation must name a declared route")
    for key in ("quotes", "route_ms"):
        if not isinstance(protocol.get(key), dict) or set(protocol[key]) != set(ROUTES):
            raise ValueError(f"{key} must cover every evaluated route")
        for value in protocol[key].values():
            check_integer(value, key)


def partition(dataset, *, calibration_only=False):
    records = dataset.get("records") if isinstance(dataset, dict) else dataset
    if not isinstance(records, list):
        raise ValueError("dataset must contain an array of records")
    splits = {"calibration": [], "holdout": []}
    ids = set()
    groups = set()
    for case in records:
        if not isinstance(case, dict) or case.get("split") not in splits:
            raise ValueError("records must declare a calibration or holdout split")
        for key in ("id", "group"):
            if not isinstance(case.get(key), str) or not case[key]:
                raise ValueError(f"missing document {key}")
        if case["id"] in ids or case["group"] in groups:
            raise ValueError("duplicate or overlapping document id/group")
        ids.add(case["id"])
        groups.add(case["group"])
        splits[case["split"]].append(case)
    for name, subset in splits.items():
        if not subset and (name == "calibration" or not calibration_only):
            raise ValueError(f"empty {name} split")
        subset.sort(key=lambda row: row["id"])
    return splits


def validate_cases(cases, protocol):
    for case in cases:
        if not isinstance(case.get("stratum"), str) or not case["stratum"]:
            raise ValueError("each document needs an observable stratum")
        if type(case.get("truth")) is not int:
            raise ValueError("an independently audited integer amount is required")
        check_integer(case.get("budget_units"), "budget_units")
        check_integer(case.get("deadline_ms"), "deadline_ms", 1)
        if not isinstance(case.get("actions"), dict) or set(case["actions"]) != set(ROUTES):
            raise ValueError("score every route on the same documents")
        for route, result in case["actions"].items():
            if not isinstance(result, dict) or "answer" not in result:
                raise ValueError("an observed route answer or explicit null is required")
            if result["answer"] is not None and type(result["answer"]) is not int:
                raise ValueError("route answer must be integer or null")
            check_integer(result.get("cost_units"), "realized cost")
            check_integer(result.get("elapsed_ms"), "realized elapsed time")
            if result["cost_units"] > protocol["quotes"][route]:
                raise ValueError(f"{route} exceeded the predeclared quote")


def fit(cases, protocol):
    observations = []
    for case in cases:
        paired = {
            name: {"correct": result["answer"] == case["truth"], "cost_units": result["cost_units"],
                   "elapsed_ms": result["elapsed_ms"]}
            for name, result in case["actions"].items()
        }
        observations.append({"case_id": case["id"], "stratum": case["stratum"], "outcomes": paired})
    return fit_verification_model(observations, baseline="direct", actions=ROUTES,
                                  min_support=protocol["min_support"], alpha=protocol["alpha"])


def replay(case, arm, model, protocol):
    # Runtime sees only the observable signal and the published route quotes. The executor
    # returns a recorded outcome *after* the immutable decision. Only the scorer receives truth.
    elapsed = 0
    recorded = case["actions"]

    def execute(op, inputs, *, attempt_id):
        nonlocal elapsed
        result = recorded[op.id]
        elapsed += result["elapsed_ms"]
        verdict = ({"outcome": "abstained"} if result["answer"] is None
                   else {"outcome": "answered", "amount": result["answer"]})
        return ExecutionResult(verdict, actual_units=result["cost_units"])

    registry = default_registry()
    registry.register_executor("document_route", execute)
    ops = [Operation(name, ["signal"], "result", "document_route", "present", protocol["quotes"][name])
           for name in ROUTES]
    allowed = ROUTES if arm == "adaptive" else (arm,)
    policy = {"artifact": "signal", "field": "stratum", "model": model,
              "fallback_operation": protocol["fallback_operation"] if arm == "adaptive" else arm,
              "min_on_time_correct": 1 - protocol["max_error_rate"] if arm == "adaptive" else 0,
              "deadline_ms": case["deadline_ms"], "route_ms": {r: protocol["route_ms"][r] for r in allowed}}
    job = Job(["result"], {"signal": {"stratum": case["stratum"]}}, case["budget_units"], ops,
              verification_policy=policy, acceptance={
                  "signal": {"validator": "json_object", "config": {"required_keys": ["stratum"]}},
                  "result": {"validator": "json_object", "config": {"required_keys": ["outcome"]}}})
    store = KernelStore(":memory:")
    try:
        runtime = Runtime(store, registry, clock_ms=lambda: 1_000_000 + elapsed)
        state = runtime.submit("document", job)
        events = store.events("document")
    finally:
        store.close()
    attempts = [e["op_id"] for e in events if e["type"] == "attempt_claimed"]
    answered = state["artifacts"].get("result", {}).get("amount") if state["status"] == "complete" else None
    correct = answered is not None and answered == case["truth"]
    return {"route": attempts, "status": state["status"], "reason": state["reason"], "answer": answered,
            "spent_units": state["spent_units"], "elapsed_ms": elapsed, "delivered": answered is not None,
            "correct": correct, "false_delivery": answered is not None and not correct,
            "on_time_correct": correct and elapsed <= case["deadline_ms"]}


def summarize(runs, protocol):
    total = len(runs)
    delivered = sum(r["delivered"] for r in runs)
    wrong = sum(r["false_delivery"] for r in runs)
    on_time = sum(r["on_time_correct"] for r in runs)
    correct = sum(r["correct"] for r in runs)
    spent = sum(r["spent_units"] for r in runs)
    return {"tasks": total, "delivered": delivered, "abstained": total - delivered,
            "correct": correct, "false_delivery": wrong, "on_time_correct": on_time,
            "coverage_rate": delivered / total, "false_per_task": wrong / total,
            "false_per_delivery": wrong / delivered if delivered else None, "on_time_correct_rate": on_time / total,
            "spent_units": spent, "spent_per_correct": spent / correct if correct else None,
            "spent_per_on_time_correct": spent / on_time if on_time else None,
            "qualifies": bool(on_time and wrong / total <= protocol["max_false_delivery_rate"]
                              and on_time / total >= protocol["min_on_time_correct_rate"]),
            "operations": dict(Counter(op for row in runs for op in row["route"]))}


def evaluate(cases, arms, model, protocol):
    return [{"id": c["id"], "group": c["group"], "stratum": c["stratum"], "arms": {
        arm: replay(c, arm, model, protocol) for arm in arms}} for c in cases]


def summaries(rows, arms, protocol):
    return {arm: summarize([row["arms"][arm] for row in rows], protocol) for arm in arms}


def select_fixed(scores):
    qualified = [route for route in ROUTES if scores[route]["qualifies"]]
    if qualified:
        return min(qualified, key=lambda r: (scores[r]["spent_per_on_time_correct"], scores[r]["false_per_task"],
                                             -scores[r]["on_time_correct_rate"], r))
    return min(ROUTES, key=lambda r: (-scores[r]["on_time_correct_rate"], scores[r]["false_per_task"],
                                      scores[r]["spent_units"], r))


def freeze(calibration, protocol):
    if not calibration or any(c["split"] != "calibration" for c in calibration):
        raise ValueError("freeze requires calibration documents only")
    check_protocol(protocol)
    validate_cases(calibration, protocol)
    model = fit(calibration, protocol)
    fixed = summaries(evaluate(calibration, ROUTES, model, protocol), ROUTES, protocol)
    payload = {"version": VERSION, "protocol_sha256": digest(protocol), "calibration_sha256": digest(calibration),
               "model": model, "calibration_fixed": fixed, "fixed_comparator": select_fixed(fixed)}
    return {**payload, "freeze_sha256": digest(payload)}


def verdict(scores, comparator, calibration_qualified):
    a = scores["adaptive"]
    b = scores[comparator]
    if not calibration_qualified:
        return "no_fixed_policy_met_calibration_gates"
    if not a["qualifies"]:
        return "adaptive_missed_holdout_gates"
    if not b["qualifies"]:
        return "fixed_comparator_missed_holdout_gates"
    if a["spent_per_on_time_correct"] < b["spent_per_on_time_correct"]:
        return "adaptive_lower_observed_cost"
    return "no_observed_adaptive_cost_advantage"


def score(splits, protocol, frozen):
    check_protocol(protocol)
    calibration, holdout = splits["calibration"], splits["holdout"]
    if (frozen.get("version") != VERSION or frozen.get("protocol_sha256") != digest(protocol)
            or frozen.get("calibration_sha256") != digest(calibration)
            or frozen.get("freeze_sha256") != digest({k: v for k, v in frozen.items() if k != "freeze_sha256"})):
        raise ValueError("frozen calibration artifact, protocol or calibration records changed")
    validate_cases(holdout, protocol)
    rows = evaluate(holdout, ARMS, frozen["model"], protocol)
    by_arm = summaries(rows, ARMS, protocol)
    comparator = frozen["fixed_comparator"]
    qualified = frozen["calibration_fixed"][comparator]["qualifies"]
    strata = {}
    for stratum in sorted({r["stratum"] for r in rows}):
        part = [r for r in rows if r["stratum"] == stratum]
        metrics = summaries(part, ARMS, protocol)
        strata[stratum] = {"metrics": metrics, "descriptive_verdict": verdict(metrics, comparator, qualified)}
    return {"version": VERSION, "freeze_sha256": frozen["freeze_sha256"], "holdout_sha256": digest(holdout),
            "counts": {"calibration": len(calibration), "holdout": len(holdout)}, "protocol": protocol,
            "fixed_comparator": comparator, "calibration_fixed": frozen["calibration_fixed"], "holdout": by_arm,
            "verdict": verdict(by_arm, comparator, qualified), "strata": strata, "runs": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--holdout", type=Path, help="separate held-out measurements, opened only for scoring")
    parser.add_argument("--freeze-only", action="store_true")
    parser.add_argument("--frozen-out", type=Path)
    parser.add_argument("--frozen-in", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.freeze_only and (not args.frozen_out or args.frozen_in or args.output):
        parser.error("--freeze-only requires --frozen-out and excludes scoring options")
    if args.frozen_in and args.frozen_out:
        parser.error("choose one frozen artifact input or output")
    if args.holdout and (args.freeze_only or not args.frozen_in):
        parser.error("--holdout requires an already frozen policy")
    data = json.loads(args.dataset.read_text(encoding="utf-8"))
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
    if args.holdout:
        other = json.loads(args.holdout.read_text(encoding="utf-8"))
        measured = other["records"] if isinstance(other, dict) else other
        calibration = data["records"] if isinstance(data, dict) else data
        if any(row["split"] != "holdout" for row in measured) or any(row["split"] != "calibration" for row in calibration):
            raise ValueError("split files must contain only their declared partition")
        data = calibration + measured
        metadata = {"calibration": metadata, "holdout": other.get("metadata", {}) if isinstance(other, dict) else {}}
    splits = partition(data, calibration_only=args.freeze_only)
    if args.frozen_in:
        frozen = json.loads(args.frozen_in.read_text(encoding="utf-8"))
    else:
        frozen = freeze(splits["calibration"], protocol)
    if args.frozen_out:
        args.frozen_out.write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.freeze_only:
        print(frozen["freeze_sha256"])
        return
    report = score(splits, protocol, frozen)
    report["dataset_metadata"] = metadata
    body = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(body, encoding="utf-8")
        print(json.dumps({"freeze_sha256": report["freeze_sha256"], "verdict": report["verdict"],
                          "holdout": report["holdout"]}, ensure_ascii=False))
    else:
        print(body, end="")


if __name__ == "__main__":
    main()
