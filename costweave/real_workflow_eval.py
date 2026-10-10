"""Bounded real multi-model workflow pilot for CostWeave v0.7.1.

The pilot executes two synthetic composite workflows under three pre-registered
policies.  It is intentionally small, deterministic and shadow-only.  It does
not mutate the production route or learn from the result automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapters import (
    AdapterRuntimePolicy,
    DeepSeekChatHTTPAdapter,
    ExperimentalAdapterConfig,
    ExternalExecutionRequest,
    OpenAICompatibleHTTPAdapter,
)
from .domain import RunMode, TaskContract, WorkerProfile
from .event_store import EventStore
from .experimental_calls import ExperimentBudget, ExperimentalCallError, ExperimentalCallStore
from .planning_contracts import WorkOrder
from .provider_setup import DEFAULT_REGISTRY_FILE, project_root, utc_now
from .real_smoke_eval import PRICE_ASSUMPTIONS, _atomic_json, _extract_json_object, _load_registry
from .router import PredictiveRouter, RoutingError


SCHEMA_VERSION = "v0.7.1-real-workflow-pilot-1"
TASKSET_VERSION = "cw-real-pilot-1-plus-workflows-2"
EXPERIMENT_ID = "experiment_real_workflow_20260831_v2"
RUN_ID = "run_real_workflow_20260831_v2"
MAX_AUTHORIZED_BUDGET_USD = 1.0
MAX_LOGICAL_CALLS = 36

PREMIUM_POLICY = "premium_single"
MANUAL_POLICY = "manual_static_v1"
COSTWEAVE_POLICY = "costweave_shadow_v1"
POLICY_ORDER = (PREMIUM_POLICY, MANUAL_POLICY, COSTWEAVE_POLICY)

PROVIDER_BY_SLOT = {
    "deepseek": "deepseek-official",
    "gpt": "traveltoken-gpt",
    "claude": "traveltoken-claude",
}
SLOT_BY_PROVIDER = {value: key for key, value in PROVIDER_BY_SLOT.items()}


class RealWorkflowError(RuntimeError):
    """Safe workflow setup or execution error."""


@dataclass(frozen=True, slots=True)
class NodeSpec:
    node_id: str
    title: str
    objective: str
    dependencies: tuple[str, ...]
    required_keys: tuple[str, ...]
    capabilities: tuple[str, ...]
    task_type: str
    difficulty: int
    max_output_tokens: int = 512
    risk_level: str = "normal"
    criticality: float = 0.6


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    workflow_id: str
    title: str
    management_topology: str
    stages: tuple[tuple[str, ...], ...]
    nodes: tuple[NodeSpec, ...]
    final_node_id: str

    def node_map(self) -> dict[str, NodeSpec]:
        return {node.node_id: node for node in self.nodes}


SOFTWARE_SOURCE = """Contract:
- total_cents must be a non-negative integer.
- weights must be a non-empty list of non-negative integers.
- at least one weight must be positive.
- output integer cents must sum exactly to total_cents.

Current implementation:
def allocate(total_cents, weights):
    scale = total_cents / sum(weights)
    return [round(weight * scale) for weight in weights]

Observed failures:
- allocate(100, [0, 0]) raises ZeroDivisionError.
- allocate(1, [1, 1, 1]) returns [0, 0, 0], violating the exact-total rule.
"""

DECISION_SOURCES = """Decision constraints:
- total first-year cost must be <= 120.
- delivery must be <= 10 days.
- personal data must remain in the EU.
- rollback support is mandatory.

Options:
- A: cost 70, delivery 4 days, US data processing, rollback supported.
- B: signed contract says cost 110, delivery 8 days, EU processing, rollback supported.
- C: cost 140, delivery 12 days, EU processing, rollback supported.

