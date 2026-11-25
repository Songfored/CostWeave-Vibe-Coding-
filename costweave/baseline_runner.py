"""Five-policy baseline runner for CostWeave v0.5.6.1.

The bundled backend is a deterministic fixture simulator.  It validates the
experiment machinery and cost accounting without making a real provider call.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .domain import RunMode, TaskContract, WorkerProfile
from .evaluation_protocol import (
    BASELINE_CHEAPEST,
    BASELINE_HUMAN_STATIC,
    BASELINE_IDS,
    BASELINE_STRONGEST,
    BASELINE_V04_FIXED,
    BASELINE_V05_ADAPTIVE,
    EVALUATION_PROTOCOL_VERSION,
    FORBIDDEN_BLIND_FIELDS,
    SIMULATED_EVIDENCE_LEVEL,
    BenchmarkTask,
    EvaluationProtocolError,
    ExecutionTaskView,
    ExperimentSpec,
    build_default_experiment_spec,
    canonical_json,
    load_seed_tasks,
    stable_digest,
    validate_task_set,
)
from .model_catalog import MODELS, SNAPSHOT_DATE
from .router import PredictiveRouter, RouteDecision, RoutingError
from .success_contract import SuccessContract


BASELINE_RUNNER_REVISION = "baseline-runner-0.5.6.1"
FIXTURE_BACKEND_REVISION = "deterministic-fixture-0.5.6.1"


@dataclass(frozen=True, slots=True)
class BaselineDefinition:
    baseline_id: str
    label: str
    policy_revision: str
    planning_overhead_ms: int
    handoff_overhead_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_id": self.baseline_id,
            "label": self.label,
            "policy_revision": self.policy_revision,
            "planning_overhead_ms": self.planning_overhead_ms,
            "handoff_overhead_ms": self.handoff_overhead_ms,
        }


@dataclass(frozen=True, slots=True)
class RouteReceipt:
    task_id: str
    baseline_id: str
    policy_revision: str
    model_id: str
    model_name: str
    provider: str
    predicted_success: float
    success_lower_bound: float
    estimated_cost_usd: float
    estimated_latency_ms: int
    planning_overhead_ms: int
    handoff_overhead_ms: int
    rationale: str
    receipt_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "baseline_id": self.baseline_id,
            "policy_revision": self.policy_revision,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "provider": self.provider,
            "predicted_success": round(self.predicted_success, 6),
            "success_lower_bound": round(self.success_lower_bound, 6),
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
            "estimated_latency_ms": self.estimated_latency_ms,
            "planning_overhead_ms": self.planning_overhead_ms,
            "handoff_overhead_ms": self.handoff_overhead_ms,
            "rationale": self.rationale,
            "receipt_id": self.receipt_id,
        }


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    status: str
    accepted: bool
    hard_pass: bool
    soft_quality: float
    criteria_results: tuple[Mapping[str, Any], ...]
    output: Mapping[str, Any]
    cost_breakdown_usd: Mapping[str, float]
    duration_breakdown_ms: Mapping[str, int]
    attempts: int
    simulated_calls: int
    tool_calls: int
    provider_calls: int
    evidence_level: str
    backend_revision: str

    def __post_init__(self) -> None:
        if self.status not in {
            "evaluated",
            "budget_rejected",
            "route_rejected",
        }:
            raise EvaluationProtocolError("unsupported evaluation outcome status")
        if self.accepted and not self.hard_pass:
            raise EvaluationProtocolError(
                "hard failure cannot be covered by a soft score"
            )
        if not 0 <= self.soft_quality <= 1:
            raise EvaluationProtocolError(
                "soft quality must be between zero and one"
            )
        if min(self.attempts, self.simulated_calls, self.tool_calls) < 0:
            raise EvaluationProtocolError("call counters cannot be negative")
        if self.provider_calls != 0:
            raise EvaluationProtocolError(
                "v0.5.6.1 outcome contains a real provider call"
            )
        if self.evidence_level != SIMULATED_EVIDENCE_LEVEL:
            raise EvaluationProtocolError(
                "v0.5.6.1 bundled runs must remain E0 simulated evidence"
            )
        if any(float(value) < 0 for value in self.cost_breakdown_usd.values()):
            raise EvaluationProtocolError("cost components cannot be negative")
        if any(int(value) < 0 for value in self.duration_breakdown_ms.values()):
            raise EvaluationProtocolError("duration components cannot be negative")

    @property
    def total_cost_usd(self) -> float:
        return round(
            sum(float(value) for value in self.cost_breakdown_usd.values()),
            8,
        )

    @property
    def total_duration_ms(self) -> int:
        return sum(int(value) for value in self.duration_breakdown_ms.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "accepted": self.accepted,
            "hard_pass": self.hard_pass,
            "soft_quality": round(self.soft_quality, 6),
            "criteria_results": [
                dict(item) for item in self.criteria_results
            ],
            "output": dict(self.output),
            "cost_breakdown_usd": {
                key: round(float(value), 8)
                for key, value in sorted(self.cost_breakdown_usd.items())
            },
            "total_cost_usd": self.total_cost_usd,
            "duration_breakdown_ms": {
                key: int(value)
                for key, value in sorted(self.duration_breakdown_ms.items())
            },
            "total_duration_ms": self.total_duration_ms,
            "attempts": self.attempts,
            "simulated_calls": self.simulated_calls,
            "tool_calls": self.tool_calls,
            "provider_calls": self.provider_calls,
            "evidence_level": self.evidence_level,
            "backend_revision": self.backend_revision,
        }


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    observation_id: str
    experiment_id: str
    task_id: str
    project_id: str
    partition: str
    baseline_id: str
    blind_alias: str
    route: RouteReceipt | None
    outcome: ExecutionOutcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "experiment_id": self.experiment_id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "partition": self.partition,
            "baseline_id": self.baseline_id,
            "blind_alias": self.blind_alias,
            "route": self.route.to_dict() if self.route else None,
            "outcome": self.outcome.to_dict(),
        }


def rejected_outcome(
    *,
    status: str,
    success_contract: SuccessContract,
    reason: str,
    evidence_ref: str,
    backend_revision: str,
) -> ExecutionOutcome:
    return ExecutionOutcome(
        status=status,
        accepted=False,
        hard_pass=False,
        soft_quality=0.0,
        criteria_results=tuple(
            {
                "criterion_id": criterion.criterion_id,
                "critical": criterion.critical,
                "passed": False,
                "evidence_ref": evidence_ref,
            }
            for criterion in success_contract.criteria
        ),
        output={
            "summary": reason,
            "deliverable": None,
            "evidence": [evidence_ref],
            "confidence": 1.0,
        },
        cost_breakdown_usd={
            "execution": 0.0,
            "retry": 0.0,
            "validation": 0.0,
            "tools": 0.0,
        },
        duration_breakdown_ms={
            "execution": 0,
            "validation": 0,
            "tools": 0,
        },
        attempts=0,
        simulated_calls=0,
        tool_calls=0,
        provider_calls=0,
        evidence_level=SIMULATED_EVIDENCE_LEVEL,
        backend_revision=backend_revision,
    )


@dataclass(frozen=True, slots=True)
class ExperimentReport:
    report_id: str
    spec: ExperimentSpec
    task_set_hash: str
    tasks: tuple[BenchmarkTask, ...]
    baselines: tuple[BaselineDefinition, ...]
    observations: tuple[EvaluationObservation, ...]
    blind_review_packets: tuple[Mapping[str, Any], ...]
    blind_identity_map: tuple[Mapping[str, Any], ...]
    metrics: Mapping[str, Mapping[str, Any]]
    execution_mode: str
    evidence_level: str
    provider_call_count: int
    commercial_claims_allowed: bool
    invariant_checks: Mapping[str, bool]
    passed: bool
    runner_revision: str = BASELINE_RUNNER_REVISION
    schema_version: str = EVALUATION_PROTOCOL_VERSION

    def to_dict(self, *, include_identity_map: bool = True) -> dict[str, Any]:
        payload = {
            "report_id": self.report_id,
            "experiment_id": self.spec.experiment_id,
            "spec": self.spec.to_dict(),
            "task_set_hash": self.task_set_hash,
            "tasks": [item.to_dict() for item in self.tasks],
            "baselines": [item.to_dict() for item in self.baselines],
            "observations": [item.to_dict() for item in self.observations],
            "blind_review_packets": [
                dict(item) for item in self.blind_review_packets
            ],
            "metrics": {
                key: dict(value)
                for key, value in sorted(self.metrics.items())
            },
            "execution_mode": self.execution_mode,
            "evidence_level": self.evidence_level,
            "provider_call_count": self.provider_call_count,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "invariant_checks": dict(self.invariant_checks),
            "passed": self.passed,
            "runner_revision": self.runner_revision,
            "schema_version": self.schema_version,
        }
        if include_identity_map:
            payload["blind_identity_map"] = [
                dict(item) for item in self.blind_identity_map
            ]
        return payload


class EvaluationBackend(Protocol):
    revision: str
    execution_mode: str
    evidence_level: str

    def execute(
        self,
        *,
        task: ExecutionTaskView,
        success_contract: SuccessContract,
        route: RouteReceipt,
        decision: RouteDecision,
        seed: int,
        remaining_calls: int,
        remaining_tool_calls: int,
        remaining_cost_usd: float,
        remaining_wall_seconds: float,
    ) -> ExecutionOutcome:
        """Execute one pre-registered task inside the shared envelope."""


def _unit_interval(*parts: object) -> float:
    digest = hashlib_sha256_text(":".join(str(item) for item in parts))
    return int(digest[:13], 16) / float(0xFFFFFFFFFFFFF)


def hashlib_sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DeterministicFixtureBackend:
    """Route-sensitive fake outcomes used only to test the evaluation system."""

    revision = FIXTURE_BACKEND_REVISION
    execution_mode = "deterministic_fixture_simulation"
    evidence_level = SIMULATED_EVIDENCE_LEVEL

    def execute(
        self,
        *,
        task: ExecutionTaskView,
        success_contract: SuccessContract,
        route: RouteReceipt,
        decision: RouteDecision,
        seed: int,
        remaining_calls: int,
        remaining_tool_calls: int,
        remaining_cost_usd: float,
        remaining_wall_seconds: float,
    ) -> ExecutionOutcome:
        required_tool_calls = len(task.task_contract.requires_tools)
        if remaining_calls < 2 or remaining_tool_calls < required_tool_calls:
            return rejected_outcome(
                status="budget_rejected",
                success_contract=success_contract,
                reason="共享实验资源上限不足，任务未执行。",
                evidence_ref="budget://not-executed",
                backend_revision=self.revision,
            )

        identity = (
            seed,
            task.task_id,
            route.model_id,
            self.revision,
        )
        first_draw = _unit_interval(*identity, "attempt-1")
        first_pass = first_draw <= decision.predicted_success
        attempts = 1
        final_pass = first_pass
        if not first_pass and remaining_calls >= 3:
            attempts = 2
            retry_draw = _unit_interval(*identity, "attempt-2")
            final_pass = retry_draw <= max(
                0.05, decision.predicted_success - 0.06
            )

        tool_calls = required_tool_calls * attempts
        if tool_calls > remaining_tool_calls:
            return rejected_outcome(
                status="budget_rejected",
                success_contract=success_contract,
                reason="共享实验工具调用上限不足，任务未执行。",
                evidence_ref="budget://tool-calls-exhausted",
                backend_revision=self.revision,
            )

        quality_noise = (_unit_interval(*identity, "quality") - 0.5) * 0.12
        soft_quality = max(
            0.0,
            min(
                1.0,
                decision.capability_match * 0.52
                + decision.predicted_success * 0.43
                + 0.05
                + quality_noise,
            ),
        )
        criterion_results = []
        for index, criterion in enumerate(success_contract.criteria):
            if criterion.critical:
                passed = final_pass
            else:
                passed = final_pass or (
                    _unit_interval(*identity, "criterion", index)
                    <= decision.predicted_success * 0.82
                )
            criterion_results.append(
                {
                    "criterion_id": criterion.criterion_id,
                    "critical": criterion.critical,
                    "passed": passed,
                    "evidence_ref": (
                        f"fixture://{task.task_id}/criterion/{index}"
                    ),
                }
            )
        hard_pass = all(
            bool(item["passed"])
            for item in criterion_results
            if item["critical"]
        )
        accepted = (
            hard_pass
            and soft_quality >= success_contract.minimum_score
        )

        execution_cost = max(0.0, route.estimated_cost_usd)
        retry_cost = execution_cost if attempts > 1 else 0.0
        validation_cost = max(0.00001, execution_cost * 0.05)
        tool_cost = 0.00002 * tool_calls
        total_cost = (
            execution_cost + retry_cost + validation_cost + tool_cost
        )
        if total_cost > remaining_cost_usd + 1e-12:
            return rejected_outcome(
                status="budget_rejected",
                success_contract=success_contract,
                reason="共享实验资源上限不足，任务未执行。",
                evidence_ref="budget://not-executed",
                backend_revision=self.revision,
            )

        duration_scale = 0.82 + _unit_interval(*identity, "latency") * 0.36
        execution_ms = max(
            1,
            round(route.estimated_latency_ms * duration_scale * attempts),
        )
        validation_ms = 18 + len(criterion_results) * 4
        tool_ms = tool_calls * 12
        if (
            execution_ms + validation_ms + tool_ms
        ) / 1000 > remaining_wall_seconds:
            return rejected_outcome(
                status="budget_rejected",
                success_contract=success_contract,
                reason="共享实验时间上限不足，任务未执行。",
                evidence_ref="budget://wall-time-exhausted",
                backend_revision=self.revision,
            )
        return ExecutionOutcome(
            status="evaluated",
            accepted=accepted,
            hard_pass=hard_pass,
            soft_quality=soft_quality,
            criteria_results=tuple(criterion_results),
            output={
                "summary": (
                    "已按预注册合同生成候选结果并附带逐项证据。"
                ),
                "deliverable": (
                    f"fixture-result:{task.task_id}"
                ),
                "evidence": [
                    item["evidence_ref"] for item in criterion_results
                ],
                "confidence": round(decision.predicted_success, 4),
            },
            cost_breakdown_usd={
                "execution": execution_cost,
                "retry": retry_cost,
                "validation": validation_cost,
                "tools": tool_cost,
            },
            duration_breakdown_ms={
                "execution": execution_ms,
                "validation": validation_ms,
                "tools": tool_ms,
            },
            attempts=attempts,
            simulated_calls=attempts + 1,
            tool_calls=tool_calls,
            provider_calls=0,
            evidence_level=self.evidence_level,
            backend_revision=self.revision,
        )


class BaselineRunner:
    def __init__(
        self,
        *,
        spec: ExperimentSpec,
        tasks: Sequence[BenchmarkTask],
        workers: Sequence[WorkerProfile],
        backend: EvaluationBackend | None = None,
    ) -> None:
        self.spec = spec
        self.tasks = validate_task_set(tasks)
        self.workers = tuple(workers)
        self.backend = backend or DeterministicFixtureBackend()
        if not self.workers:
            raise EvaluationProtocolError("catalog snapshot cannot be empty")
        ids = [worker.id for worker in self.workers]
        if len(ids) != len(set(ids)):
            raise EvaluationProtocolError("catalog model ids must be unique")
        missing_manual = {
            *self.spec.manual_assignment_rules.values(),
            self.spec.default_manual_model,
        } - set(ids)
        if missing_manual:
            raise EvaluationProtocolError(
                "manual baseline references missing models: "
                + ",".join(sorted(missing_manual))
            )
        if self.backend.evidence_level != SIMULATED_EVIDENCE_LEVEL:
            raise EvaluationProtocolError(
                "v0.5.6.1 accepts only E0 simulated backends"
            )
        self.router = PredictiveRouter(
            workers=self.workers,
            catalog_revision=self.spec.catalog_revision,
        )
        self.baselines = self._baseline_definitions()

    def _baseline_definitions(self) -> tuple[BaselineDefinition, ...]:
        values = (
            (BASELINE_STRONGEST, "全部任务使用最强可行模型", 4, 3),
            (BASELINE_CHEAPEST, "全部任务使用最低价可行模型", 4, 3),
            (BASELINE_HUMAN_STATIC, "人工维护的静态任务类型映射", 20, 5),
            (BASELINE_V04_FIXED, "v0.4 固定难度分层规则", 10, 5),
            (BASELINE_V05_ADAPTIVE, "v0.5 当前自适应策略", 35, 8),
        )
        return tuple(
            BaselineDefinition(
                baseline_id=baseline_id,
                label=label,
                policy_revision=self.spec.policy_versions[baseline_id],
                planning_overhead_ms=planning,
                handoff_overhead_ms=handoff,
            )
            for baseline_id, label, planning, handoff in values
        )

    def run(self) -> ExperimentReport:
        ordered_ids = self.spec.ordered_task_ids(
            item.benchmark_id for item in self.tasks
        )
        by_id = {item.benchmark_id: item for item in self.tasks}
        ordered_tasks = tuple(by_id[item] for item in ordered_ids)
        observations: list[EvaluationObservation] = []

        for baseline in self.baselines:
            used_cost = 0.0
            used_calls = 0
            used_tool_calls = 0
            used_wall_seconds = 0.0
            for task in ordered_tasks:
                minimum_overhead_seconds = (
                    baseline.planning_overhead_ms
                    + baseline.handoff_overhead_ms
                ) / 1000
                if (
                    used_cost >= self.spec.limits.max_cost_usd
                    or used_calls >= self.spec.limits.max_calls
                    or used_wall_seconds + minimum_overhead_seconds
                    > self.spec.limits.max_wall_seconds
                ):
                    observations.append(
                        self._observation(
                            task=task,
                            baseline=baseline,
                            route=None,
                            outcome=rejected_outcome(
                                status="budget_rejected",
                                success_contract=task.success_contract,
                                reason=(
                                    "共享实验资源上限不足，任务未执行。"
                                ),
                                evidence_ref="budget://not-executed",
                                backend_revision=BASELINE_RUNNER_REVISION,
                            ),
                        )
                    )
                    continue
                try:
                    decision = self._select(
                        task.task_copy(), baseline.baseline_id
                    )
                    route = self._receipt(task, baseline, decision)
                except RoutingError as exc:
                    route = None
                    outcome = rejected_outcome(
                        status="route_rejected",
                        success_contract=task.success_contract,
                        reason=str(exc),
                        evidence_ref="route://no-feasible-candidate",
                        backend_revision=BASELINE_RUNNER_REVISION,
                    )
                    observations.append(
                        self._observation(
                            task=task,
                            baseline=baseline,
                            route=route,
                            outcome=outcome,
                        )
                    )
                    continue
                remaining_cost = self.spec.limits.max_cost_usd - used_cost
                remaining_calls = self.spec.limits.max_calls - used_calls
                remaining_tools = (
                    self.spec.limits.max_tool_calls - used_tool_calls
                )
                remaining_wall = (
                    self.spec.limits.max_wall_seconds
                    - used_wall_seconds
                    - (
                        baseline.planning_overhead_ms
                        + baseline.handoff_overhead_ms
                    )
                    / 1000
                )
                if (
                    remaining_cost <= 0
                    or remaining_calls <= 0
                    or remaining_wall <= 0
                ):
                    outcome = rejected_outcome(
                        status="budget_rejected",
                        success_contract=task.success_contract,
                        reason="共享实验资源上限不足，任务未执行。",
                        evidence_ref="budget://not-executed",
                        backend_revision=BASELINE_RUNNER_REVISION,
                    )
                else:
                    outcome = self.backend.execute(
                        task=task.execution_view(),
                        success_contract=task.success_contract,
                        route=route,
                        decision=decision,
                        seed=self.spec.random_seed,
                        remaining_calls=remaining_calls,
                        remaining_tool_calls=remaining_tools,
                        remaining_cost_usd=remaining_cost,
                        remaining_wall_seconds=remaining_wall,
                    )
                if outcome.provider_calls:
                    raise EvaluationProtocolError(
                        "real provider call detected during v0.5.6.1"
                    )
                used_cost += outcome.total_cost_usd
                used_calls += outcome.simulated_calls
                used_tool_calls += outcome.tool_calls
                used_wall_seconds += (
                    outcome.total_duration_ms
                    + baseline.planning_overhead_ms
                    + baseline.handoff_overhead_ms
                ) / 1000
                if used_cost > self.spec.limits.max_cost_usd + 1e-8:
                    raise EvaluationProtocolError(
                        "baseline exceeded its shared cost limit"
                    )
                if used_calls > self.spec.limits.max_calls:
                    raise EvaluationProtocolError(
                        "baseline exceeded its shared call limit"
                    )
                if used_tool_calls > self.spec.limits.max_tool_calls:
                    raise EvaluationProtocolError(
                        "baseline exceeded its shared tool-call limit"
                    )
                observations.append(
                    self._observation(
                        task=task,
                        baseline=baseline,
                        route=route,
                        outcome=outcome,
                    )
                )

        blind_packets, identity_map = self._blind_review(
            ordered_tasks, observations
        )
        metrics = self._compare(observations)
        invariant_checks = self._invariants(
            ordered_tasks, observations, blind_packets
        )
        report_material = {
            "spec": self.spec.to_dict(),
            "task_set_hash": stable_digest(
                [item.to_dict() for item in ordered_tasks]
            ),
            "observation_ids": [
                item.observation_id for item in observations
            ],
            "metrics": metrics,
            "invariants": invariant_checks,
        }
        return ExperimentReport(
            report_id=f"evaluation_{stable_digest(report_material)}",
            spec=self.spec,
            task_set_hash=report_material["task_set_hash"],
            tasks=ordered_tasks,
            baselines=self.baselines,
            observations=tuple(observations),
            blind_review_packets=blind_packets,
            blind_identity_map=identity_map,
            metrics=metrics,
            execution_mode=self.backend.execution_mode,
            evidence_level=self.backend.evidence_level,
            provider_call_count=sum(
                item.outcome.provider_calls for item in observations
            ),
            commercial_claims_allowed=False,
            invariant_checks=invariant_checks,
            passed=all(invariant_checks.values()),
        )

    def _observation(
        self,
        *,
        task: BenchmarkTask,
        baseline: BaselineDefinition,
        route: RouteReceipt | None,
        outcome: ExecutionOutcome,
    ) -> EvaluationObservation:
        alias = self._blind_alias(
            task.benchmark_id, baseline.baseline_id
        )
        identity = {
            "experiment_id": self.spec.experiment_id,
            "task_id": task.benchmark_id,
            "baseline_id": baseline.baseline_id,
            "route_id": route.receipt_id if route else None,
            "outcome": outcome.to_dict(),
        }
        return EvaluationObservation(
            observation_id=f"observation_{stable_digest(identity)}",
            experiment_id=self.spec.experiment_id,
            task_id=task.benchmark_id,
            project_id=task.project_id,
            partition=task.partition,
            baseline_id=baseline.baseline_id,
            blind_alias=alias,
            route=route,
            outcome=outcome,
        )

    def _ranked(self, task: TaskContract) -> list[RouteDecision]:
        ranked = list(
            self.router.rank_candidates(task, RunMode.BALANCED)
        )
        viable = [
            item
            for item in ranked
            if item.success_lower_bound >= self.spec.quality_floor
            and math.isfinite(item.estimated_cost)
        ]
        if not viable:
            raise RoutingError(
                f"no feasible baseline route for {task.id}"
            )
        return viable

    def _select(
        self, task: TaskContract, baseline_id: str
    ) -> RouteDecision:
        viable = self._ranked(task)
        if baseline_id == BASELINE_STRONGEST:
            return max(
                viable,
                key=lambda item: (
                    item.success_lower_bound,
                    item.predicted_success,
                    item.worker.reliability,
                    -item.estimated_cost,
                    item.worker.id,
                ),
            )
        if baseline_id == BASELINE_CHEAPEST:
            return min(
                viable,
                key=lambda item: (
                    item.estimated_cost,
                    -item.success_lower_bound,
                    item.estimated_latency_ms,
                    item.worker.id,
                ),
            )
        if baseline_id == BASELINE_HUMAN_STATIC:
            target = self.spec.manual_assignment_rules.get(
                task.task_type, self.spec.default_manual_model
            )
            selected = next(
                (item for item in viable if item.worker.id == target),
                None,
            )
            if selected is None:
                raise RoutingError(
                    f"manual static assignment {target} is not feasible "
                    f"for {task.id}"
                )
            return selected
        if baseline_id == BASELINE_V04_FIXED:
            if task.risk_level == "high" or task.difficulty >= 8:
                tiers = {"frontier", "premium"}
            elif task.difficulty <= 3:
                tiers = {"economy", "local", "utility"}
            else:
                tiers = {"balanced"}
            fixed_pool = [
                item for item in viable if item.worker.tier in tiers
            ] or viable
            return min(
                fixed_pool,
                key=lambda item: (
                    item.estimated_cost,
                    -item.success_lower_bound,
                    item.worker.id,
                ),
            )
        if baseline_id == BASELINE_V05_ADAPTIVE:
            return max(
                viable,
                key=lambda item: (
                    item.utility,
                    item.success_lower_bound,
                    -item.estimated_cost,
                    item.worker.id,
                ),
            )
        raise EvaluationProtocolError(
            f"unknown baseline policy: {baseline_id}"
        )

    def _receipt(
        self,
        task: BenchmarkTask,
        baseline: BaselineDefinition,
        decision: RouteDecision,
    ) -> RouteReceipt:
        material = {
            "experiment_id": self.spec.experiment_id,
            "task_id": task.benchmark_id,
            "baseline_id": baseline.baseline_id,
            "policy_revision": baseline.policy_revision,
            "model_id": decision.worker.id,
            "catalog_hash": self.spec.catalog_hash,
        }
        return RouteReceipt(
            task_id=task.benchmark_id,
            baseline_id=baseline.baseline_id,
            policy_revision=baseline.policy_revision,
            model_id=decision.worker.id,
            model_name=decision.worker.name,
            provider=decision.worker.provider,
            predicted_success=decision.predicted_success,
            success_lower_bound=decision.success_lower_bound,
            estimated_cost_usd=decision.estimated_cost,
            estimated_latency_ms=decision.estimated_latency_ms,
            planning_overhead_ms=baseline.planning_overhead_ms,
            handoff_overhead_ms=baseline.handoff_overhead_ms,
            rationale=decision.rationale,
            receipt_id=f"route_{stable_digest(material)}",
        )

    def _blind_alias(self, task_id: str, baseline_id: str) -> str:
        return "output_" + stable_digest(
            {
                "seed": self.spec.random_seed,
                "experiment_id": self.spec.experiment_id,
                "task_id": task_id,
                "baseline_id": baseline_id,
            },
            length=16,
        )

    def _blind_review(
        self,
        tasks: Sequence[BenchmarkTask],
        observations: Sequence[EvaluationObservation],
    ) -> tuple[
        tuple[Mapping[str, Any], ...],
        tuple[Mapping[str, Any], ...],
    ]:
        task_lookup = {item.benchmark_id: item for item in tasks}
        packets = []
        identities = []
        for observation in observations:
            task = task_lookup[observation.task_id]
            route = observation.route
            packet_digest = stable_digest({
                "experiment": self.spec.experiment_id,
                "alias": observation.blind_alias,
            })
            packets.append(
                {
                    "review_packet_id": f"review_{packet_digest}",
                    "experiment_id": self.spec.experiment_id,
                    "task_id": observation.task_id,
                    "output_alias": observation.blind_alias,
                    "user_goal": task.user_goal,
                    "success_contract": task.success_contract.to_dict(),
                    "candidate_output": dict(observation.outcome.output),
                    "hard_review": {
                        "criteria": [
                            {
                                "criterion_id": item.criterion_id,
                                "text": item.text,
                                "critical": item.critical,
                                "evidence_required": (
                                    item.evidence_required
                                ),
                            }
                            for item in task.success_contract.criteria
                        ],
                        "submitted_evidence": list(
                            observation.outcome.output.get(
                                "evidence", []
                            )
                        ),
                        "review_result": None,
                    },
                    "soft_review": {
                        "dimensions": [
                            "correctness",
                            "completeness",
                            "clarity",
                        ],
                        "score": None,
                        "minimum_score": (
                            task.success_contract.minimum_score
                        ),
                    },
                    "identity_hidden": True,
                }
            )
            identities.append(
                {
                    "output_alias": observation.blind_alias,
                    "baseline_id": observation.baseline_id,
                    "model_id": route.model_id if route else None,
                    "provider": route.provider if route else None,
                    "policy_revision": (
                        route.policy_revision if route else None
                    ),
                    "total_cost_usd": (
                        observation.outcome.total_cost_usd
                    ),
                }
            )
        if self.spec.blind_review.randomize_output_order:
            packets.sort(
                key=lambda item: (
                    stable_digest(
                        {
                            "seed": self.spec.random_seed,
                            "alias": item["output_alias"],
                            "order": "blind",
                        },
                        length=16,
                    ),
                    item["output_alias"],
                )
            )
        return tuple(packets), tuple(identities)

    @staticmethod
    def _loss(observation: EvaluationObservation) -> float:
        route = observation.route
        overhead_ms = (
            (route.planning_overhead_ms + route.handoff_overhead_ms)
            if route
            else 0
        )
        duration_seconds = (
            observation.outcome.total_duration_ms + overhead_ms
        ) / 1000
        return (
            (0.0 if observation.outcome.accepted else 1.0)
            + observation.outcome.total_cost_usd * 0.25
            + duration_seconds / 3_600 * 0.05
        )

    def _compare(
        self, observations: Sequence[EvaluationObservation]
    ) -> dict[str, dict[str, Any]]:
        oracle_loss: dict[str, float] = {}
        for task_id in {item.task_id for item in observations}:
            oracle_loss[task_id] = min(
                self._loss(item)
                for item in observations
                if item.task_id == task_id
            )

        result: dict[str, dict[str, Any]] = {}
        for baseline_id in BASELINE_IDS:
            items = [
                item
                for item in observations
                if item.baseline_id == baseline_id
            ]
            accepted = sum(item.outcome.accepted for item in items)
            total_cost = sum(
                item.outcome.total_cost_usd for item in items
            )
            total_duration_ms = sum(
                item.outcome.total_duration_ms
                + (
                    item.route.planning_overhead_ms
                    + item.route.handoff_overhead_ms
                    if item.route
                    else 0
                )
                for item in items
            )
            overhead_ms = sum(
                (
                    item.route.planning_overhead_ms
                    + item.route.handoff_overhead_ms
                )
                if item.route
                else 0
                for item in items
            )
            result[baseline_id] = {
                "tasks": len(items),
                "evaluated": sum(
                    item.outcome.status == "evaluated" for item in items
                ),
                "accepted": accepted,
                "simulated_acceptance_rate": round(
                    accepted / len(items) if items else 0.0, 6
                ),
                "mean_soft_quality": round(
                    sum(item.outcome.soft_quality for item in items)
                    / len(items)
                    if items
                    else 0.0,
                    6,
                ),
                "hard_failures": sum(
                    not item.outcome.hard_pass for item in items
                ),
                "total_cost_usd": round(total_cost, 8),
                "cost_per_accepted_task_usd": (
                    round(total_cost / accepted, 8)
                    if accepted
                    else None
                ),
                "time_to_accepted_result_seconds": (
                    round(total_duration_ms / 1000 / accepted, 6)
                    if accepted
                    else None
                ),
                "planning_and_handoff_overhead_ms": overhead_ms,
                "simulated_calls": sum(
                    item.outcome.simulated_calls for item in items
                ),
                "tool_calls": sum(
                    item.outcome.tool_calls for item in items
                ),
                "provider_calls": sum(
                    item.outcome.provider_calls for item in items
                ),
                "routing_regret": round(
                    sum(
                        self._loss(item) - oracle_loss[item.task_id]
                        for item in items
                    )
                    / len(items)
                    if items
                    else 0.0,
                    8,
                ),
                "evidence_level": SIMULATED_EVIDENCE_LEVEL,
            }
        strongest = result[BASELINE_STRONGEST]
        for metrics in result.values():
            metrics["quality_non_inferiority_delta"] = round(
                metrics["mean_soft_quality"]
                - strongest["mean_soft_quality"],
                6,
            )
            metrics["acceptance_non_inferiority_delta"] = round(
                metrics["simulated_acceptance_rate"]
                - strongest["simulated_acceptance_rate"],
                6,
            )
        return result

    def _invariants(
        self,
        tasks: Sequence[BenchmarkTask],
        observations: Sequence[EvaluationObservation],
        packets: Sequence[Mapping[str, Any]],
    ) -> dict[str, bool]:
        expected = len(tasks) * len(BASELINE_IDS)
        packet_json = [canonical_json(item).lower() for item in packets]
        blind = all(
            not any(
                f'"{field.lower()}"' in raw
                for field in FORBIDDEN_BLIND_FIELDS
            )
            for raw in packet_json
        )
        packet_by_alias = {
            str(packet["output_alias"]): canonical_json(packet).lower()
            for packet in packets
        }
        for observation in observations:
            route = observation.route
            if route is None:
                continue
            raw = packet_by_alias[observation.blind_alias]
            identities = (
                observation.baseline_id,
                route.model_id,
                route.model_name,
                route.provider,
                route.policy_revision,
            )
            if any(
                str(identity).lower() in raw
                for identity in identities
                if identity
            ):
                blind = False
                break
        within_limits = True
        for baseline_id in BASELINE_IDS:
            items = [
                item
                for item in observations
                if item.baseline_id == baseline_id
            ]
            total_seconds = sum(
                (
                    item.outcome.total_duration_ms
                    + (
                        item.route.planning_overhead_ms
                        + item.route.handoff_overhead_ms
                        if item.route
                        else 0
                    )
                )
                / 1000
                for item in items
            )
            within_limits = within_limits and (
                sum(item.outcome.total_cost_usd for item in items)
                <= self.spec.limits.max_cost_usd + 1e-8
                and sum(
                    item.outcome.simulated_calls for item in items
                )
                <= self.spec.limits.max_calls
                and sum(item.outcome.tool_calls for item in items)
                <= self.spec.limits.max_tool_calls
                and total_seconds
                <= self.spec.limits.max_wall_seconds + 1e-8
            )
        return {
            "five_baselines": tuple(
                item.baseline_id for item in self.baselines
            )
            == BASELINE_IDS,
            "complete_observation_matrix": len(observations) == expected,
            "success_contract_preregistered": all(
                bool(item.success_contract.criteria) for item in tasks
            ),
            "hard_failure_not_soft_overridden": all(
                not item.outcome.accepted or item.outcome.hard_pass
                for item in observations
            ),
            "blind_identity_hidden": blind,
            "blind_reviews_unscored": all(
                item["hard_review"]["review_result"] is None
                and item["soft_review"]["score"] is None
                for item in packets
            ),
            "reference_answers_not_in_execution_view": all(
                "reference_answer" not in item.execution_view().to_dict()
                and "independent_validator"
                not in item.execution_view().to_dict()
                for item in tasks
            ),
            "provider_calls_zero": all(
                item.outcome.provider_calls == 0
                for item in observations
            ),
            "cost_components_reconcile": all(
                abs(
                    item.outcome.total_cost_usd
                    - sum(item.outcome.cost_breakdown_usd.values())
                )
                <= 1e-8
                for item in observations
            ),
            "project_partition_isolated": bool(validate_task_set(tasks)),
            "shared_resource_envelope_respected": within_limits,
            "commercial_claims_disabled": True,
        }


def catalog_snapshot_hash(workers: Sequence[WorkerProfile]) -> str:
    return stable_digest(
        [
            worker.to_dict()
            for worker in sorted(workers, key=lambda item: item.id)
        ]
    )


def build_seed_runner(
    *,
    random_seed: int = 5601,
    workers: Sequence[WorkerProfile] = MODELS,
    backend: EvaluationBackend | None = None,
) -> BaselineRunner:
    frozen_workers = tuple(workers)
    spec = build_default_experiment_spec(
        catalog_revision=f"builtin-{SNAPSHOT_DATE}",
        catalog_hash=catalog_snapshot_hash(frozen_workers),
        random_seed=random_seed,
    )
    return BaselineRunner(
        spec=spec,
        tasks=load_seed_tasks(),
        workers=frozen_workers,
        backend=backend,
    )


def export_report(
    report: ExperimentReport,
    output_directory: str | Path,
) -> dict[str, str]:
    root = Path(output_directory)
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "experiment.json"
    observations_path = root / "observations.jsonl"
    tasks_path = root / "tasks.jsonl"
    blind_path = root / "blind-review.jsonl"
    summary_path = root / "baseline-summary.csv"

    report_content = (
        json.dumps(
            report.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    observations_content = (
        "".join(
            canonical_json(item.to_dict()) + "\n"
            for item in report.observations
        )
    )
    tasks_content = (
        "".join(
            canonical_json(item.to_dict()) + "\n"
            for item in report.tasks
        )
    )
    blind_content = (
        "".join(
            canonical_json(dict(item)) + "\n"
            for item in report.blind_review_packets
        )
    )

    buffer = io.StringIO(newline="")
    fieldnames = [
        "baseline_id",
        "tasks",
        "evaluated",
        "accepted",
        "simulated_acceptance_rate",
        "mean_soft_quality",
        "hard_failures",
        "total_cost_usd",
        "cost_per_accepted_task_usd",
        "time_to_accepted_result_seconds",
        "planning_and_handoff_overhead_ms",
        "simulated_calls",
        "tool_calls",
        "provider_calls",
        "routing_regret",
        "quality_non_inferiority_delta",
        "acceptance_non_inferiority_delta",
        "evidence_level",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for baseline_id in BASELINE_IDS:
        writer.writerow(
            {
                "baseline_id": baseline_id,
                **report.metrics[baseline_id],
            }
        )
    contents = {
        report_path: report_content,
        observations_path: observations_content,
        tasks_path: tasks_content,
        blind_path: blind_content,
        summary_path: buffer.getvalue(),
    }
    for path, content in contents.items():
        encoded = content.encode("utf-8")
        if path.exists() and path.read_bytes() != encoded:
            raise EvaluationProtocolError(
                f"immutable evaluation artifact conflict: {path.name}"
            )
    for path, content in contents.items():
        if path.exists():
            continue
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(content.encode("utf-8"))
        temporary.replace(path)
    return {
        "report": str(report_path),
        "observations_jsonl": str(observations_path),
        "tasks_jsonl": str(tasks_path),
        "blind_review_jsonl": str(blind_path),
        "summary_csv": str(summary_path),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the CostWeave v0.5.6.1 zero-provider-call "
            "evaluation protocol"
        )
    )
    parser.add_argument(
        "--output-dir",
        default="work/v0561-evaluation",
        help="directory for immutable JSONL/CSV evaluation artifacts",
    )
    parser.add_argument("--seed", type=int, default=5601)
    parser.add_argument(
        "--database",
        help="optional CostWeave runtime SQLite database for report persistence",
    )
    args = parser.parse_args(argv)

    report = build_seed_runner(random_seed=args.seed).run()
    artifacts = export_report(report, args.output_dir)
    if args.database:
        from .evaluation_store import EvaluationProtocolStore
        from .event_store import EventStore

        EventStore(args.database)
        EvaluationProtocolStore(args.database).record(report)
    print(
        json.dumps(
            {
                "report_id": report.report_id,
                "passed": report.passed,
                "evidence_level": report.evidence_level,
                "provider_call_count": report.provider_call_count,
                "commercial_claims_allowed": (
                    report.commercial_claims_allowed
                ),
                "artifacts": artifacts,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BASELINE_RUNNER_REVISION",
    "BaselineDefinition",
    "BaselineRunner",
    "DeterministicFixtureBackend",
    "EvaluationBackend",
    "EvaluationObservation",
    "ExecutionOutcome",
    "ExperimentReport",
    "RouteReceipt",
    "build_seed_runner",
    "catalog_snapshot_hash",
    "export_report",
    "main",
]
