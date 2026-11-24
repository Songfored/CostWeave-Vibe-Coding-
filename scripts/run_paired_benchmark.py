"""Paired 120-case offline benchmark comparing CostWeave Next against baselines.

Usage from repo root:
    python -m scripts.run_paired_benchmark
    python -m scripts.run_paired_benchmark --seed 20261124 --bootstrap-samples 1000
    python -m scripts.run_paired_benchmark --traces path/to/traces.jsonl
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import random
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

_MODELS_BY_ID = {m.id: m for m in BENCHMARK_MODELS}
_MODELS_BY_EXECUTOR = {m.executor: m for m in BENCHMARK_MODELS}


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
    latencies_log: dict[str, int],
    truth_log: dict[str, bool],
    external_outcomes: Mapping[str, Any] | None = None,
) -> Registry:
    reg = default_registry()
    retry_counts: dict[str, int] = defaultdict(int)

    def make_exec(model: ModelSpec):
        def _exec(op: Operation, inputs: Mapping[str, Any], *, attempt_id: str) -> ExecutionResult:
            stage = _stage_kind(op.id)
            if external_outcomes is not None and op.id in external_outcomes:
                rec = external_outcomes[op.id]
                lat = int(rec.get("latency_ms", model.latency_p50_ms))
                latencies_log[op.id] = lat
                truth_log[op.id] = bool(rec.get("ground_truth_ok", True))
                units = int(rec.get("actual_units", min(op.cost_units, int(op.effective_expected_units))))
                if rec.get("fail_known"):
                    raise KnownFailure("external_trace_fail", actual_units=units)
                return ExecutionResult(rec["value"], actual_units=units)

            attempt_idx = retry_counts[op.id]
            retry_counts[op.id] += 1
            r_pass = _det_rand(seed, "eval", case_idx, family, model.id, stage, attempt_idx)
            r_tok = _det_rand(seed, "eval_tok", case_idx, family, model.id, stage, attempt_idx)
            passed = r_pass < _true_pass_prob(family, model.id, stage)
            in_tok = max(64, estimate_tokens(inputs))
            out_tok = _actual_tokens(family, model.id, stage, r_tok)
            actual_u = int(math.ceil(in_tok * model.input_price_per_mtok_usd + out_tok * model.output_price_per_mtok_usd))
            actual_u = max(0, min(op.cost_units, actual_u))
            scale = 0.45 if stage.startswith(("map_", "reduce_", "synth_", "gvr_")) else 1.0
            prefill_ms = int(in_tok * 0.04) if model.id == "frontier" and in_tok > 4000 else 0
            lat = int(model.latency_p50_ms * scale) + prefill_ms
            latencies_log[op.id] = latencies_log.get(op.id, 0) + max(20, lat)

            if family == "coding_fanout":
                truth_log[op.id] = passed
                if op.produces.startswith("sub_"):
                    val = {"fn": f"{op.produces}_{case_idx}", "valid": True} if passed else {"bad": True}
                else:
                    val = {"ok": passed, "modules": ["part_a", "part_b"]} if passed else {"bad": True}
                return ExecutionResult(val, actual_units=actual_u)

            if family == "long_doc_mapreduce":
                truth_log[op.id] = passed
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
            truth_log[op.id] = passed
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

    for m in BENCHMARK_MODELS:
        reg.register_executor(m.executor, make_exec(m))
    return reg


def _policy_job(
    policy: str,
    task: TaskSpec,
    models: Sequence[ModelSpec],
    *,
    beliefs: BeliefEngine,
    crc_res: Any,
) -> Job:
    if policy == "costweave_next":
        return compile_task_hypergraph(task, models, beliefs=beliefs, crc=crc_res)

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
    by_id = {op.id: op for op in direct_ops}

    if policy == "always_frontier":
        chosen = [by_id["direct_frontier"]]
    elif policy == "single_model_router":
        chosen = [min(direct_ops, key=lambda o: (o.cost_units, o.id))]
    elif policy == "naive_cascade":
        cheapest = min(direct_ops, key=lambda o: (o.cost_units, o.id))
        chosen = [cheapest] if cheapest.id == "direct_frontier" else [cheapest, by_id["direct_frontier"]]
    else:
        raise ValueError(f"unknown benchmark policy: {policy}")

    return Job(
        goals=full_compiled.goals,
        inputs=full_compiled.inputs,
        budget_units=full_compiled.budget_units,
        operations=[Operation.from_dict({**op.to_dict(), "false_pass_risk": 0.0}) for op in chosen],
        acceptance={task.goal_name: full_compiled.acceptance[task.goal_name]},
        planning_policy="lowest_cost",
        max_parallel=1,
    )


def _critical_path_latency_ms(events: Sequence[Mapping[str, Any]], latencies: Mapping[str, int], max_parallel: int, operations: Sequence[Operation] = ()) -> int:
    claimed_ops = [e["op_id"] for e in events if e.get("type") == "attempt_claimed"]
    if not claimed_ops:
        return 0
    if max_parallel <= 1:
        return sum(latencies.get(op_id, 0) for op_id in claimed_ops)
    # Causal critical-path execution clock respecting artifact dependency readiness & failure waves.
    op_map = {op.id: op for op in operations}
    art_ready_t: dict[str, int] = {}
    end_times: list[int] = []
    retry_floor_t = 0
    for ev in events:
        etype = ev.get("type")
        if etype != "attempt_settled":
            continue
        op_id = str(ev["op_id"])
        lat = latencies.get(op_id, 0)
        op = op_map.get(op_id)
        dep_t = max((art_ready_t.get(req, 0) for req in op.requires), default=0) if op is not None else retry_floor_t
        start_t = max(dep_t, retry_floor_t if (op is not None and op.produces in {op2.produces for op2 in op_map.values() if op2.id != op_id}) else dep_t)
        finish_t = start_t + lat
        end_times.append(finish_t)
        if ev.get("accepted") and op is not None:
            prev = art_ready_t.get(op.produces)
            art_ready_t[op.produces] = finish_t if prev is None else min(prev, finish_t)
        elif not ev.get("accepted"):
            retry_floor_t = max(retry_floor_t, finish_t)
    return max(end_times) if end_times else 0


def _run_one_case(
    *,
    case_idx: int,
    case_id: str,
    family: str,
    task: TaskSpec,
    models: Sequence[ModelSpec],
    policy: str,
    seed: int,
    beliefs: BeliefEngine,
    crc_res: Any,
    external_outcomes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    latencies: dict[str, int] = {}
    truth: dict[str, bool] = {}
    reg = _make_paired_registry(
        seed=seed,
        case_idx=case_idx,
        family=family,
        latencies_log=latencies,
        truth_log=truth,
        external_outcomes=external_outcomes,
    )
    job = _policy_job(policy, task, models, beliefs=beliefs, crc_res=crc_res)
    store = KernelStore(":memory:")
    try:
        state = Runtime(store, reg).submit(f"bench-{case_id}-{policy}", job)
        events = store.events(f"bench-{case_id}-{policy}")
    finally:
        store.close()

    completed = state["status"] == "complete"
    budget_overrun = (state["spent_units"] + state["held_units"]) > job.budget_units
    final_art = state["artifacts"].get(task.goal_name)
    if family == "compliance_open" and completed:
        expected_verdict = "COMPLIANT" if (case_idx % 2 == 0) else "FLAGGED"
        ground_truth_ok = isinstance(final_art, Mapping) and final_art.get("verdict") == expected_verdict
        if external_outcomes is not None and isinstance(final_art, Mapping):
            # External traces can tag explicit ground truth directly on final outcome
            ground_truth_ok = all(truth.get(ev["op_id"], True) for ev in events if ev.get("type") == "attempt_settled" and ev.get("accepted") and ev["op_id"].startswith(("direct_", "reduce_", "synth_", "cons_left_")))
        silent_corruption = not ground_truth_ok
    elif completed:
        ground_truth_ok = True
        silent_corruption = False
    else:
        ground_truth_ok = False
        silent_corruption = False

    return {
        "case_id": case_id,
        "family": family,
        "policy": policy,
        "completed": completed,
        "ground_truth_ok": ground_truth_ok,
        "silent_corruption": silent_corruption,
        "cost_units": int(state["spent_units"]),
        "budget_overrun": budget_overrun,
        "latency_ms": _critical_path_latency_ms(events, latencies, job.max_parallel, job.operations),
    }


def _load_external_traces(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


def run_benchmark(
    *,
    seed: int = 20261124,
    bootstrap_samples: int = 1000,
    traces_path: str | Path | None = None,
) -> dict[str, Any]:
    beliefs = _build_warm_belief_engine(seed)
    crc_res, crc_samples = _build_crc_calibration(seed)

    if traces_path is not None:
        raw_cases = _load_external_traces(Path(traces_path))
        case_specs = [
            (
                i,
                str(row.get("case_id", f"ext_{i:03d}")),
                str(row.get("family", "custom")),
                TaskSpec.from_dict(row["task"]),
                tuple(ModelSpec.from_dict(m) for m in row.get("models", ())) or BENCHMARK_MODELS,
                row.get("outcomes"),
            )
            for i, row in enumerate(raw_cases)
        ]
    else:
        case_specs = []
        idx = 0
        for family in ("coding_fanout", "long_doc_mapreduce", "compliance_open"):
            for local_i in range(40):
                task, _ = _case_family_prompt(family, local_i)
                case_specs.append((idx, f"{family}_{local_i:02d}", family, task, BENCHMARK_MODELS, None))
                idx += 1

    per_policy: dict[str, list[dict[str, Any]]] = {p: [] for p in POLICIES}
    for c_idx, cid, fam, task, mods, ext_out in case_specs:
        for pol in POLICIES:
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
                )
            )

    policy_summary: dict[str, Any] = {}
    by_family: dict[str, dict[str, Any]] = defaultdict(dict)
    for pol in POLICIES:
        rows = per_policy[pol]
        n = len(rows)
        policy_summary[pol] = {
            "cases": n,
            "verified_completion_rate": round(sum(1 for r in rows if r["completed"]) / max(1, n), 4),
            "ground_truth_pass_rate": round(sum(1 for r in rows if r["ground_truth_ok"]) / max(1, n), 4),
            "silent_corruption_rate": round(sum(1 for r in rows if r["silent_corruption"]) / max(1, n), 4),
            "mean_cost_units": round(sum(r["cost_units"] for r in rows) / max(1, n), 2),
            "total_cost_units": sum(r["cost_units"] for r in rows),
            "p95_latency_ms": sorted(r["latency_ms"] for r in rows)[max(0, math.ceil(0.95 * n) - 1)] if n else 0,
            "budget_overruns": sum(1 for r in rows if r["budget_overrun"]),
        }
        fams = sorted({r["family"] for r in rows})
        for fam in fams:
            sub = [r for r in rows if r["family"] == fam]
            m = len(sub)
            by_family[fam][pol] = {
                "cases": m,
                "completion_rate": round(sum(1 for r in sub if r["completed"]) / max(1, m), 4),
                "ground_truth_pass_rate": round(sum(1 for r in sub if r["ground_truth_ok"]) / max(1, m), 4),
                "silent_corruption_rate": round(sum(1 for r in sub if r["silent_corruption"]) / max(1, m), 4),
                "mean_cost_units": round(sum(r["cost_units"] for r in sub) / max(1, m), 2),
                "p95_latency_ms": sorted(r["latency_ms"] for r in sub)[max(0, math.ceil(0.95 * m) - 1)] if m else 0,
            }

    cw_rows = per_policy["costweave_next"]
    fr_rows = per_policy["always_frontier"]
    cost_red_ci = paired_bootstrap_cost_reduction_pct(
        [float(r["cost_units"]) for r in cw_rows],
        [float(r["cost_units"]) for r in fr_rows],
        samples=bootstrap_samples,
        seed=seed,
    )
    pass_diff_ci = paired_bootstrap_difference(
        [1.0 if r["ground_truth_ok"] else 0.0 for r in cw_rows],
        [1.0 if r["ground_truth_ok"] else 0.0 for r in fr_rows],
        samples=bootstrap_samples,
        seed=seed,
    )
    p95_lat_ci = paired_bootstrap_p95_latency_regression_pct(
        [float(r["latency_ms"]) for r in cw_rows],
        [float(r["latency_ms"]) for r in fr_rows],
        samples=bootstrap_samples,
        seed=seed,
    )

    return {
        "seed": seed,
        "cases": len(case_specs),
        "crc_calibration": {
            "sample_count": len(crc_samples),
            "admissible": crc_res.admissible,
            "threshold": crc_res.threshold,
            "risk_ucb": round(crc_res.risk_ucb, 6),
        },
        "policies": policy_summary,
        "by_family": dict(by_family),
        "paired_vs_always_frontier": {
            "cost_reduction_pct": cost_red_ci.to_dict() if cost_red_ci else None,
            "ground_truth_pass_diff": pass_diff_ci.to_dict(),
            "p95_latency_change_pct": p95_lat_ci.to_dict() if p95_lat_ci else None,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run paired CostWeave Next benchmark suite")
    parser.add_argument("--seed", type=int, default=20261124)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    parser.add_argument("--traces", help="Optional JSONL trace file for external replay")
    parser.add_argument("--json-out", help="Optional output file path")
    args = parser.parse_args(argv)
    report = run_benchmark(
        seed=args.seed,
        bootstrap_samples=args.bootstrap_samples,
        traces_path=args.traces,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