Source conflict:
- B marketing page says cost 90.
- B signed contract says cost 110 and contract evidence outranks marketing.
"""


WORKFLOWS: tuple[WorkflowSpec, ...] = (
    WorkflowSpec(
        workflow_id="W09-software-fix",
        title="复合软件缺陷修复",
        management_topology="single_planner_parallel_specialists",
        stages=(
            ("planner",),
            ("source_analysis", "contract_analysis"),
            ("patch_design",),
            ("independent_review",),
            ("final_assembly",),
        ),
        final_node_id="final_assembly",
        nodes=(
            NodeSpec(
                node_id="planner",
                title="任务拆解",
                objective=(
                    "Act as the planner for the supplied software contract. Return JSON with "
                    "work_items, parallel_group, and success_conditions. work_items must name "
                    "source_analysis, contract_analysis, patch_design, independent_review, "
                    "and final_assembly. parallel_group must contain source_analysis and "
                    "contract_analysis.\n\n" + SOFTWARE_SOURCE
                ),
                dependencies=(),
                required_keys=("work_items", "parallel_group", "success_conditions"),
                capabilities=("planning", "structure", "coding"),
                task_type="planning",
                difficulty=6,
                max_output_tokens=512,
            ),
            NodeSpec(
                node_id="source_analysis",
                title="实现与失败分析",
                objective=(
                    "Analyze only the implementation and observed failures. Return JSON with "
                    "root_cause_ids and evidence. Allowed root-cause IDs are zero_sum_division "
                    "and independent_rounding_loses_total.\n\n" + SOFTWARE_SOURCE
                ),
                dependencies=("planner",),
                required_keys=("root_cause_ids", "evidence"),
                capabilities=("coding", "analysis"),
                task_type="coding",
                difficulty=6,
            ),
            NodeSpec(
                node_id="contract_analysis",
                title="合约与边界分析",
                objective=(
                    "Analyze only the contract boundaries. Return JSON with required_guards and "
                    "invariants. required_guards must use applicable IDs from: total_nonnegative, "
                    "weights_nonempty, weights_nonnegative, positive_weight_exists. invariants "
                    "must include exact_cent_total.\n\n" + SOFTWARE_SOURCE
                ),
                dependencies=("planner",),
                required_keys=("required_guards", "invariants"),
                capabilities=("testing", "validation", "structure"),
                task_type="analysis",
                difficulty=5,
            ),
            NodeSpec(
                node_id="patch_design",
                title="补丁方案拼接",
                objective=(
                    "Synthesize the supplied specialist artifacts into one patch design. Return "
                    "JSON with root_cause_ids, required_guards, allocation_algorithm, and tests. "
                    "allocation_algorithm must be largest_remainder_cents. tests must use IDs "
                    "all_zero_rejected, thirds_sum_exactly, negative_weight_rejected, and "
                    "normal_case_preserved."
                ),
                dependencies=("source_analysis", "contract_analysis"),
                required_keys=("root_cause_ids", "required_guards", "allocation_algorithm", "tests"),
                capabilities=("coding", "synthesis", "testing"),
                task_type="coding",
                difficulty=7,
                max_output_tokens=512,
            ),
            NodeSpec(
                node_id="independent_review",
                title="独立审查",
                objective=(
                    "Independently review the proposed patch against the original contract. "
                    "Return JSON with accepted, checked_root_causes, checked_guards, and "
                    "checked_tests. Set accepted true only if all supplied required IDs are present."
                ),
                dependencies=("patch_design",),
                required_keys=("accepted", "checked_root_causes", "checked_guards", "checked_tests"),
                capabilities=("validation", "coding", "risk"),
                task_type="validation",
                difficulty=7,
                max_output_tokens=512,
                risk_level="high",
                criticality=0.9,
            ),
            NodeSpec(
                node_id="final_assembly",
                title="最终成果组装",
                objective=(
                    "Assemble the final result. Return exactly one JSON object with keys: "
                    "status, root_cause_ids, required_guards, allocation_algorithm, tests, "
                    "review_passed. Use only IDs supported by the supplied artifacts."
                ),
                dependencies=("patch_design", "independent_review"),
                required_keys=("status", "root_cause_ids", "required_guards", "allocation_algorithm", "tests", "review_passed"),
                capabilities=("synthesis", "validation", "coding"),
                task_type="synthesis",
                difficulty=7,
                max_output_tokens=512,
                criticality=0.95,
            ),
        ),
    ),
    WorkflowSpec(
        workflow_id="W10-multiplanner-decision",
        title="MultiPlanner 多来源决策",
        management_topology="metaplanner_selected_multiplanner",
        stages=(
            ("planner_a", "planner_b"),
            ("cost_analysis", "risk_analysis"),
            ("plan_merge",),
            ("final_verification",),
        ),
        final_node_id="final_verification",
        nodes=(
            NodeSpec(
                node_id="planner_a",
                title="效益 Planner",
                objective=(
                    "Plan the decision from cost and schedule perspectives. Return JSON with "
                    "candidate_ids, checks, and provisional_choice. Do not ignore hard constraints.\n\n"
                    + DECISION_SOURCES
                ),
                dependencies=(),
                required_keys=("candidate_ids", "checks", "provisional_choice"),
                capabilities=("planning", "analysis", "math"),
                task_type="planning",
                difficulty=7,
            ),
            NodeSpec(
                node_id="planner_b",
                title="风险 Planner",
                objective=(
                    "Plan the decision from evidence, residency, and rollback perspectives. Return "
                    "JSON with candidate_ids, hard_constraints, source_priority, and "
                    "provisional_choice. Signed contract must outrank marketing.\n\n"
                    + DECISION_SOURCES
                ),
                dependencies=(),
                required_keys=("candidate_ids", "hard_constraints", "source_priority", "provisional_choice"),
                capabilities=("planning", "risk", "validation"),
                task_type="planning",
                difficulty=7,
                risk_level="high",
            ),
            NodeSpec(
                node_id="cost_analysis",
                title="成本与工期分析",
                objective=(
                    "Using both planner artifacts and the fixed sources, return JSON with "
                    "authoritative_costs, delivery_days, and cost_schedule_feasible. Use the signed "
                    "contract price 110 for B.\n\n" + DECISION_SOURCES
                ),
                dependencies=("planner_a", "planner_b"),
                required_keys=("authoritative_costs", "delivery_days", "cost_schedule_feasible"),
                capabilities=("math", "analysis", "structure"),
                task_type="data",
                difficulty=6,
            ),
            NodeSpec(
                node_id="risk_analysis",
                title="合规与证据分析",
                objective=(
                    "Using both planner artifacts and the fixed sources, return JSON with "
                    "residency_feasible, rollback_feasible, conflict_id, and authoritative_source. "
                    "The conflict_id is B_marketing_vs_contract_price and authoritative_source is "
                    "signed_contract.\n\n" + DECISION_SOURCES
                ),
                dependencies=("planner_a", "planner_b"),
                required_keys=("residency_feasible", "rollback_feasible", "conflict_id", "authoritative_source"),
                capabilities=("risk", "validation", "analysis"),
                task_type="analysis",
                difficulty=7,
                risk_level="high",
            ),
            NodeSpec(
                node_id="plan_merge",
                title="多 Planner 结论合并",
                objective=(
                    "Merge the cost and risk artifacts without discarding disagreements. Return "
                    "JSON with selected_option, selected_cost, selected_days, rejected_reason_codes, "
                    "and resolved_conflicts. Allowed rejection codes are A_residency_violation, "
                    "C_budget_violation, and C_deadline_violation."
                ),
                dependencies=("cost_analysis", "risk_analysis"),
                required_keys=("selected_option", "selected_cost", "selected_days", "rejected_reason_codes", "resolved_conflicts"),
                capabilities=("synthesis", "planning", "validation"),
                task_type="synthesis",
                difficulty=8,
                max_output_tokens=512,
                criticality=0.9,
            ),
            NodeSpec(
                node_id="final_verification",
                title="最终独立验收",
                objective=(
                    "Independently verify the merged decision against every hard constraint. Return "
                    "exactly one JSON object with keys status, selected_option, selected_cost, "
                    "selected_days, rejected_reason_codes, conflict_id, authoritative_source, and "
                    "all_hard_constraints_passed."
                ),
                dependencies=("plan_merge", "cost_analysis", "risk_analysis"),
                required_keys=("status", "selected_option", "selected_cost", "selected_days", "rejected_reason_codes", "conflict_id", "authoritative_source", "all_hard_constraints_passed"),
                capabilities=("validation", "risk", "synthesis"),
                task_type="validation",
                difficulty=8,
                max_output_tokens=512,
                risk_level="high",
                criticality=0.98,
            ),
        ),
    ),
)


MANUAL_ROUTES: Mapping[str, Mapping[str, str]] = {
    "W09-software-fix": {
        "planner": "claude",
        "source_analysis": "gpt",
        "contract_analysis": "deepseek",
        "patch_design": "gpt",
        "independent_review": "claude",
        "final_assembly": "claude",
    },
    "W10-multiplanner-decision": {
        "planner_a": "claude",
        "planner_b": "gpt",
        "cost_analysis": "deepseek",
        "risk_analysis": "claude",
        "plan_merge": "gpt",
        "final_verification": "claude",
    },
}


def _workers(provider_rows: Mapping[str, Mapping[str, Any]]) -> tuple[WorkerProfile, ...]:
    common = dict(
        cost_per_task=0.01,
        context_window=100_000,
        max_output_tokens=8_000,
        pricing_currency="USD",
        modalities=["text"],
        tools=[],
        verified_at="2026-08-31",
        availability=0.99,
        data_confidence=0.62,
        local=False,
        routable=True,
    )
    profiles = {
        "deepseek": dict(
            name="DeepSeek V4 Pro", specialty="economical structured reasoning",
            capabilities={"planning": .76, "structure": .88, "coding": .84, "analysis": .84, "testing": .80, "validation": .78, "synthesis": .76, "risk": .74, "math": .90, "safety": .78},
            latency_factor=.78, reliability=.94, reasoning=.83, speed=.90, tier="utility",
        ),
        "gpt": dict(
            name="GPT-5.5 relay claim", specialty="frontier coding and reasoning",
            capabilities={"planning": .94, "structure": .94, "coding": .97, "analysis": .95, "testing": .95, "validation": .94, "synthesis": .94, "risk": .94, "math": .95, "safety": .95},
            latency_factor=1.05, reliability=.96, reasoning=.97, speed=.74, tier="frontier",
        ),
        "claude": dict(
            name="Claude Sonnet 4.6 relay claim", specialty="planning, synthesis and review",
            capabilities={"planning": .96, "structure": .95, "coding": .91, "analysis": .95, "testing": .91, "validation": .96, "synthesis": .97, "risk": .97, "math": .91, "safety": .97},
            latency_factor=.95, reliability=.96, reasoning=.95, speed=.80, tier="premium",
        ),
    }
    result: list[WorkerProfile] = []
    for slot, provider_id in PROVIDER_BY_SLOT.items():
        row = provider_rows[provider_id]
        price = PRICE_ASSUMPTIONS[provider_id]
        result.append(WorkerProfile(
            id=slot,
            provider=provider_id,
            model_id=str(row["selected_model"]),
            input_price_per_mtok=price.input_per_mtok_usd,
            output_price_per_mtok=price.output_per_mtok_usd,
            source_url=price.source_url,
            **common,
            **profiles[slot],
        ))
    return tuple(result)


def _task_contract(workflow: WorkflowSpec, node: NodeSpec) -> TaskContract:
    return TaskContract(
        id=f"{workflow.workflow_id}:{node.node_id}",
        title=node.title,
        objective=node.objective,
        task_type=node.task_type,
        dependencies=[f"{workflow.workflow_id}:{item}" for item in node.dependencies],
        required_capabilities=list(node.capabilities),
        acceptance_criteria=[f"output contains {key}" for key in node.required_keys],
        difficulty=node.difficulty,
        risk_level=node.risk_level,
        estimated_input_tokens=max(400, len(node.objective) // 2),
        estimated_output_tokens=node.max_output_tokens,
        uncertainty=.18,
        criticality=node.criticality,
        classification_confidence=.88,
    )


def freeze_routes(provider_rows: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    router = PredictiveRouter(workers=_workers(provider_rows), catalog_revision="real-pilot-priors-2026-08-31")
    frozen: dict[str, Any] = {}
    for workflow in WORKFLOWS:
        tasks = [_task_contract(workflow, node) for node in workflow.nodes]
        try:
            summary = router.assign(tasks, RunMode.ECONOMY, quality_floor=.68, budget=.35)
        except RoutingError as error:
            raise RealWorkflowError(f"CostWeave 无法为 {workflow.workflow_id} 冻结路线：{error}") from error
        routes = {
            task.id.split(":", 1)[1]: {
                "slot": task.selected_worker,
                "predicted_success": task.predicted_success,
                "success_lower_bound": task.predicted_success_lower_bound,
                "estimated_cost_usd": task.estimated_cost,
                "reason": task.routing_rationale,
            }
            for task in tasks
        }
        frozen[workflow.workflow_id] = {"summary": summary, "routes": routes}
    canonical = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"catalog_revision": router.catalog_revision, "workflows": frozen, "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def _policy_routes(policy_id: str, workflow: WorkflowSpec, frozen: Mapping[str, Any]) -> dict[str, str]:
    if policy_id == PREMIUM_POLICY:
        return {node.node_id: "gpt" for node in workflow.nodes}
    if policy_id == MANUAL_POLICY:
        return dict(MANUAL_ROUTES[workflow.workflow_id])
    if policy_id == COSTWEAVE_POLICY:
        rows = frozen["workflows"][workflow.workflow_id]["routes"]
        return {node.node_id: str(rows[node.node_id]["slot"]) for node in workflow.nodes}
    raise RealWorkflowError(f"未知策略：{policy_id}")


def _append_artifacts(node: NodeSpec, artifacts: Mapping[str, Mapping[str, Any]]) -> str:
    if not node.dependencies:
        return node.objective
    supplied = {dependency: artifacts[dependency] for dependency in node.dependencies}
    return node.objective + "\n\nUPSTREAM_ARTIFACTS_JSON:\n" + json.dumps(supplied, ensure_ascii=False, sort_keys=True)


def validate_node(node: NodeSpec, content: str) -> dict[str, Any]:
    value = _extract_json_object(content)
    checks = {
        "valid_json_object": value is not None,
        "required_keys_present": bool(value is not None and all(key in value for key in node.required_keys)),
    }
    return {"accepted": all(checks.values()), "checks": checks, "parsed": dict(value) if value is not None else None}


def validate_final(workflow_id: str, value: Mapping[str, Any] | None) -> dict[str, Any]:
    if workflow_id == "W09-software-fix":
        expected_causes = {"zero_sum_division", "independent_rounding_loses_total"}
        expected_guards = {"total_nonnegative", "weights_nonempty", "weights_nonnegative", "positive_weight_exists"}
        expected_tests = {"all_zero_rejected", "thirds_sum_exactly", "negative_weight_rejected", "normal_case_preserved"}
        checks = {
            "status_terminal_success": bool(value and value.get("status") in {"completed", "accepted", "verified"}),
            "root_causes_complete": bool(value and set(value.get("root_cause_ids", ())) == expected_causes),
            "guards_complete": bool(value and set(value.get("required_guards", ())) == expected_guards),
            "algorithm_correct": bool(value and value.get("allocation_algorithm") == "largest_remainder_cents"),
            "tests_complete": bool(value and set(value.get("tests", ())) == expected_tests),
            "independent_review_passed": bool(value and value.get("review_passed") is True),
        }
    elif workflow_id == "W10-multiplanner-decision":
        expected_rejections = {"A_residency_violation", "C_budget_violation", "C_deadline_violation"}
        checks = {
            "status_terminal_success": bool(value and value.get("status") in {"completed", "accepted", "verified"}),
            "selected_B": bool(value and value.get("selected_option") == "B"),
            "authoritative_cost": bool(value and value.get("selected_cost") == 110),
            "delivery_days": bool(value and value.get("selected_days") == 8),
            "rejections_complete": bool(value and set(value.get("rejected_reason_codes", ())) == expected_rejections),
            "conflict_identified": bool(value and value.get("conflict_id") == "B_marketing_vs_contract_price"),
            "source_priority_correct": bool(value and value.get("authoritative_source") == "signed_contract"),
            "hard_constraints_passed": bool(value and value.get("all_hard_constraints_passed") is True),
        }
    else:
        raise RealWorkflowError(f"未知工作流：{workflow_id}")
    return {"accepted": all(checks.values()), "score": round(sum(checks.values()) / len(checks) * 100, 2), "checks": checks}


def _work_order(workflow: WorkflowSpec, node: NodeSpec, objective: str, cost_limit: float, policy_id: str) -> WorkOrder:
    task_id = f"task_{policy_id}_{workflow.workflow_id}_{node.node_id}".replace("-", "_")
    return WorkOrder(
        work_order_id=f"work_order_{task_id}",
        task_id=task_id,
        objective=objective,
        dependencies=tuple(node.dependencies),
        input_artifacts=tuple(node.dependencies),
        context_manifest=(),
        assumptions=("All supplied data is synthetic and untrusted.",),
        allowed_tools=(),
        forbidden_actions=("Do not call tools.", "Do not access files, network, secrets, or external systems.", "Return JSON only."),
        output_schema={"type": "object", "required": list(node.required_keys)},
        acceptance_tests=tuple(f"contains {key}" for key in node.required_keys),
        cost_limit=cost_limit,
        time_limit_ms=60_000,
        escalation_conditions=("Required upstream artifact is missing.",),
        idempotency_key=f"work-{TASKSET_VERSION}-{policy_id}-{workflow.workflow_id}-{node.node_id}",
    )


def _execute_node(
    *, workflow: WorkflowSpec, node: NodeSpec, policy_id: str, slot: str,
    artifacts: Mapping[str, Mapping[str, Any]], providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore, per_call_limit: float,
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    provider_id = PROVIDER_BY_SLOT[slot]
    provider = providers[provider_id]
    price = PRICE_ASSUMPTIONS[provider_id]
    objective = _append_artifacts(node, artifacts)
    order = _work_order(workflow, node, objective, per_call_limit, policy_id)
    idempotency_key = f"call-{TASKSET_VERSION}-{policy_id}-{workflow.workflow_id}-{node.node_id}"
    request = ExternalExecutionRequest(
        experiment_id=EXPERIMENT_ID, run_id=RUN_ID, task_id=order.task_id,
        work_order=order, max_output_tokens=node.max_output_tokens,
        idempotency_key=idempotency_key,
    )
    adapter_type = (
        DeepSeekChatHTTPAdapter
        if provider_id == "deepseek-official"
        else OpenAICompatibleHTTPAdapter
    )
    adapter = adapter_type(
        ExperimentalAdapterConfig(
            adapter_id=f"real-workflow-{provider_id}", provider=provider_id,
            endpoint=str(provider["chat_completions_url"]), model_id=str(provider["selected_model"]),
            deployment="remote", input_price_per_mtok_usd=price.input_per_mtok_usd,
            output_price_per_mtok_usd=price.output_per_mtok_usd, timeout_seconds=60.0,
            max_retries=0, secret_env=str(provider["secret_env"]), reasoning_effort="low",
            input_reservation_multiplier=(4.0 if provider.get("trust") != "official" else 1.0),
        ),
        store=store,
        policy=AdapterRuntimePolicy(experimental_calls_enabled=True, paid_calls_enabled=True),
    )
    started = time.perf_counter()
    row: dict[str, Any] = {
        "node_id": node.node_id, "slot": slot, "provider_id": provider_id,
        "model_requested": provider["selected_model"], "status": "failed", "accepted": False,
    }
    try:
        receipt = adapter.execute(request)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        content = str(receipt.result.output.get("content", ""))
        validation = validate_node(node, content)
        telemetry = dict(receipt.telemetry)
        row.update({
            "status": "completed", "accepted": validation["accepted"],
            "schema_validation": {"accepted": validation["accepted"], "checks": validation["checks"]},
            "elapsed_ms": elapsed_ms, "provider_latency_ms": telemetry.get("request_latency_ms"),
            "input_tokens": telemetry.get("input_tokens"), "output_tokens": telemetry.get("output_tokens"),
            "cached_input_tokens": telemetry.get("cached_input_tokens"),
            "reasoning_tokens": telemetry.get("reasoning_tokens"),
            "reasoning_only_response_count": telemetry.get(
                "reasoning_only_response_count"
            ),
            "finish_reasons": telemetry.get("finish_reasons", []),
            "provider_profile_revision": telemetry.get(
                "provider_profile_revision"
            ),
            "estimated_cost_usd": telemetry.get("actual_cost_usd"),
            "provider_response_ids": telemetry.get("provider_response_ids", []),
            "reported_models": telemetry.get("reported_models", []),
            "output_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "raw_output_persisted": False,
        })
        return row, validation["parsed"] if validation["accepted"] else None
    except Exception as error:
        # A transport can have completed and incurred cost before a downstream
        # protocol/security/persistence check fails.  Always recover the durable
        # call record so policy totals include charged-but-rejected work.
        external_call_id = "external_call_" + store._hash(
            {"experiment_id": EXPERIMENT_ID, "idempotency_key": idempotency_key}
        )[:32]
        stored = store.call(external_call_id)
        row.update({
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "error": type(error).__name__, "diagnostic": str(error),
            "estimated_cost_usd": (
                float(stored.get("actual_cost") or 0.0) if stored else 0.0
            ),
            "ledger_call_status": stored.get("status") if stored else None,
            "external_call_id": external_call_id if stored else None,
        })
        return row, None


def _execute_workflow(
    *, workflow: WorkflowSpec, policy_id: str, routes: Mapping[str, str],
    providers: Mapping[str, Mapping[str, Any]], store: ExperimentalCallStore,
    per_call_limit: float,
) -> dict[str, Any]:
    node_map = workflow.node_map()
    artifacts: dict[str, Mapping[str, Any]] = {}
    node_rows: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    workflow_started = time.perf_counter()
    blocked = False
    for stage_index, stage in enumerate(workflow.stages, start=1):
        if blocked:
            for node_id in stage:
                node_rows.append({"node_id": node_id, "slot": routes[node_id], "status": "skipped_dependency_failed", "accepted": False})
            continue
        if any(any(dependency not in artifacts for dependency in node_map[node_id].dependencies) for node_id in stage):
            blocked = True
            for node_id in stage:
                node_rows.append({"node_id": node_id, "slot": routes[node_id], "status": "skipped_missing_artifact", "accepted": False})
            continue
        stage_started = time.perf_counter()
        results: dict[str, tuple[dict[str, Any], Mapping[str, Any] | None]] = {}
        with ThreadPoolExecutor(max_workers=len(stage), thread_name_prefix="costweave-real-workflow") as executor:
            futures = {
                executor.submit(
                    _execute_node, workflow=workflow, node=node_map[node_id], policy_id=policy_id,
                    slot=routes[node_id], artifacts=dict(artifacts), providers=providers,
                    store=store, per_call_limit=per_call_limit,
                ): node_id for node_id in stage
            }
            for future in as_completed(futures):
                node_id = futures[future]
                try:
                    results[node_id] = future.result()
                except Exception as error:  # keep later nodes fail-closed
                    results[node_id] = (
                        {
                            "node_id": node_id,
                            "slot": routes[node_id],
                            "provider_id": PROVIDER_BY_SLOT[routes[node_id]],
                            "status": "failed",
                            "accepted": False,
                            "error": type(error).__name__,
                            "diagnostic": "unexpected_node_failure",
                        },
                        None,
                    )
        wall_ms = round((time.perf_counter() - stage_started) * 1000)
        summed_ms = sum(int(results[node_id][0].get("elapsed_ms") or 0) for node_id in stage)
        observed_overlap = max(0, summed_ms - wall_ms) if len(stage) > 1 else 0
        real_parallel = bool(
            len(stage) > 1
            and observed_overlap > 0
            and all(
                results[node_id][0].get("status") == "completed"
                and int(results[node_id][0].get("provider_latency_ms") or 0) > 0
                for node_id in stage
            )
        )
        stages.append({
            "stage": stage_index, "nodes": list(stage), "parallel": len(stage) > 1,
            "wall_ms": wall_ms, "sum_node_elapsed_ms": summed_ms,
            "observed_parallel_overlap_ms": observed_overlap,
            "real_provider_parallelism_observed": real_parallel,
        })
        for node_id in stage:
            row, artifact = results[node_id]
            node_rows.append(row)
            if artifact is None:
                blocked = True
            else:
                artifacts[node_id] = artifact
    final_value = artifacts.get(workflow.final_node_id)
    final_validation = validate_final(workflow.workflow_id, final_value)
    total_cost = round(sum(float(row.get("estimated_cost_usd") or 0) for row in node_rows), 10)
    return {
        "workflow_id": workflow.workflow_id, "title": workflow.title,
        "management_topology": workflow.management_topology, "policy_id": policy_id,
        "routes": dict(routes), "route_hash": hashlib.sha256(json.dumps(dict(routes), sort_keys=True).encode()).hexdigest(),
        "status": "accepted" if final_validation["accepted"] else "rejected",
        "accepted": final_validation["accepted"], "quality_score": final_validation["score"],
        "final_validation": final_validation, "final_output": dict(final_value) if final_value else None,
        "estimated_cost_usd": total_cost,
        "elapsed_ms": round((time.perf_counter() - workflow_started) * 1000),
        "parallel_stage_count": sum(stage["parallel"] for stage in stages),
        "parallel_execution_observed": any(stage["real_provider_parallelism_observed"] for stage in stages if stage["parallel"]),
        "stages": stages, "nodes": node_rows,
    }


def _aggregate_policy(policy_id: str, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    selected = [row for row in rows if row["policy_id"] == policy_id]
    accepted = sum(bool(row["accepted"]) for row in selected)
    cost = round(sum(float(row["estimated_cost_usd"]) for row in selected), 10)
    return {
        "policy_id": policy_id, "workflow_count": len(selected), "accepted_count": accepted,
        "accepted_rate": round(accepted / len(selected), 4) if selected else 0,
        "mean_quality_score": round(sum(float(row["quality_score"]) for row in selected) / len(selected), 2) if selected else 0,
        "estimated_total_cost_usd": cost,
        "cost_per_accepted_result_usd": round(cost / accepted, 10) if accepted else None,
        "total_elapsed_ms": sum(int(row["elapsed_ms"]) for row in selected),
        "all_parallel_stages_observed": all(bool(row["parallel_execution_observed"]) for row in selected),
    }


def run_real_workflow_pilot(
    *, root: Path, budget_usd: float, registry_path: Path | None = None,
    runtime_path: Path | None = None, report_path: Path | None = None,
) -> dict[str, Any]:
    budget = float(budget_usd)
    if not math.isfinite(budget) or budget <= 0 or budget > MAX_AUTHORIZED_BUDGET_USD + 1e-12:
        raise RealWorkflowError("本轮预算必须在 0 到 1.00 美元之间")
    if os.environ.get("COSTWEAVE_EXPERIMENTAL_ADAPTERS") != "1" or os.environ.get("COSTWEAVE_PAID_CALLS") != "1":
        raise RealWorkflowError("真实工作流评测需要本次运行的双重显式授权开关")
    provider_list = _load_registry(registry_path or root / DEFAULT_REGISTRY_FILE)
    providers = {str(row["provider_id"]): row for row in provider_list}
    frozen_routes = freeze_routes(providers)
    runtime_file = runtime_path or root / "work/local-data/runtime-v4.sqlite3"
    report_file = report_path or root / "work/local-data/real-eval/first-real-workflow-pilot.json"
    route_file = root / "work/local-data/real-eval/first-real-workflow-frozen-plan.json"
    taskset_payload = {
        "taskset_version": TASKSET_VERSION,
        "workflows": [asdict(workflow) for workflow in WORKFLOWS],
        "manual_routes": MANUAL_ROUTES,
        "policy_order": POLICY_ORDER,
    }
    taskset_canonical = json.dumps(
        taskset_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    taskset_sha256 = hashlib.sha256(taskset_canonical.encode("utf-8")).hexdigest()
    _atomic_json(
        route_file,
        {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "taskset_version": TASKSET_VERSION,
            "taskset_sha256": taskset_sha256,
            "frozen_costweave_routes": frozen_routes,
            "paid_calls_started": False,
        },
    )
    EventStore(runtime_file)
    store = ExperimentalCallStore(runtime_file)
    # The split is pre-registered and sums to the user-authorized total.
    provider_limits = {
        "traveltoken-gpt": round(budget * .70, 10),
        "traveltoken-claude": round(budget * .22, 10),
        "deepseek-official": round(budget * .08, 10),
    }
    store.register_experiment(ExperimentBudget(
        experiment_id=EXPERIMENT_ID, total_cost_limit_usd=budget,
        provider_cost_limits_usd=provider_limits, max_logical_calls=MAX_LOGICAL_CALLS,
        max_attempts_per_call=1, purpose="two_composite_workflow_policy_comparison",
        expires_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
    ))
    results: list[dict[str, Any]] = []
    per_call_limit = min(.20, budget / 4)
    for workflow in WORKFLOWS:
        for policy_id in POLICY_ORDER:
            routes = _policy_routes(policy_id, workflow, frozen_routes)
            results.append(_execute_workflow(
                workflow=workflow, policy_id=policy_id, routes=routes,
                providers=providers, store=store, per_call_limit=per_call_limit,
            ))
    aggregates = [_aggregate_policy(policy_id, results) for policy_id in POLICY_ORDER]
    premium = next(row for row in aggregates if row["policy_id"] == PREMIUM_POLICY)
    for row in aggregates:
        if premium["estimated_total_cost_usd"] > 0:
            row["cost_reduction_vs_premium"] = round(
                (premium["estimated_total_cost_usd"] - row["estimated_total_cost_usd"])
                / premium["estimated_total_cost_usd"], 6
            )
        else:
            row["cost_reduction_vs_premium"] = None
    ledger_summary = store.experiment_summary(EXPERIMENT_ID)
    estimated_total = float(ledger_summary["actual_cost_usd"])
    report = {
        "schema_version": SCHEMA_VERSION, "taskset_version": TASKSET_VERSION,
        "experiment_id": EXPERIMENT_ID, "run_id": RUN_ID, "created_at": utc_now(),
        "purpose": "two-composite-workflow-process-proof-pilot",
        "authorized_budget_usd": budget, "estimated_total_cost_usd": estimated_total,
        "provider_limits_usd": provider_limits, "provider_invoice_reconciled": False,
        "ledger_summary": ledger_summary,
        "budget_exceeded": estimated_total > budget + 1e-12,
        "model_identity": {provider_id: {"requested_model": row["selected_model"], "trust": row.get("trust")} for provider_id, row in providers.items()},
        "route_priors_are_configured_not_learned": True,
        "taskset_sha256": taskset_sha256,
        "frozen_plan_path": str(route_file.resolve()),
        "frozen_costweave_routes": frozen_routes,
        "workflow_count": len(WORKFLOWS), "policy_count": len(POLICY_ORDER),
        "maximum_logical_calls": MAX_LOGICAL_CALLS,
        "completed_node_calls": sum(row.get("status") == "completed" for result in results for row in result["nodes"]),
        "all_workflow_policy_pairs_accepted": all(row["accepted"] for row in results),
        "process_proof_checks": {
            "decomposition_artifact_created": all(any(node["node_id"] in {"planner", "planner_a", "planner_b"} and node.get("accepted") for node in result["nodes"]) for result in results),
            "parallel_execution_observed": all(result["parallel_execution_observed"] for result in results),
            "handoffs_schema_valid": all(
                all(
                    node.get("status") == "completed" and node.get("accepted", False)
                    for node in result["nodes"]
                )
                for result in results
            ),
            "final_assembly_accepted": all(result["accepted"] for result in results),
        },
        "policy_aggregates": aggregates, "results": results,
        "commercial_claims_allowed": False, "statistical_superiority_claims_allowed": False,
    }
    _atomic_json(report_file, report)
    return {**report, "report_path": str(report_file.resolve())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行两个复合任务的真实多模型工作流试点")
    parser.add_argument("--budget-usd", type=float, required=True)
    parser.add_argument("--root", type=Path, default=project_root())
    args = parser.parse_args(argv)
    try:
        report = run_real_workflow_pilot(root=args.root.resolve(), budget_usd=args.budget_usd)
    except RealWorkflowError as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["completed_node_calls"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
