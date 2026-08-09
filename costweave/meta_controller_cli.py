"""Offline CLI for the v0.6.1 Rational MetaController."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .intelligence.beliefs import SystemBeliefV1, TaskBeliefV1
from .intelligence.common import NonNegativeEstimate, ProbabilityInterval
from .intelligence.meta_controller import BudgetAmountV1, MetaControllerEngine, PlannerMemberV1
from .intelligence.strategy import StrategyCandidateV1, StrategyNodeV1


MAX_REQUEST_BYTES = 64 * 1024 * 1024


def _read_request(path: str) -> Mapping[str, Any]:
    candidate = Path(path)
    try:
        if not candidate.is_file() or candidate.stat().st_size > MAX_REQUEST_BYTES:
            raise ValueError("request must be a regular file no larger than 64 MiB")
        raw = candidate.read_bytes().decode("utf-8")
        value = json.loads(raw)
    except UnicodeDecodeError as exc:
        raise ValueError("request must be strict UTF-8") from exc
    except OSError as exc:
        raise ValueError("cannot read request") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("request is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("request must contain a JSON object")
    return value


def _demo() -> tuple[TaskBeliefV1, SystemBeliefV1, tuple[StrategyCandidateV1, ...]]:
    objective_hash = hashlib.sha256(b"costweave-v061-demo").hexdigest()
    task = TaskBeliefV1(
        case_id="v061-demo",
        objective_hash=objective_hash,
        task_family_probabilities={"code": 1.0},
        required_modalities=(),
        required_tools=(),
        risk_level="low",
        reversible=True,
        external_side_effects=False,
        decomposability=ProbabilityInterval(mean=0.1, lower=0.0, upper=0.2),
        parallelizability=ProbabilityInterval(mean=0.1, lower=0.0, upper=0.2),
        verification_difficulty=ProbabilityInterval(mean=0.1, lower=0.0, upper=0.2),
        epistemic_uncertainty=0.05,
        aleatoric_uncertainty=0.05,
        ood_score=0.02,
        hard_constraints={"side_effects": False},
        evidence_ids=("demo-task-evidence",),
        observed_at="2026-08-09T00:00:00Z",
    )
    system = SystemBeliefV1(
        case_id="v061-demo",
        local_queue_depth=0,
        local_available_slots=2,
        local_gpu_memory_available_mb=None,
        budget_remaining_usd=1.0,
        deadline_remaining_ms=10_000,
        provider_beliefs=(),
        telemetry_age_ms=0,
        evidence_ids=("demo-system-evidence",),
        observed_at="2026-08-09T00:00:00Z",
    )
    node = StrategyNodeV1(
        node_id="demo-node",
        objective_hash=objective_hash,
        dependencies=(),
        executor_id="local-demo",
        reasoning_tier="low",
        tool_ids=(),
        parallel_group="",
        validator_id="demo-validator",
        predicted_success=ProbabilityInterval(mean=0.95, lower=0.90, upper=0.98),
        predicted_cost_usd=NonNegativeEstimate(mean=0.001, p95=0.002),
        predicted_latency_ms=NonNegativeEstimate(mean=20, p95=40),
    )
    candidate = StrategyCandidateV1(
        case_id="v061-demo",
        management_mode="no_delegation",
        nodes=(node,),
        planning_cost_usd=NonNegativeEstimate(mean=0.0, p95=0.0),
        planning_latency_ms=NonNegativeEstimate(mean=0.0, p95=0.0),
        predicted_success=node.predicted_success,
        predicted_total_cost_usd=NonNegativeEstimate(mean=0.001, p95=0.002),
        predicted_total_latency_ms=node.predicted_latency_ms,
        hard_constraint_checks={"demo": True},
        recovery_policy="none",
        evidence_ids=("demo-strategy-evidence",),
        policy_revision="v061-demo-policy",
    )
    return task, system, (candidate,)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CostWeave v0.6.1 offline Rational MetaController")
    parser.add_argument("--request", help="JSON request containing task_belief, system_belief and candidates")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--now", default="2026-08-09T00:00:00Z")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.request:
            payload = _read_request(args.request)
            task = TaskBeliefV1.from_dict(payload["task_belief"])
            system = SystemBeliefV1.from_dict(payload["system_belief"])
            candidates = tuple(StrategyCandidateV1.from_dict(item) for item in payload["candidates"])
            members = tuple(PlannerMemberV1.from_dict(item) for item in payload.get("planner_members", []))
            budget = BudgetAmountV1.from_dict(payload["available_budget"]) if payload.get("available_budget") is not None else None
            project = str(payload.get("project_id", "local-default"))
        else:
            task, system, candidates = _demo()
            members = ()
            budget = BudgetAmountV1(money_usd=1.0)
            project = "local-default"
        decision, committee = MetaControllerEngine().decide(
            project_id=project,
            task_belief=task,
            system_belief=system,
            candidates=candidates,
            planner_members=members,
            available_budget=budget,
            now=args.now,
        )
        result = {"schema_version": "v0.6.1", "decision": decision.to_dict(), "committee": committee.to_dict() if committee else None, "provider_calls": 0, "network_calls": 0}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":")))
        return 0
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"metacontroller input error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
