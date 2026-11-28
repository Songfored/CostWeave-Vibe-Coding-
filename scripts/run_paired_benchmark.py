"""Paired 120-case offline benchmark comparing CostWeave Next against baselines.

Usage from repo root:
    python -m scripts.run_paired_benchmark
    python -m scripts.run_paired_benchmark --seed 20261124 --bootstrap-samples 1000
    python -m scripts.run_paired_benchmark --traces path/to/traces.jsonl
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from costweave.benchmarks.statistics import (
    paired_bootstrap_cost_reduction_pct,
    paired_bootstrap_difference,
    paired_bootstrap_p95_latency_regression_pct,
)
from costweave.kernel.beliefs import BeliefEngine
from costweave.kernel.compiler import ModelSpec, SubtaskSpec, TaskSpec, compile_task_hypergraph, estimate_tokens
from costweave.kernel.contracts import Job, Operation
from costweave.kernel.ports import ExecutionResult, KnownFailure, Registry, default_registry
from costweave.kernel.runtime import Runtime
from costweave.kernel.store import KernelStore
from costweave.kernel.verifiers import CRCCalibrator, CalibrationSample


POLICIES = (
    "always_frontier",
    "single_model_router",
    "naive_cascade",
    "v2_deterministic",
    "costweave_next",
)
ABLATION_POLICIES = ("costweave_next_serial", "costweave_next_no_decomposition_serial", "matched_lowest_cost_serial")

BENCHMARK_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="cheap-text",
        executor="exec_cheap_text",
        provider_family="google",
        input_price_per_mtok_usd=0.15,
        output_price_per_mtok_usd=0.50,
        context_window_tokens=16_000,
        quote_output_tokens=1024,
        latency_p50_ms=260,
        latency_p95_ms=500,
        base_pass_prob=0.80,
    ),
    ModelSpec(
        id="cheap-code",
        executor="exec_cheap_code",
        provider_family="deepseek",
        input_price_per_mtok_usd=0.20,
        output_price_per_mtok_usd=0.60,
        context_window_tokens=16_000,
        quote_output_tokens=1024,
        latency_p50_ms=300,
        latency_p95_ms=560,
        base_pass_prob=0.80,
    ),
    ModelSpec(
        id="cheap-b",
        executor="exec_cheap_b",
        provider_family="mistral",
        input_price_per_mtok_usd=0.25,
        output_price_per_mtok_usd=0.75,
        context_window_tokens=16_000,
        quote_output_tokens=1024,
        latency_p50_ms=320,
        latency_p95_ms=620,
        base_pass_prob=0.80,
    ),
    ModelSpec(
        id="frontier",
        executor="exec_frontier",
        provider_family="openai",
        input_price_per_mtok_usd=3.00,
        output_price_per_mtok_usd=15.00,
        context_window_tokens=200_000,
        quote_output_tokens=1024,
        latency_p50_ms=1450,
        latency_p95_ms=2700,
        base_pass_prob=0.975,
    ),
)

@dataclass
class AttemptObservation:
    units: int | None
    latency_ms: int | None
    correct: bool | None = None
    missing_outcome: bool = False


def _nonnegative_int(raw: Any) -> bool:
    return type(raw) is int and raw >= 0


def _synthetic_truth(task: TaskSpec, family: str, case_idx: int, value: Any) -> bool:
    """Score the final payload against the fixture source, independently of execution."""
    if not isinstance(value, Mapping):
        return False
    if family == "coding_fanout":
        # The fixture delivers a module manifest, not executable generated code.
        modules = value.get("modules")
        return (value.get("ok") is True and isinstance(modules, list)
                and all(isinstance(name, str) for name in modules)
                and sorted(modules) == sorted(sub.name for sub in task.subtasks))
    if family == "compliance_open":
        return value.get("verdict") == ("COMPLIANT" if case_idx % 2 == 0 else "FLAGGED")
    if family != "long_doc_mapreduce":
        raise ValueError(f"unknown synthetic task family: {family}")
    expected = {}
    for section in task.inputs["sections"]:
        match = re.match(r"Clause \d+-(\d+): allocation cap (\d+) usd\b", section)
        if not match:
            raise ValueError("unrecognized clause in the synthetic source")
        key, amount = map(int, match.groups())
        if key in expected:
            raise ValueError("duplicate source clause")
        expected[key] = amount
    rows = value.get("clauses")
    if not isinstance(rows, list) or len(rows) != len(expected):
        return False
    observed = {}
    for row in rows:
        if not isinstance(row, Mapping) or not _nonnegative_int(row.get("id")) or not _nonnegative_int(row.get("amount")):
            return False
        observed[row["id"]] = row["amount"]
    return observed == expected and _nonnegative_int(value.get("total")) and value["total"] == sum(expected.values())


def _det_rand(seed: int, *parts: object) -> float:
    raw = "|".join(str(p) for p in (seed, *parts)).encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64)


def _true_pass_prob(family: str, model_id: str, stage: str) -> float:
    if model_id == "frontier":
        return 0.975 if family != "coding_fanout" else 0.965
    if family == "coding_fanout":
        if model_id == "cheap-code":
            if stage.startswith(("sub_", "synth_")):
                return 0.96
            return 0.20
        if model_id == "cheap-text":
            return 0.55 if stage.startswith("sub_") else 0.15
        return 0.60 if stage.startswith("sub_") else 0.18
    if family == "long_doc_mapreduce":
        if model_id == "cheap-text":
            return 0.96 if stage.startswith("map_") else 0.35
        if model_id == "cheap-code":
            return 0.80 if stage.startswith("map_") else 0.32
        return 0.78 if stage.startswith("map_") else 0.34
    # compliance_open
    if model_id == "cheap-text":
        return 0.85
    if model_id == "cheap-code":
        return 0.84
    return 0.85


def _actual_tokens(family: str, model_id: str, stage: str, r: float) -> int:
    if stage.startswith(("map_", "sub_")):
        base = 110 if model_id != "frontier" else 180
    elif stage.startswith(("gvr_draft_", "gvr_repair_")):
        base = 160
    elif stage.startswith("reduce_"):
        base = 190
    else:
        base = 240 if model_id != "frontier" else 340
    return max(32, int(base * (0.85 + 0.30 * r)))


def _stage_kind(op_id: str) -> str:
    for prefix in ("sub_", "map_", "gvr_draft_", "gvr_repair_", "reduce_", "synth_", "cons_left_", "cons_right_", "direct_"):
        if op_id.startswith(prefix):
            return prefix + op_id[len(prefix):].rsplit("_", 1)[0] if prefix in ("sub_", "map_") else prefix
    return op_id


def _case_family_prompt(family: str, idx: int) -> tuple[TaskSpec, dict[str, Any]]:
    if family == "coding_fanout":
        task = TaskSpec(
            goal_name="bundle",
            prompt=(
                f"Case {idx}: Design and verify complete multi-module billing suite in one strict schema.\n"
                + ("module specification requirement " * 120)
            ),
            inputs={"spec": f"module_spec_{idx}: exact schema and invariant checks " + ("detail " * 250)},
            budget_units=60_000,
            acceptance_validator="json_object",
            acceptance_config={"required_keys": ["ok", "modules"]},
            max_parallel=4,
            latency_weight_units_per_sec=900.0,
            subtasks=(
                SubtaskSpec(
                    name="part_a",
                    prompt="def mod_a(): class HelperA: return {} exact single python module",
                    validator="json_schema_strict",
                    validator_config={"type": "object", "required": ["fn", "valid"]},
                    expected_output_tokens=110,
                ),
                SubtaskSpec(
                    name="part_b",
                    prompt="def mod_b(): class HelperB: return {} exact single python module",
                    validator="json_schema_strict",
                    validator_config={"type": "object", "required": ["fn", "valid"]},
                    expected_output_tokens=110,
                ),
            ),
        )
        return task, {"family": family, "idx": idx}

    if family == "long_doc_mapreduce":
        chunks = [
            f"Clause {idx}-{k}: allocation cap {100 + k * 50} usd section extract quote grounded evidence. "
            + ("word " * 3400)
            for k in range(4)
        ]
        task = TaskSpec(
            goal_name="summary",
            prompt=f"Case {idx}: extract clause totals sum ratio usd quote and aggregate validated JSON summary.",
            inputs={"sections": chunks},
            chunks_input_key="sections",
            reduce_prompt=f"Case {idx}: reduce mapped clause counts into total sum ratio usd JSON.",
            budget_units=120_000,
            acceptance_validator="invariants",
            acceptance_config={
                "non_empty_fields": ["clauses", "total"],
                "sum_equals": {"items_field": "clauses", "item_key": "amount", "total_field": "total"},
            },
            max_parallel=4,
            latency_weight_units_per_sec=300.0,
        )
        return task, {"family": family, "idx": idx}

    task = TaskSpec(
        goal_name="verdict",
        prompt=f"Case {idx}: audit vendor compliance policy risk assess judgment strictly.",
        inputs={"case_file": f"vendor_docket_{idx}: SLA clauses and indemnity liability audit"},
        budget_units=60_000,
        acceptance_validator="present",
        max_risk_alpha=0.08,
        failure_penalty_units=120_000,
        corruption_penalty_units=100_000,
        max_parallel=4,
        latency_weight_units_per_sec=400.0,
        consensus_mode="exact_canonical",
        consensus_threshold=1.0,
    )
    return task, {"family": family, "idx": idx}


def _build_warm_belief_engine(seed: int) -> BeliefEngine:
    engine = BeliefEngine()
    for i in range(360):
        fam = ("coding_fanout", "long_doc_mapreduce", "compliance_open")[i % 3]
        task, _ = _case_family_prompt(fam, 10_000 + i)
        for m in BENCHMARK_MODELS:
            # 1) Observe monolithic baseline outcomes on direct prompts
            p_dir = _true_pass_prob(fam, m.id, "direct_")
            passed_d = _det_rand(seed, "train_d", i, m.id) < p_dir
            toks_d = _actual_tokens(fam, m.id, "direct_", _det_rand(seed, "tok_d", i, m.id))
            engine.observe(m.id, task.prompt, task.inputs, passed=passed_d, completion_tokens=toks_d, prior_prob=m.base_pass_prob)
            # 2) Observe decomposed stage outcomes when applicable
            if fam == "coding_fanout" and task.subtasks:
                sub_p = _true_pass_prob(fam, m.id, "sub_part_a")
                passed_s = _det_rand(seed, "train_s", i, m.id) < sub_p
                toks_s = _actual_tokens(fam, m.id, "sub_part_a", _det_rand(seed, "tok_s", i, m.id))
                engine.observe(m.id, task.subtasks[0].prompt, task.inputs, passed=passed_s, completion_tokens=toks_s, prior_prob=m.base_pass_prob)
            elif fam == "long_doc_mapreduce":
                map_p = _true_pass_prob(fam, m.id, "map_0")
                passed_m = _det_rand(seed, "train_m", i, m.id) < map_p
                toks_m = _actual_tokens(fam, m.id, "map_0", _det_rand(seed, "tok_m", i, m.id))
                engine.observe(m.id, f"{task.prompt}\n[Map phase: extract verifiable facts]", {"chunk_0": task.inputs["sections"][0]}, passed=passed_m, completion_tokens=toks_m, prior_prob=m.base_pass_prob)
    return engine


def _build_crc_calibration(seed: int) -> tuple[Any, list[CalibrationSample]]:
    samples: list[CalibrationSample] = []
    # Isolated 140-sample held-out open-ended calibration pool for family=compliance_open.
    # Orthogonal cheap families correlate very rarely when agreeing (~0.5% joint hallucination).
    for i in range(140):
        ok_a = _det_rand(seed, "crc_a", i) < _true_pass_prob("compliance_open", "cheap-text", "cons")
        ok_b = _det_rand(seed, "crc_b", i) < _true_pass_prob("compliance_open", "cheap-b", "cons")
        if ok_a and ok_b:
            samples.append(CalibrationSample(1.0, 0.0))
        elif not ok_a and not ok_b:
            # Independent wrong answers rarely match identically (~10% collision rate on binary/multi-label judgment).
            agreed_wrong = _det_rand(seed, "crc_collide", i) < 0.08
            samples.append(CalibrationSample(1.0 if agreed_wrong else 0.0, 1.0 if agreed_wrong else 0.0))
        else:
            samples.append(CalibrationSample(0.0, 0.0))
    cal = CRCCalibrator().calibrate(samples, alpha=0.05, delta=0.05)
    return cal, samples


def _make_paired_registry(
    *,
    seed: int,
    case_idx: int,
    family: str,
    measurements: dict[str, AttemptObservation],
    models: Sequence[ModelSpec],
    external_outcomes: Mapping[str, Any] | None = None,
) -> Registry:
    reg = default_registry()
    retry_counts: dict[str, int] = defaultdict(int)

    def make_exec(model: ModelSpec):
        def _exec(op: Operation, inputs: Mapping[str, Any], *, attempt_id: str) -> ExecutionResult:
            stage = _stage_kind(op.id)
            attempt_idx = retry_counts[op.id]
            retry_counts[op.id] += 1
            if external_outcomes is not None:
                entry = external_outcomes.get(op.id, [])
                sequence = entry if isinstance(entry, list) else [entry]
                if attempt_idx >= len(sequence):
                    measurements[attempt_id] = AttemptObservation(None, None, missing_outcome=True)
                    raise RuntimeError("counterfactual attempt has no recorded outcome")
                rec = sequence[attempt_idx]
                missing = "value" not in rec and not rec.get("fail_known")
                measurement = AttemptObservation(rec.get("actual_units"), rec.get("latency_ms"),
                                                 rec.get("ground_truth_ok"), missing)
                measurements[attempt_id] = measurement
                if missing:
                    raise RuntimeError("missing executor outcome")
                if rec.get("fail_known"):
                    raise KnownFailure("external_trace_fail", actual_units=measurement.units)
                return ExecutionResult(rec["value"], actual_units=measurement.units)

            r_pass = _det_rand(seed, "eval", case_idx, family, model.id, stage, attempt_idx)
            r_tok = _det_rand(seed, "eval_tok", case_idx, family, model.id, stage, attempt_idx)
            passed = r_pass < _true_pass_prob(family, model.id, stage)
            in_tok = max(64, estimate_tokens(inputs))
            out_tok = _actual_tokens(family, model.id, stage, r_tok)
            actual_u = int(math.ceil(in_tok * model.input_price_per_mtok_usd + out_tok * model.output_price_per_mtok_usd))
            scale = 0.45 if stage.startswith(("map_", "reduce_", "synth_", "gvr_")) else 1.0
            prefill_ms = int(in_tok * 0.04) if model.id == "frontier" and in_tok > 4000 else 0
            lat = int(model.latency_p50_ms * scale) + prefill_ms
            measurements[attempt_id] = AttemptObservation(actual_u, max(20, lat))

            if family == "coding_fanout":
                if op.produces.startswith("sub_"):
                    val = {"fn": f"{op.produces}_{case_idx}", "valid": True} if passed else {"bad": True}
                else:
                    val = {"ok": passed, "modules": ["part_a", "part_b"]} if passed else {"bad": True}
                return ExecutionResult(val, actual_units=actual_u)

            if family == "long_doc_mapreduce":
                if op.produces.startswith("mapped_"):
                    if not passed:
                        raise KnownFailure("chunk_extraction_failed", actual_units=actual_u)
                    val = {"chunk_ok": True, "amount": 100}
                else:
                    if passed:
                        val = {"clauses": [{"id": k, "amount": 100} for k in range(4)], "total": 400}
                    else:
                        val = {"clauses": [{"id": 0, "amount": 100}], "total": 999}
                return ExecutionResult(val, actual_units=actual_u)

            # compliance_open (no V0): wrong predictions produce structurally valid JSON with wrong verdict!
            if passed:
                verdict = "COMPLIANT" if (case_idx % 2 == 0) else "FLAGGED"
            else:
                if model.id == "frontier":
                    verdict = "FLAGGED" if (case_idx % 2 == 0) else "COMPLIANT"
                else:
                    # Independent families disagree most of the time on wrong predictions!
                    err_bucket = 0 if _det_rand(seed, "err_dir", case_idx, model.provider_family) < 0.25 else (1 if model.provider_family == "google" else 2)
                    verdict = f"ALT_{err_bucket}"
            return ExecutionResult({"verdict": verdict, "case": case_idx}, actual_units=actual_u)

        return _exec

    for m in models:
        reg.register_executor(m.executor, make_exec(m))
    return reg


def _policy_job(
    policy: str,
    task: TaskSpec,
    models: Sequence[ModelSpec],
    *,
    beliefs: BeliefEngine | None,
    crc_res: Any,
) -> Job:
    if policy == "costweave_next" or policy in ABLATION_POLICIES:
        if policy == "costweave_next_no_decomposition_serial":
            # Disable only map/reduce and subtask fan-out. Consensus and repair
            # are independently available in both arms.
            task = replace(task, subtasks=(), chunks_input_key=None)
        compiled = compile_task_hypergraph(task, models, beliefs=beliefs, crc=crc_res)
        if policy == "costweave_next":
            return compiled
        # Change one capability at a time *after* compiling the very same graph,
        # so learned probabilities, prices and input payloads stay matched.
        payload = {**compiled.to_dict(), "max_parallel": 1}
        if policy == "matched_lowest_cost_serial":
            payload["planning_policy"] = "lowest_cost"
        return Job.from_dict(payload)

    if policy == "v2_deterministic":
        compiled = compile_task_hypergraph(task, models, beliefs=None, crc=None)
        return Job(
            goals=compiled.goals,
            inputs=compiled.inputs,
            budget_units=compiled.budget_units,
            operations=compiled.operations,
            search_limit=compiled.search_limit,
            acceptance=compiled.acceptance,
            planning_policy="lowest_cost",
            max_risk_alpha=task.max_risk_alpha,
            max_parallel=1,
        )

    # Monolithic baseline builders reuse compile_task_hypergraph direct ops cleanly
    full_compiled = compile_task_hypergraph(task, models, beliefs=None, crc=None)
    direct_ops = [op for op in full_compiled.operations if op.id.startswith("direct_")]
    if policy not in {"always_frontier", "single_model_router", "naive_cascade"}:
        raise ValueError(f"unknown benchmark policy: {policy}")
    chosen = []
    if direct_ops:
        frontier = next((op for op in direct_ops if op.id == "direct_frontier"),
                        max(direct_ops, key=lambda op: (op.cost_units, op.id)))
        cheapest = min(direct_ops, key=lambda o: (o.cost_units, o.id))
        chosen = [frontier] if policy == "always_frontier" else [cheapest]
        if policy == "naive_cascade" and cheapest.id != frontier.id:
            chosen.append(frontier)

    return Job(
        goals=full_compiled.goals,
        inputs=full_compiled.inputs,
        budget_units=full_compiled.budget_units,
        operations=[Operation.from_dict({**op.to_dict(), "false_pass_risk": 0.0}) for op in chosen],
        acceptance={task.goal_name: full_compiled.acceptance[task.goal_name]},
        planning_policy="lowest_cost",
        max_parallel=1,
    )


def _projected_latency(events: Sequence[Mapping[str, Any]], measurements: Mapping[str, AttemptObservation]) -> int | None:
    """Replay dispatch/settle event order with per-attempt recorded durations (no queueing)."""
    end_times = {}
    completed_clock = 0
    for event in events:
        if event["type"] == "attempt_claimed":
            aid = event["attempt_id"]
            # Local deterministic transforms have no modeled duration.
            obs = measurements.get(aid)
            if obs and obs.latency_ms is None:
                return None
            end_times[aid] = completed_clock + (obs.latency_ms if obs else 0)
        elif event["type"] == "attempt_settled":
            completed_clock = max(completed_clock, end_times[event["attempt_id"]])
    return max(end_times.values(), default=0)


def _run_one_case(
    *,
    case_idx: int,
    case_id: str,
    family: str,
    task: TaskSpec,
    models: Sequence[ModelSpec],
    policy: str,
    seed: int,
    beliefs: BeliefEngine | None,
    crc_res: Any,
    external_outcomes: Mapping[str, Any] | None = None,
    reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    measurements: dict[str, AttemptObservation] = {}
    reg = _make_paired_registry(
        seed=seed,
        case_idx=case_idx,
        family=family,
        measurements=measurements,
        models=models,
        external_outcomes=external_outcomes,
    )
    job = _policy_job(policy, task, models, beliefs=beliefs, crc_res=crc_res)
    store = KernelStore(":memory:")
    try:
        state = Runtime(store, reg).submit(f"bench-{case_id}-{policy}", job)
        events = store.events(f"bench-{case_id}-{policy}")
    finally:
        store.close()

    settled = [ev for ev in events if ev["type"] == "attempt_settled"]
    known_charges = [obs.units for obs in measurements.values() if obs.units is not None]
    # Non-model transforms are local operations charged by the actual replay ledger.
    known_charges.extend(ev["actual_units"] for ev in settled if ev["attempt_id"] not in measurements)
    observed_cost = sum(known_charges)
    cost_known = all(obs.units is not None for obs in measurements.values())
    incomplete_outcome = any(obs.missing_outcome for obs in measurements.values())
    unpriced_previous = False
    for ev in events:
        if ev["type"] == "attempt_settled":
            obs = measurements.get(ev["attempt_id"])
            unpriced_previous |= obs is not None and obs.units is None
        elif ev["type"] == "attempt_claimed" and unpriced_previous:
            # The replay reserved the quote for an unreported charge, so the next
            # choice has no factual remaining-budget input.
            incomplete_outcome = True

    quotes = {op.id: op.cost_units for op in job.operations}
    quote_overruns = [ev["op_id"] for ev in events if ev["type"] == "attempt_claimed"
                      and ev["attempt_id"] in measurements
                      and measurements[ev["attempt_id"]].units is not None
                      and measurements[ev["attempt_id"]].units > quotes[ev["op_id"]]]
    # An excess invoice is retained above as observed expenditure. The kernel
    # leaves it for reconciliation; this is not an observed terminal delivery.
    unresolved = incomplete_outcome or state["status"] == "needs_reconciliation"
    completed = None if unresolved else state["status"] == "complete"
    final_art = state["artifacts"].get(task.goal_name)
    correct = completed
    if completed:
        if external_outcomes is None:
            correct = _synthetic_truth(task, family, case_idx, final_art)
        elif reference is not None:
            correct = json.dumps(final_art, sort_keys=True, allow_nan=False) == json.dumps(
                reference["value"], sort_keys=True, allow_nan=False)
        else:
            # Only a label on the accepted final producer grades this output.
            # A true label on a draft, consensus input, or failed predecessor cannot.
            winner = next((ev for ev in settled if ev["accepted"] and ev["artifact"] == task.goal_name), None)
            obs = measurements.get(winner["attempt_id"]) if winner else None
            correct = obs.correct if obs else None
    if observed_cost > job.budget_units:
        overrun = True
    elif not cost_known or incomplete_outcome:
        overrun = None
    else:
        overrun = False
    return {
        "case_id": case_id,
        "family": family,
        "policy": policy,
        "completed": completed,
        "ground_truth_ok": correct,
        "silent_corruption": None if correct is None else bool(completed and not correct),
        "cost_units": observed_cost if cost_known and not incomplete_outcome else None,
        "observed_cost_units": observed_cost,
        "quote_overruns": quote_overruns,
        "budget_overrun": overrun,
        "latency_ms": None if unresolved else _projected_latency(events, measurements),
        "missing_outcome": incomplete_outcome,
        "needs_reconciliation": state["status"] == "needs_reconciliation",
    }


def _load_external_traces(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cid = str(row.get("case_id", f"ext_{len(cases):03d}"))
        if cid in seen:
            raise ValueError(f"duplicate external case_id {cid!r}")
        seen.add(cid)
        if "reference" in row and (not isinstance(row["reference"], dict) or "value" not in row["reference"]):
            raise ValueError("reference requires an independently labeled value")
        outcomes = row.get("outcomes", {})
        if not isinstance(outcomes, dict):
            raise ValueError("outcomes must map operation IDs to receipts")
        for key, entry in outcomes.items():
            for rec in (entry if isinstance(entry, list) else [entry]):
                if not isinstance(rec, dict):
                    raise ValueError(f"invalid {key} receipt")
                for field in ("actual_units", "latency_ms"):
                    if rec.get(field) is not None and not _nonnegative_int(rec[field]):
                        raise ValueError(f"invalid {key} {field}")
                for field in ("ground_truth_ok", "fail_known"):
                    if rec.get(field) is not None and type(rec[field]) is not bool:
                        raise ValueError(f"invalid {key} {field}")
        cases.append(row)
    return cases


def _summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    coverage = {field: sum(row[field] is not None for row in rows)
                for field in ("completed", "ground_truth_ok", "silent_corruption", "cost_units",
                              "latency_ms", "budget_overrun")}

    def rate(field: str) -> float | None:
        return round(sum(row[field] is True for row in rows) / n, 4) if n and coverage[field] == n else None

    cost = sum(row["observed_cost_units"] for row in rows)
    cost_complete = n > 0 and coverage["cost_units"] == n
    correct = sum(row["ground_truth_ok"] is True for row in rows)
    latency = sorted(row["latency_ms"] for row in rows if row["latency_ms"] is not None)
    return {
        "cases": n, "observed_cases": coverage,
        "verified_completion_rate": rate("completed"), "completion_rate": rate("completed"),
        "ground_truth_pass_rate": rate("ground_truth_ok"), "silent_corruption_rate": rate("silent_corruption"),
        "total_cost_units": cost if cost_complete else None,
        "units_per_correct_delivery": round(cost / correct, 4)
        if cost_complete and coverage["ground_truth_ok"] == n and correct else None,
        "mean_cost_units": round(cost / n, 2) if cost_complete else None,
        "observed_cost_units": cost,
        "p95_latency_ms": latency[math.ceil(0.95 * n) - 1] if n and len(latency) == n else None,
        "budget_overruns": sum(row["budget_overrun"] is True for row in rows)
        if coverage["budget_overrun"] == n else None,
        "observed_budget_overruns": sum(row["budget_overrun"] is True for row in rows),
        "quote_overruns": sum(len(row["quote_overruns"]) for row in rows),
    }


def _paired_comparison(
    candidate: Sequence[Mapping[str, Any]], control: Sequence[Mapping[str, Any]],
    bootstrap_samples: int, seed: int,
) -> dict[str, Any]:
    if [r["case_id"] for r in candidate] != [r["case_id"] for r in control]:
        raise ValueError("policy arms require the same paired documents in order")
    paired = {}
    for name, field, estimator in (
        ("cost_reduction_pct", "cost_units", paired_bootstrap_cost_reduction_pct),
        ("ground_truth_pass_diff", "ground_truth_ok", paired_bootstrap_difference),
        ("p95_latency_change_pct", "latency_ms", paired_bootstrap_p95_latency_regression_pct),
    ):
        pairs = [(a[field], b[field]) for a, b in zip(candidate, control, strict=True)
                 if a[field] is not None and b[field] is not None]
        paired[name + "_observed_pairs"] = len(pairs)
        interval = None
        if pairs and len(pairs) == len(candidate):
            left, right = zip(*pairs)
            interval = estimator([float(x) for x in left], [float(x) for x in right],
                                 samples=bootstrap_samples, seed=seed)
        paired[name] = interval.to_dict() if interval else None

    return paired


def run_benchmark(
    *,
    seed: int = 20261124,
    bootstrap_samples: int = 1000,
    traces_path: str | Path | None = None,
) -> dict[str, Any]:
    if traces_path is not None:
        beliefs, crc_res, crc_samples = None, None, []
        raw_cases = _load_external_traces(Path(traces_path))
        case_specs = [
            (
                i,
                str(row.get("case_id", f"ext_{i:03d}")),
                str(row.get("family", "custom")),
                TaskSpec.from_dict(row["task"]),
                tuple(ModelSpec.from_dict(m) for m in row.get("models", ())) or BENCHMARK_MODELS,
                row.get("outcomes", {}), row.get("reference"),
            )
            for i, row in enumerate(raw_cases)
        ]
    else:
        beliefs = _build_warm_belief_engine(seed)
        crc_res, crc_samples = _build_crc_calibration(seed)
        case_specs = []
        idx = 0
        for family in ("coding_fanout", "long_doc_mapreduce", "compliance_open"):
            for local_i in range(40):
                task, _ = _case_family_prompt(family, local_i)
                case_specs.append((idx, f"{family}_{local_i:02d}", family, task, BENCHMARK_MODELS, None, None))
                idx += 1

    arms = POLICIES if traces_path is not None else (*POLICIES, *ABLATION_POLICIES)
    per_policy: dict[str, list[dict[str, Any]]] = {p: [] for p in arms}
    for c_idx, cid, fam, task, mods, ext_out, reference in case_specs:
        for pol in arms:
            per_policy[pol].append(
                _run_one_case(
                    case_idx=c_idx,
                    case_id=cid,
                    family=fam,
                    task=task,
                    models=mods,
                    policy=pol,
                    seed=seed,
                    beliefs=beliefs,
                    crc_res=crc_res,
                    external_outcomes=ext_out,
                    reference=reference,
                )
            )

    policy_summary = {policy: _summarize(rows) for policy, rows in per_policy.items()}
    families = sorted({row["family"] for row in per_policy["costweave_next"]})
    by_family = {
        fam: {policy: _summarize([r for r in rows if r["family"] == fam])
              for policy, rows in per_policy.items()}
        for fam in families
    }
    paired = _paired_comparison(per_policy["costweave_next"], per_policy["always_frontier"],
                                bootstrap_samples, seed)
    ablations = {}
    if traces_path is None:
        for dimension, active, control in (
            ("parallel_capacity", "costweave_next", "costweave_next_serial"),
            ("decomposed_routes", "costweave_next_serial", "costweave_next_no_decomposition_serial"),
            ("decision_policy", "costweave_next_serial", "matched_lowest_cost_serial"),
        ):
            ablations[dimension] = {
                "active": active, "control": control,
                "paired": _paired_comparison(per_policy[active], per_policy[control], bootstrap_samples, seed),
            }

    return {
        "seed": seed,
        "bootstrap_samples": bootstrap_samples,
        "cases": len(case_specs),
        "crc_calibration": {
            "sample_count": len(crc_samples),
            "admissible": crc_res.admissible if crc_res else False,
            "threshold": crc_res.threshold if crc_res else None,
            "risk_ucb": round(crc_res.risk_ucb, 6) if crc_res else None,
        },
        "policies": policy_summary,
        "by_family": dict(by_family),
        "provenance": "external_trace" if traces_path is not None else "synthetic_fixture",
        "latency_basis": "projected from recorded dispatch and settlement order; excludes queueing",
        "paired_vs_always_frontier": paired,
        "ablations": ablations,
        "records": per_policy,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run paired CostWeave Next benchmark suite")
    parser.add_argument("--seed", type=int, default=20261124)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    parser.add_argument("--traces", help="Optional JSONL trace file for external replay")
    parser.add_argument("--summary-only", action="store_true", help="Omit individual case records from the report")
    parser.add_argument("--json-out", help="Optional output file path")
    args = parser.parse_args(argv)
    report = run_benchmark(
        seed=args.seed,
        bootstrap_samples=args.bootstrap_samples,
        traces_path=args.traces,
    )
    if args.summary_only:
        del report["records"]
    rendered = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
