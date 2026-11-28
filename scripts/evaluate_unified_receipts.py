"""Freeze serial receipt planners before reading the final CORD shard, then replay paired actions."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import random

from costweave.kernel.contracts import Job, Operation
from costweave.kernel.ports import ExecutionResult, KnownFailure, default_registry
from costweave.kernel.runtime import Runtime
from costweave.kernel.store import KernelStore
from scripts.compare_receipt_readers import paired_splits
from scripts.evaluate_verification_value import ROUTES, digest, partition, replay, summarize, validate_cases
from scripts.measure_receipt_readers import INDEPENDENT


ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "examples" / "receipt-readers"
EXPERIMENT = ROOT / "examples" / "unified-verification"
VERSIONS = ("old", "new")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fit_routes(cases, minimum):
    strata = defaultdict(list)
    for case in cases:
        strata[case["stratum"]].append(case)
    estimates = {}
    for name, rows in [("*", cases), *strata.items()]:
        if name != "*" and len(rows) < minimum:
            continue
        estimates[name] = {}
        for route in ROUTES:
            answers = [row["actions"][route]["answer"] for row in rows]
            answered = sum(value is not None for value in answers)
            wrong = sum(value is not None and value != row["truth"] for value, row in zip(answers, rows, strict=True))
            estimates[name][route] = {"n": len(rows), "pass_prob": (answered + 1) / (len(rows) + 2),
                                     "false_pass_risk": (wrong + 1) / (len(rows) + 2)}
    return estimates


def replay_bsao(case, estimates, protocol, terms):
    prices, durations = protocol["quotes"], protocol["route_ms"]
    params = estimates.get(case["stratum"], estimates["*"])
    ops = [Operation(route, ["signal"], "result", "read", "present", prices[route],
                     pass_prob=params[route]["pass_prob"], false_pass_risk=params[route]["false_pass_risk"],
                     latency_p50_ms=durations[route], latency_p95_ms=durations[route]) for route in ROUTES]
    job = Job(["result"], {"signal": {"stratum": case["stratum"]}}, case["budget_units"], ops,
              acceptance={"result": {"validator": "present", "config": {}}}, planning_policy="bsao",
              deadline_ms=case["deadline_ms"], failure_penalty_units=terms["failure_penalty_units"],
              corruption_penalty_units=terms["corruption_penalty_units"], max_risk_alpha=terms["max_risk_alpha"])
    elapsed = 0

    def execute(op, inputs, *, attempt_id):
        nonlocal elapsed
        observed = case["actions"][op.id]
        elapsed += observed["elapsed_ms"]
        if observed["answer"] is None:
            raise KnownFailure("reader_abstained", actual_units=observed["cost_units"])
        return ExecutionResult(observed["answer"], actual_units=observed["cost_units"])

    registry = default_registry()
    registry.register_executor("read", execute)
    store = KernelStore(":memory:")
    try:
        state = Runtime(store, registry, clock_ms=lambda: 1_000_000 + elapsed).submit("document", job)
        attempts = [ev["op_id"] for ev in store.events("document") if ev["type"] == "attempt_claimed"]
    finally:
        store.close()
    answer = state["artifacts"].get("result") if state["status"] == "complete" else None
    correct = answer is not None and answer == case["truth"]
    if answer is not None and elapsed > case["deadline_ms"]:
        raise ValueError("runtime published a late final answer")
    return {"route": attempts, "status": state["status"], "reason": state["reason"], "answer": answer,
            "delivered": answer is not None, "correct": correct, "false_delivery": answer is not None and not correct,
            "on_time_correct": correct, "spent_units": state["spent_units"], "elapsed_ms": elapsed}


def baseline_inputs(version, protocol):
    data = read(HISTORY / f"{version}-calibration.json")
    baseline = read(HISTORY / f"{version}-frozen.json")
    cases = partition(data, calibration_only=True)["calibration"]
    validate_cases(cases, protocol)
    if (baseline["calibration_sha256"] != digest(cases) or baseline["protocol_sha256"] != digest(protocol)
            or baseline["freeze_sha256"] != digest({k: v for k, v in baseline.items() if k != "freeze_sha256"})):
        raise ValueError("historical calibration or freeze changed")
    return data, cases, baseline


def freeze(terms, protocol):
    result = {"protocol_sha256": digest(terms), "operating_protocol_sha256": digest(protocol), "readers": {}}
    for version in VERSIONS:
        _, cases, baseline = baseline_inputs(version, protocol)
        estimates = fit_routes(cases, terms["min_support"])
        runs = [replay_bsao(case, estimates, protocol, terms) for case in cases]
        result["readers"][version] = {"historical_freeze": baseline["freeze_sha256"], "fixed": baseline["fixed_comparator"],
                                      "estimates": estimates, "calibration_bsao": summarize(runs, protocol)}
    return {**result, "sha256": digest(result)}


def paired_interval(fixed, candidate, seed, draws):
    # Positive = BSAO saves units per timely correct delivery on the same resampled documents.
    rng = random.Random(seed)
    pairs = [(a["spent_units"], a["on_time_correct"], b["spent_units"], b["on_time_correct"])
             for a, b in zip(fixed, candidate, strict=True)]
    differences = []
    for _ in range(draws):
        sample = rng.choices(pairs, k=len(pairs))
        cost_a, n_a, cost_b, n_b = map(sum, zip(*sample))
        if n_a and n_b:
            differences.append(cost_a / n_a - cost_b / n_b)
    differences.sort()
    ci = None
    if len(differences) == draws:
        ci = [differences[int((draws - 1) * q)] for q in (0.025, 0.975)]
    return {"ci95": ci, "defined_draws": len(differences), "draws": draws}


def score(terms, protocol, frozen, directory):
    if (frozen["protocol_sha256"] != digest(terms) or frozen["operating_protocol_sha256"] != digest(protocol)
            or digest({k: v for k, v in frozen.items() if k != "sha256"}) != frozen["sha256"]):
        raise ValueError("experiment freeze changed")
    inputs = {v: baseline_inputs(v, protocol) for v in VERSIONS}
    holdout = {v: read(directory / f"{v}-independent.json") for v in VERSIONS}
    for v, data in holdout.items():
        meta = data["metadata"]
        if (meta["reader"] != v or meta["revision"] != terms["source_revision"]
                or meta["source_sha256"] != {terms["source_file"]: terms["source_sha256"]}
                or meta["source_sha256"] != dict(INDEPENDENT)):
            raise ValueError("wrong independent source or reader")
    old, new = paired_splits(inputs["old"][0], inputs["new"][0], holdout["old"], holdout["new"], protocol)
    result = {"freeze_sha256": frozen["sha256"], "readers": {}, "runs": {}}
    for v, partitions in zip(VERSIONS, (old, new), strict=True):
        cases, baseline, policy = partitions["holdout"], inputs[v][2], frozen["readers"][v]
        if baseline["freeze_sha256"] != policy["historical_freeze"]:
            raise ValueError("historical comparator changed")
        fixed = policy["fixed"]
        arms = {}
        for arm in (*ROUTES, "adaptive", "bsao"):
            if arm == "bsao":
                runs = [replay_bsao(c, policy["estimates"], protocol, terms) for c in cases]
            else:
                runs = [replay(c, arm, baseline["model"], protocol) for c in cases]
            arms[arm] = runs
        metrics = {arm: summarize(runs, protocol) for arm, runs in arms.items()}
        interval = paired_interval(arms[fixed], arms["bsao"], terms["bootstrap_seed"], terms["bootstrap_draws"])
        jointly_qualified = all([baseline["calibration_fixed"][fixed]["qualifies"], policy["calibration_bsao"]["qualifies"],
                                 metrics[fixed]["qualifies"], metrics["bsao"]["qualifies"]])
        enabled = bool(jointly_qualified and interval["ci95"] and interval["ci95"][0] > 0)
        result["readers"][v] = {"fixed": fixed, "metrics": metrics, "savings_per_timely_correct": interval,
                                "enable_by_default": enabled, "holdout_sha256": digest(cases)}
        result["runs"][v] = [{"id": c["id"], "arms": {arm: rows[i] for arm, rows in arms.items()}}
                             for i, c in enumerate(cases)]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("freeze", "score"))
    parser.add_argument("--directory", type=Path, default=EXPERIMENT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    terms, protocol = read(EXPERIMENT / "protocol.json"), read(HISTORY / "protocol.json")
    if args.stage == "freeze":
        result = freeze(terms, protocol)
    else:
        result = score(terms, protocol, read(args.directory / "frozen.json"), args.directory)
    args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                      allow_nan=False) + "\n", encoding="utf-8")
    print(args.output, digest(result))


if __name__ == "__main__":
    main()
