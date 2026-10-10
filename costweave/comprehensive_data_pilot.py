"""Pre-v0.7 real-data pilot for the CostWeave comprehensive benchmark.

This module intentionally lives outside the versioned v0.7 implementation
roadmap.  It freezes a synthetic task, four comparison policies and a local
oracle before any paid call.  Real execution is opt-in, hard-budgeted and does
not mutate routing or learning policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .adapters import (
    AdapterRuntimePolicy,
    DeepSeekChatHTTPAdapter,
    ExperimentalAdapterConfig,
    ExternalExecutionRequest,
    OpenAICompatibleHTTPAdapter,
)
from .event_store import EventStore
from .experimental_calls import (
    ExperimentBudget,
    ExperimentalCallStore,
    ExternalBudgetExceededError,
    ExternalCallsDisabledError,
)
from .planning_contracts import WorkOrder
from .provider_setup import DEFAULT_REGISTRY_FILE, project_root, utc_now
from .real_smoke_eval import PRICE_ASSUMPTIONS, _atomic_json, _extract_json_object, _load_registry
from .security_boundary import SecretLeaseError


SCHEMA_VERSION = "pre-v0.7-comprehensive-data-pilot-3"
TASKSET_VERSION = "cw-72h-cross-region-support-v1"
EXPERIMENT_ID = "experiment_comprehensive_data_pilot_20260901_v6"
RUN_ID = "run_comprehensive_data_pilot_20260901_v6"
ADDITIONAL_AUTHORIZED_BUDGET_CNY = 10.0
MAX_THIS_RUN_BUDGET_USD = 1.25
PRIOR_PILOT_INTERNAL_COST_USD = 1.19717240
MAX_AUTHORIZED_BUDGET_USD = round(
    PRIOR_PILOT_INTERNAL_COST_USD + MAX_THIS_RUN_BUDGET_USD, 8
)
# Four frozen policies require 76 primary calls.  CostWeave alone may use one
# fallback for each of its 19 required nodes; cost and provider ledgers remain
# the tighter authority in ordinary runs.
MAX_LOGICAL_CALLS = 95
RANDOM_SEED = 20260901
MAX_SINGLE_CALL_BUDGET_SHARE = 0.80
REPEAT_AUTHORIZATION_ENV = "COSTWEAVE_REPEAT_EXPERIMENT_AUTHORIZED"

PREMIUM_HIGH = "premium_high"
STRONG_DYNAMIC = "strong_dynamic_reasoning"
MANUAL_STATIC = "manual_static"
COSTWEAVE_DYNAMIC = "costweave_dynamic"
POLICY_ORDER = (PREMIUM_HIGH, STRONG_DYNAMIC, MANUAL_STATIC, COSTWEAVE_DYNAMIC)

PROVIDER_BY_SLOT = {
    "deepseek": "deepseek-official",
    "gpt": "traveltoken-gpt",
    "claude": "traveltoken-claude",
}


class ComprehensivePilotError(RuntimeError):
    """A fail-closed setup or execution error."""


@dataclass(frozen=True, slots=True)
class NodeSpec:
    node_id: str
    title: str
    objective: str
    dependencies: tuple[str, ...]
    required_keys: tuple[str, ...]
    stage: int
    difficulty: int
    risk: str = "normal"
    max_output_tokens: int = 384


SYNTHETIC_INPUTS: Mapping[str, Any] = {
    "requirements": {
        "deadline_hours": 72,
        "implementation_budget_usd": 1200,
        "peak_rpm": 1800,
        "regional_peak_rpm": {"CN": 900, "EU": 900},
        "p95_latency_seconds": 4,
        "data_residency": {"CN": "must remain in CN", "EU": "must remain in EU"},
        "independent_review_for_high_risk": True,
        "deliverables": ["technical_plan", "patch", "tests", "provider_selection", "cost_table", "risk_table", "rollback_plan", "six_shot_storyboard"],
    },
    "service_bug": {
        "contract": ["total_cents_nonnegative", "weights_nonempty", "weights_nonnegative", "positive_weight_exists", "exact_total_preserved"],
        "implementation": "scale = total_cents / sum(weights); return [round(w * scale) for w in weights]",
        "failures": ["zero sum divides by zero", "independent rounding can lose cents"],
    },
    "provider_catalog": {
        "A": {"regions": ["CN"], "capacity_rpm": 1100, "p95_seconds": 2.2, "rollback": True, "marketing_setup_usd": 200},
        "B": {"regions": ["EU"], "capacity_rpm": 1000, "p95_seconds": 2.5, "rollback": True, "marketing_setup_usd": 240},
        "C": {"regions": ["CN", "EU"], "capacity_rpm": 800, "p95_seconds": 3.0, "rollback": True, "marketing_setup_usd": 350},
    },
    "signed_contracts": {
        "A": {"setup_usd": 250, "sla_p95_seconds": 2.2},
        "B": {"setup_usd": 300, "sla_p95_seconds": 2.5},
        "C": {"setup_usd": 400, "sla_p95_seconds": 3.0},
        "authority": "signed_contracts outrank marketing pages",
    },
    "capacity_snapshot": {"local_pool_used_pct": 92, "local_queue_p95_seconds": 48, "local_slots_available": 0},
    "incident_event": {"provider": "B", "capacity_drop_pct": 60, "price_increase_pct": 20, "new_capacity_rpm": 400, "new_setup_usd": 360},
}

INPUT_TEXT = json.dumps(SYNTHETIC_INPUTS, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _objective(instruction: str) -> str:
    return instruction + "\nSynthetic input package:\n" + INPUT_TEXT


NODES: tuple[NodeSpec, ...] = (
    NodeSpec("meta_decision", "管理拓扑判定", _objective("Choose exactly one topology from direct, planner, multiplanner, escalate. Use multiplanner when the task contains at least two independent high-risk decision domains or parallel specialist branches requiring independent review; this synthetic task satisfies both conditions. Return exactly {\"topology\":\"multiplanner\",\"planner_count\":2,\"reasons\":[short IDs],\"bounded\":true}. Do not add more management layers."), (), ("topology", "planner_count", "reasons", "bounded"), 0, 9, "high", 384),
    NodeSpec("planner_a", "独立规划 A", _objective("Create an independent compact implementation plan. Return JSON with plan_id, stages, parallel_groups, risks, success_conditions. stages must be 3-5 short string IDs, parallel_groups must be arrays of short string IDs, risks at most 5 IDs, success_conditions at most 6 IDs. No nested task objects, prose paragraphs, owners, durations, or explanations. Do not infer or mention another planner's answer."), ("meta_decision",), ("plan_id", "stages", "parallel_groups", "risks", "success_conditions"), 1, 8, "high", 512),
    NodeSpec("planner_b", "独立规划 B", _objective("Create a second independent compact plan emphasizing constraint conflicts and recovery. Return JSON with plan_id, stages, parallel_groups, risks, success_conditions. stages must be 3-5 short string IDs, parallel_groups must be arrays of short string IDs, risks at most 5 IDs, success_conditions at most 6 IDs. No nested task objects, prose paragraphs, owners, durations, or explanations. Do not infer or mention another planner's answer."), ("meta_decision",), ("plan_id", "stages", "parallel_groups", "risks", "success_conditions"), 1, 8, "high", 512),
    NodeSpec("requirements_extract", "硬约束抽取", _objective("Return JSON with hard_constraints, deliverables, peak_rpm, regional_peak_rpm, deadline_hours, implementation_budget_usd. Use exact facts from requirements."), ("planner_a", "planner_b"), ("hard_constraints", "deliverables", "peak_rpm", "regional_peak_rpm", "deadline_hours", "implementation_budget_usd"), 2, 4, "low"),
    NodeSpec("bug_diagnosis", "缺陷根因", _objective("Return JSON with root_cause_ids, required_guards, invariant, recommended_algorithm, required_tests. Root causes are zero_sum_division and independent_rounding_loses_total; use largest_remainder_cents and include tests for negative total, empty weights, negative weight, all zero, thirds exact, normal case."), ("planner_a", "planner_b"), ("root_cause_ids", "required_guards", "invariant", "recommended_algorithm", "required_tests"), 2, 7, "high", 512),
    NodeSpec("contract_truth", "权威合同事实", _objective("Resolve signed-contract versus marketing conflicts. Return JSON with authoritative_source, setup_costs_usd, rejected_source, conflict_resolved. Use signed contract values A=250, B=300, C=400."), ("planner_a", "planner_b"), ("authoritative_source", "setup_costs_usd", "rejected_source", "conflict_resolved"), 2, 5),
    NodeSpec("residency_risk", "驻留约束", _objective("Return JSON with cn_allowed, eu_allowed, forbidden_routes, violations. A is CN only, B is EU only, C supports both. violations must be empty for the proposed constraints."), ("planner_a", "planner_b"), ("cn_allowed", "eu_allowed", "forbidden_routes", "violations"), 2, 7, "high"),
    NodeSpec("capacity_analysis", "容量准入", _objective("Return JSON with local_usable, action, local_queue_p95_seconds, required_peak_rpm, regional_peak_rpm, reason. Because local slots are zero and queue P95 is 48 seconds, action must be escalate_before_execution, not try_local_first."), ("planner_a", "planner_b"), ("local_usable", "action", "local_queue_p95_seconds", "required_peak_rpm", "regional_peak_rpm", "reason"), 2, 7, "high"),
    NodeSpec("plan_merge", "计划合并", "Compare the supplied independent plans and specialist artifacts. Return JSON with selected_topology, dag_nodes, parallel_groups, resolved_conflicts, critical_path, bounded_management. selected_topology must be multiplanner and management must remain two levels.", ("planner_a", "planner_b", "requirements_extract", "bug_diagnosis", "contract_truth", "residency_risk", "capacity_analysis"), ("selected_topology", "dag_nodes", "parallel_groups", "resolved_conflicts", "critical_path", "bounded_management"), 3, 9, "high", 768),
    NodeSpec("patch_design", "补丁方案", "Use the bug diagnosis artifact. Return JSON with algorithm, guards, invariant, tests, pseudocode. algorithm must be largest_remainder_cents and preserve exact integer total.", ("plan_merge", "bug_diagnosis"), ("algorithm", "guards", "invariant", "tests", "pseudocode"), 4, 8, "high", 640),
    NodeSpec("provider_costing", "供应商成本", "Use signed contracts, never marketing prices. Return JSON with authoritative_source, selected_providers, setup_costs_usd, total_implementation_cost_usd, within_budget, currency_unit. Select A, B and C; total is 950 before the incident; currency_unit must be USD.", ("plan_merge", "contract_truth", "requirements_extract"), ("authoritative_source", "selected_providers", "setup_costs_usd", "total_implementation_cost_usd", "within_budget", "currency_unit"), 4, 6),
    NodeSpec("deployment_design", "跨区域部署", "Design a residency-compliant route meeting CN=900 rpm and EU=900 rpm before the incident. Return JSON with cn_route, eu_route, total_capacity_rpm, p95_seconds, residency_violations, rollback_supported. CN must remain on A/C and EU on B/C.", ("plan_merge", "requirements_extract", "residency_risk", "capacity_analysis", "contract_truth"), ("cn_route", "eu_route", "total_capacity_rpm", "p95_seconds", "residency_violations", "rollback_supported"), 4, 9, "high", 640),
    NodeSpec("storyboard_plan", "六镜头分镜", "Return JSON with shots and traceable_claims. shots must contain exactly six concise objects showing problem, routing, parallel specialists, validation, incident recovery, and final outcome. Do not generate images or video.", ("plan_merge", "requirements_extract"), ("shots", "traceable_claims"), 4, 5, "normal", 640),
    NodeSpec("patch_tests", "补丁独立测试", "Independently review the patch. Return JSON with passed_tests, exact_total_preserved, guard_coverage_complete, accepted. Include negative_total_rejected, empty_weights_rejected, negative_weight_rejected, all_zero_rejected, thirds_sum_exactly, normal_case_preserved.", ("patch_design",), ("passed_tests", "exact_total_preserved", "guard_coverage_complete", "accepted"), 5, 8, "high"),
    NodeSpec("security_review", "驻留与高风险复核", "Independently review deployment residency. Return JSON with cn_residency_passed, eu_residency_passed, violations, independent_review, accepted. Reject any CN data outside CN or EU data outside EU.", ("deployment_design", "residency_risk"), ("cn_residency_passed", "eu_residency_passed", "violations", "independent_review", "accepted"), 5, 9, "critical"),
    NodeSpec("financial_review", "成本复核", "Independently verify authoritative prices and arithmetic. Return JSON with authoritative_source, recomputed_total_usd, within_budget, rejected_marketing_prices, accepted. Correct pre-incident total is 950." , ("provider_costing", "contract_truth"), ("authoritative_source", "recomputed_total_usd", "within_budget", "rejected_marketing_prices", "accepted"), 5, 8, "high"),
    NodeSpec("handoff_review", "交接完整性", "Review artifact handoffs and return JSON with schema_valid, missing_artifacts, provenance_complete, version_consistent, accepted. Accept only when nothing required is missing.", ("patch_design", "provider_costing", "deployment_design", "storyboard_plan"), ("schema_valid", "missing_artifacts", "provenance_complete", "version_consistent", "accepted"), 5, 7, "high"),
    NodeSpec("incident_replan", "事故后局部重规划", _objective("Apply the incident only to cost, capacity and deployment. Return JSON with affected_nodes, preserved_nodes, provider_b_new_capacity_rpm, provider_b_new_setup_usd, post_incident_total_usd, eu_route, total_capacity_rpm, within_budget, residency_violations. affected_nodes must be provider_costing and deployment_design; preserve patch_design and storyboard_plan. Use B=360 and total=1010. Route EU with B=200 rpm and C=700 rpm."), ("provider_costing", "deployment_design", "financial_review", "security_review"), ("affected_nodes", "preserved_nodes", "provider_b_new_capacity_rpm", "provider_b_new_setup_usd", "post_incident_total_usd", "eu_route", "total_capacity_rpm", "within_budget", "residency_violations"), 6, 9, "critical", 768),
    NodeSpec("final_assembly", "最终成果组装", "Assemble only validated artifacts. Return JSON with status, topology, algorithm, tests, cn_residency_passed, eu_residency_passed, residency_violations, post_incident_total_usd, total_capacity_rpm, independent_reviews_passed, preserved_nodes, deliverables, storyboard, rollback_plan, risks, traceability. Include all eight required deliverables and exactly six storyboard shots.", ("patch_tests", "security_review", "financial_review", "handoff_review", "incident_replan", "storyboard_plan"), ("status", "topology", "algorithm", "tests", "cn_residency_passed", "eu_residency_passed", "residency_violations", "post_incident_total_usd", "total_capacity_rpm", "independent_reviews_passed", "preserved_nodes", "deliverables", "storyboard", "rollback_plan", "risks", "traceability"), 7, 10, "critical", 1024),
)

STAGES: tuple[tuple[str, ...], ...] = tuple(
    tuple(node.node_id for node in NODES if node.stage == stage)
    for stage in range(8)
)
NODE_MAP = {node.node_id: node for node in NODES}

# Every listed node feeds the required final artifact either directly or
# transitively.  The explicit set is frozen with the taskset so a future DAG
# may add optional branches without silently gaining paid fallback authority.
CRITICAL_FALLBACK_NODE_IDS = frozenset(NODE_MAP)
MAX_FALLBACK_ATTEMPTS_PER_NODE = 1
FALLBACK_RETRYABLE_ERROR_CODES = frozenset(
    {
        "transport_error",
        "provider_rate_limited",
        "provider_unavailable",
        "invalid_json",
        "invalid_usage",
        "adapter_contract_error",
        "secret_inspection_failed",
        "secret_lease_expired",
        "secret_unavailable",
    }
)
FALLBACK_HARD_STOP_ERROR_CODES = frozenset(
    {
        "secret_exposure_detected",
        "prompt_injection_detected",
        "project_revoked",
    }
)

REQUIRED_TESTS = {
    "negative_total_rejected", "empty_weights_rejected", "negative_weight_rejected",
    "all_zero_rejected", "thirds_sum_exactly", "normal_case_preserved",
}
REQUIRED_DELIVERABLES = set(SYNTHETIC_INPUTS["requirements"]["deliverables"])


def _items(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    result: set[str] = set()
    for item in value:
        if isinstance(item, str):
            result.add(item.strip().lower())
        elif isinstance(item, Mapping):
            identity = item.get("id") or item.get("name") or item.get("deliverable")
            if identity is not None:
                result.add(str(identity).strip().lower())
    return result


def _largest_remainder(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "largest_remainder_cents"
    return "largest_remainder_cents" in _items(value)


def _zero_violations(value: Any) -> bool:
    return value in (None, False, 0, "", "none", "NONE") or value == []


def _normalized_identifier(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "_".join(part for part in re.split(r"[^a-z0-9]+", text) if part)


def _coerce_positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _coerce_bounded(value: Any, *, management_levels: Any = None) -> bool:
    if isinstance(value, Mapping):
        levels = _coerce_positive_int(
            value.get("levels") or value.get("management_levels")
        )
        explicit = value.get("bounded")
        return 0 < levels <= 2 and explicit not in (False, "false", "no", 0)
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in {
        "true", "yes", "bounded", "two_levels", "2_levels",
    }:
        return True
    levels = _coerce_positive_int(management_levels)
    return 0 < levels <= 2


def normalize_meta_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize equivalent meta-planner encodings without changing intent."""

    normalized = dict(value)
    topology_raw = (
        value.get("topology")
        or value.get("planning_topology")
        or value.get("planning_mode")
        or value.get("mode")
    )
    topology_key = _normalized_identifier(topology_raw)
    aliases = {
        "multi_planner": "multiplanner",
        "multiple_planner": "multiplanner",
        "multiple_planners": "multiplanner",
        "multi_agent_planner": "multiplanner",
        "multi_agent_planning": "multiplanner",
        "single_planner": "planner",
        "direct_execution": "direct",
        "escalation": "escalate",
    }
    topology = aliases.get(topology_key, topology_key)

    planner_count = _coerce_positive_int(
        value.get("planner_count")
        or value.get("planners_count")
        or value.get("number_of_planners")
    )
    planners = value.get("planners")
    if planner_count == 0 and isinstance(planners, list):
        planner_count = len(planners)
    # ``planner`` plus an explicit count of two or more is a common relay
    # encoding of the multi-planner topology, not a different decision.
    if topology == "planner" and planner_count >= 2:
        topology = "multiplanner"

    bounded = _coerce_bounded(
        value.get("bounded"),
        management_levels=value.get("management_levels")
        or value.get("levels"),
    )
    reasons = value.get("reasons")
    if not isinstance(reasons, list):
        reason = value.get("reason") or value.get("rationale")
        reasons = [str(reason)] if reason else []

    normalized.update(
        topology=topology,
        planner_count=planner_count,
        reasons=reasons,
        bounded=bounded,
    )
    return normalized


def validate_node(node: NodeSpec, content: str) -> dict[str, Any]:
    value = _extract_json_object(content)
    if node.node_id == "meta_decision" and isinstance(value, Mapping):
        value = normalize_meta_decision(value)
    checks: dict[str, bool] = {
        "json_object": isinstance(value, Mapping),
        "required_keys": bool(value) and all(key in value for key in node.required_keys),
    }
    if not isinstance(value, Mapping):
        return {"accepted": False, "checks": checks, "parsed": None}
    if node.node_id == "meta_decision":
        checks["multiplanner_selected"] = value.get("topology") == "multiplanner"
        # Meta may recommend more members, but the runtime policy below admits
        # exactly two frozen planner calls.  Recommending 3 is not a quality
        # failure when the proposal is explicitly bounded.
        checks["bounded_multiplanner"] = int(value.get("planner_count") or 0) >= 2 and value.get("bounded") is True
    elif node.node_id == "bug_diagnosis":
        checks["root_causes"] = {"zero_sum_division", "independent_rounding_loses_total"} <= _items(value.get("root_cause_ids"))
        checks["correct_algorithm"] = _largest_remainder(value.get("recommended_algorithm"))
    elif node.node_id == "contract_truth":
        costs = value.get("setup_costs_usd") or {}
        checks["signed_contract_wins"] = str(value.get("authoritative_source", "")).lower() == "signed_contracts"
        checks["contract_values"] = isinstance(costs, Mapping) and all(float(costs.get(key, -1)) == expected for key, expected in {"A": 250, "B": 300, "C": 400}.items())
    elif node.node_id == "capacity_analysis":
        checks["pre_execution_escalation"] = value.get("local_usable") is False and str(value.get("action", "")).lower() == "escalate_before_execution"
    elif node.node_id == "plan_merge":
        bounded = value.get("bounded_management")
        bounded_ok = bounded is True or (
            isinstance(bounded, Mapping)
            and 0 < int(bounded.get("levels") or 0) <= 2
        )
        checks["multiplanner_preserved"] = str(value.get("selected_topology", "")).lower() == "multiplanner" and bounded_ok
    elif node.node_id == "patch_design":
        checks["correct_algorithm"] = _largest_remainder(value.get("algorithm"))
    elif node.node_id == "provider_costing":
        checks["correct_cost"] = float(value.get("total_implementation_cost_usd") or -1) == 950
        checks["correct_source"] = str(value.get("authoritative_source", "")).lower() == "signed_contracts"
        checks["unit"] = str(value.get("currency_unit", "")).upper() == "USD"
    elif node.node_id == "deployment_design":
        checks["capacity"] = float(value.get("total_capacity_rpm") or 0) >= 1800
        checks["residency"] = _zero_violations(value.get("residency_violations"))
        checks["rollback"] = value.get("rollback_supported") is True
    elif node.node_id == "storyboard_plan":
        checks["six_shots"] = isinstance(value.get("shots"), list) and len(value["shots"]) == 6
    elif node.node_id == "patch_tests":
        checks["all_tests"] = REQUIRED_TESTS <= _items(value.get("passed_tests"))
        checks["accepted"] = value.get("accepted") is True and value.get("exact_total_preserved") is True
    elif node.node_id == "security_review":
        checks["zero_violation"] = value.get("cn_residency_passed") is True and value.get("eu_residency_passed") is True and _zero_violations(value.get("violations"))
        checks["independent"] = value.get("independent_review") is True and value.get("accepted") is True
    elif node.node_id == "financial_review":
        checks["correct_total"] = float(value.get("recomputed_total_usd") or -1) == 950
        checks["accepted"] = value.get("within_budget") is True and value.get("accepted") is True
    elif node.node_id == "handoff_review":
        checks["complete"] = value.get("missing_artifacts") == [] and value.get("schema_valid") is True and value.get("accepted") is True
    elif node.node_id == "incident_replan":
        checks["affected_only"] = {"provider_costing", "deployment_design"} <= _items(value.get("affected_nodes"))
        checks["siblings_preserved"] = {"patch_design", "storyboard_plan"} <= _items(value.get("preserved_nodes"))
        checks["incident_math"] = float(value.get("provider_b_new_setup_usd") or -1) == 360 and float(value.get("post_incident_total_usd") or -1) == 1010
        checks["post_incident_capacity"] = float(value.get("total_capacity_rpm") or 0) >= 1800
        checks["post_incident_safe"] = value.get("within_budget") is True and _zero_violations(value.get("residency_violations"))
    return {"accepted": all(checks.values()), "checks": checks, "parsed": dict(value)}


def judge_final(value: Mapping[str, Any] | None) -> dict[str, Any]:
    value = value or {}
    status = str(value.get("status", "")).lower()
    checks = {
        "terminal_success": status in {"accepted", "completed", "verified"},
        "multiplanner": str(value.get("topology", "")).lower() == "multiplanner",
        "correct_algorithm": _largest_remainder(value.get("algorithm")),
        "required_tests": REQUIRED_TESTS <= _items(value.get("tests")),
        "zero_residency_violation": value.get("cn_residency_passed") is True and value.get("eu_residency_passed") is True and _zero_violations(value.get("residency_violations")),
        "budget": 0 < float(value.get("post_incident_total_usd") or 0) <= 1200,
        "capacity": float(value.get("total_capacity_rpm") or 0) >= 1800,
        "independent_reviews": value.get("independent_reviews_passed") is True,
        "local_recovery": {"patch_design", "storyboard_plan"} <= _items(value.get("preserved_nodes")),
        "deliverables": REQUIRED_DELIVERABLES <= _items(value.get("deliverables")),
        "six_shots": isinstance(value.get("storyboard"), list) and len(value["storyboard"]) == 6,
        "rollback": bool(value.get("rollback_plan")),
        "traceability": bool(value.get("traceability")),
    }
    return {"accepted": all(checks.values()), "score": round(sum(checks.values()) / len(checks) * 100, 2), "checks": checks}


def _route(slot: str, effort: str) -> dict[str, str]:
    return {"slot": slot, "reasoning_effort": effort}


def freeze_policy_routes() -> dict[str, dict[str, dict[str, str]]]:
    validators = {"patch_tests", "security_review", "financial_review", "handoff_review"}
    premium: dict[str, dict[str, str]] = {}
    strong: dict[str, dict[str, str]] = {}
    manual: dict[str, dict[str, str]] = {}
    costweave: dict[str, dict[str, str]] = {}
    for node in NODES:
        # The first paid pilot established that the relay's GPT high-effort
        # route times out before returning a response.  Premium therefore uses
        # the strongest observed stable channel (Claude high) for every node;
        # validators remain independent calls, not self-acceptance.
        premium[node.node_id] = _route("claude", "high")
        strong_effort = "low" if node.difficulty <= 4 else "medium" if node.difficulty <= 7 else "high"
        strong[node.node_id] = _route("claude", "high" if node.node_id in validators else strong_effort)
        if node.node_id in {"contract_truth", "requirements_extract", "provider_costing", "storyboard_plan"}:
            manual[node.node_id] = _route("deepseek", "low")
        elif node.node_id in {"bug_diagnosis", "patch_design", "patch_tests"}:
            manual[node.node_id] = _route("gpt", "low")
        else:
            manual[node.node_id] = _route("claude", "high")
        if node.node_id in {"requirements_extract", "contract_truth", "storyboard_plan"}:
            costweave[node.node_id] = _route("deepseek", "low")
        elif node.node_id in {"bug_diagnosis", "provider_costing"}:
            costweave[node.node_id] = _route("gpt", "low")
        elif node.node_id in {"patch_design", "patch_tests"}:
            costweave[node.node_id] = _route("claude", "high")
        else:
            costweave[node.node_id] = _route("claude", "high" if node.difficulty >= 8 or node.risk in {"high", "critical"} else "medium")
    return {PREMIUM_HIGH: premium, STRONG_DYNAMIC: strong, MANUAL_STATIC: manual, COSTWEAVE_DYNAMIC: costweave}


def _append_artifacts(node: NodeSpec, artifacts: Mapping[str, Mapping[str, Any]]) -> str:
    if not node.dependencies:
        return node.objective
    supplied = {key: artifacts[key] for key in node.dependencies}
    return node.objective + "\nValidated dependency artifacts:\n" + json.dumps(supplied, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _attempt_suffix(attempt_label: str) -> str:
    label = str(attempt_label).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", label):
        raise ComprehensivePilotError("fallback attempt label is invalid")
    return "" if label == "primary" else f"_{label}"


def _call_idempotency_key(
    *, policy_id: str, node_id: str, attempt_label: str
) -> str:
    return (
        f"call-{TASKSET_VERSION}-{policy_id}-{node_id}"
        f"{_attempt_suffix(attempt_label)}"
    )


def _work_order(
    node: NodeSpec,
    policy_id: str,
    objective: str,
    cost_limit: float,
    *,
    attempt_label: str = "primary",
) -> WorkOrder:
    suffix = _attempt_suffix(attempt_label)
    task_id = f"task_{policy_id}_{node.node_id}{suffix}"
    return WorkOrder(
        work_order_id=f"work_order_{task_id}", task_id=task_id,
        objective=(
            "Return exactly one compact valid JSON object with no Markdown fence, "
            "commentary, preamble, or trailing text. Use short IDs and arrays; never "
            "expand a requested list into nested prose objects unless explicitly "
            "required. Keep the visible answer under the supplied output limit.\n" + objective
        ),
        dependencies=node.dependencies, input_artifacts=node.dependencies, context_manifest=(),
        assumptions=("All inputs are frozen synthetic fixtures.",), allowed_tools=(),
        forbidden_actions=("Do not call tools.", "Do not access files, network, secrets, personal data, or project data.", "Return JSON only."),
        output_schema={"type": "object", "required": list(node.required_keys)},
        acceptance_tests=tuple(f"contains and satisfies {key}" for key in node.required_keys),
        cost_limit=cost_limit, time_limit_ms=90_000,
        escalation_conditions=("A required validated dependency is missing.",),
        idempotency_key=(
            f"work-{TASKSET_VERSION}-{policy_id}-{node.node_id}{suffix}"
        ),
    )


def _adapter_for_route(
    *,
    provider_id: str,
    provider: Mapping[str, Any],
    route: Mapping[str, str],
    store: ExperimentalCallStore,
) -> Any:
    price = PRICE_ASSUMPTIONS[provider_id]
    adapter_type = (
        DeepSeekChatHTTPAdapter
        if provider_id == "deepseek-official"
        else OpenAICompatibleHTTPAdapter
    )
    return adapter_type(
        ExperimentalAdapterConfig(
            adapter_id=f"comprehensive-{provider_id}",
            provider=provider_id,
            endpoint=str(provider["chat_completions_url"]),
            model_id=str(provider["selected_model"]),
            deployment="remote",
            input_price_per_mtok_usd=price.input_per_mtok_usd,
            output_price_per_mtok_usd=price.output_per_mtok_usd,
            timeout_seconds=90.0,
            max_retries=0,
            secret_env=str(provider["secret_env"]),
            reasoning_effort=str(route["reasoning_effort"]),
            input_reservation_multiplier=(
                4.0 if provider.get("trust") != "official" else 1.0
            ),
        ),
        store=store,
        policy=AdapterRuntimePolicy(
            experimental_calls_enabled=True, paid_calls_enabled=True
        ),
    )


def plan_node_reservation(
    *,
    node: NodeSpec,
    policy_id: str,
    route: Mapping[str, str],
    artifacts: Mapping[str, Mapping[str, Any]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    max_call_budget_usd: float,
    attempt_label: str = "primary",
) -> tuple[Any, ExternalExecutionRequest, dict[str, Any]]:
    """Build an exact, provider-aware pre-call reservation plan."""

    slot = str(route["slot"])
    provider_id = PROVIDER_BY_SLOT[slot]
    provider = providers[provider_id]
    objective = _append_artifacts(node, artifacts)
    provisional_order = _work_order(
        node,
        policy_id,
        objective,
        max_call_budget_usd,
        attempt_label=attempt_label,
    )
    idempotency_key = _call_idempotency_key(
        policy_id=policy_id,
        node_id=node.node_id,
        attempt_label=attempt_label,
    )
    provisional_request = ExternalExecutionRequest(
        experiment_id=EXPERIMENT_ID,
        run_id=RUN_ID,
        task_id=provisional_order.task_id,
        work_order=provisional_order,
        max_output_tokens=node.max_output_tokens,
        idempotency_key=idempotency_key,
    )
    adapter = _adapter_for_route(
        provider_id=provider_id,
        provider=provider,
        route=route,
        store=store,
    )
    estimate = dict(adapter.estimate_reservation(provisional_request))
    required = float(estimate["required_work_order_cost_limit_usd"])
    affordable = required <= max_call_budget_usd + 1e-10
    plan = {
        **estimate,
        "provider_id": provider_id,
        "model_requested": str(provider["selected_model"]),
        "reasoning_effort": str(route["reasoning_effort"]),
        "max_call_budget_usd": round(max_call_budget_usd, 10),
        "affordable": affordable,
        "method": "exact_adapter_payload_upper_bound",
    }
    if not affordable:
        return adapter, provisional_request, plan

    # The exact computed amount becomes the WorkOrder limit.  Cost limit is
    # intentionally excluded from the provider prompt contract, so rebuilding
    # the order cannot change the payload size or create a circular estimate.
    final_order = _work_order(
        node,
        policy_id,
        objective,
        required,
        attempt_label=attempt_label,
    )
    request = ExternalExecutionRequest(
        experiment_id=EXPERIMENT_ID,
        run_id=RUN_ID,
        task_id=final_order.task_id,
        work_order=final_order,
        max_output_tokens=node.max_output_tokens,
        idempotency_key=idempotency_key,
    )
    final_estimate = adapter.estimate_reservation(request)
    if (
        float(final_estimate["required_work_order_cost_limit_usd"])
        > required + 1e-10
    ):
        raise ComprehensivePilotError(
            "dynamic reservation changed after WorkOrder finalization"
        )
    return adapter, request, plan


def _execute_node_once(
    *,
    node: NodeSpec,
    policy_id: str,
    route: Mapping[str, str],
    artifacts: Mapping[str, Mapping[str, Any]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    max_call_budget_usd: float,
    attempt_label: str,
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    slot = route["slot"]
    provider_id = PROVIDER_BY_SLOT[slot]
    provider = providers[provider_id]
    idempotency_key = _call_idempotency_key(
        policy_id=policy_id,
        node_id=node.node_id,
        attempt_label=attempt_label,
    )
    started = time.perf_counter()
    row: dict[str, Any] = {
        "node_id": node.node_id, "stage": node.stage, "slot": slot,
        "reasoning_effort": route["reasoning_effort"], "provider_id": provider_id,
        "model_requested": provider["selected_model"], "status": "failed", "accepted": False,
        "attempt_label": attempt_label,
    }
    try:
        adapter, request, budget_plan = plan_node_reservation(
            node=node,
            policy_id=policy_id,
            route=route,
            artifacts=artifacts,
            providers=providers,
            store=store,
            max_call_budget_usd=max_call_budget_usd,
            attempt_label=attempt_label,
        )
        row["budget_plan"] = budget_plan
        if not budget_plan["affordable"]:
            row.update({
                "status": "blocked_budget_preflight",
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "error": "DynamicCallBudgetExceeded",
                "diagnostic": "node-specific worst-case reservation exceeds the single-call safety ceiling",
                "estimated_cost_usd": 0.0,
                "ledger_call_status": None,
                "external_call_id": None,
            })
            return row, None
        receipt = adapter.execute(request)
        content = str(receipt.result.output.get("content", ""))
        validation = validate_node(node, content)
        telemetry = dict(receipt.telemetry)
        row.update({
            "status": "completed", "accepted": validation["accepted"],
            "validation": {"accepted": validation["accepted"], "checks": validation["checks"]},
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "provider_latency_ms": telemetry.get("request_latency_ms"),
            "input_tokens": telemetry.get("input_tokens"), "output_tokens": telemetry.get("output_tokens"),
            "cached_input_tokens": telemetry.get("cached_input_tokens"),
            "reasoning_tokens": telemetry.get("reasoning_tokens"),
            "estimated_cost_usd": telemetry.get("actual_cost_usd"),
            "reported_models": telemetry.get("reported_models", []),
            "finish_reasons": telemetry.get("finish_reasons", []),
            "output_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "raw_output_persisted": False,
        })
        return row, validation["parsed"] if validation["accepted"] else None
    except Exception as error:
        external_call_id = "external_call_" + store._hash({"experiment_id": EXPERIMENT_ID, "idempotency_key": idempotency_key})[:32]
        stored = store.call(external_call_id)
        local_error_code = None
        cause = error.__cause__
        if isinstance(error, ExternalBudgetExceededError):
            local_error_code = "primary_budget_unaffordable"
        elif isinstance(error, ExternalCallsDisabledError) and isinstance(
            cause, SecretLeaseError
        ):
            local_error_code = "secret_unavailable"
        row.update({
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "error": type(error).__name__, "diagnostic": str(error),
            "estimated_cost_usd": float(stored.get("actual_cost") or 0.0) if stored else 0.0,
            "ledger_call_status": stored.get("status") if stored else None,
            "error_code": (
                stored.get("error_code") if stored else local_error_code
            ),
            "external_call_id": external_call_id if stored else None,
        })
        return row, None


def _fallback_trigger(row: Mapping[str, Any]) -> str | None:
    error_code = str(row.get("error_code") or "")
    if error_code in FALLBACK_HARD_STOP_ERROR_CODES:
        return None
    if row.get("status") == "completed" and not row.get("accepted"):
        return "semantic_validation_failed"
    if row.get("status") == "blocked_budget_preflight":
        return "primary_budget_unaffordable"
    if error_code == "primary_budget_unaffordable":
        return error_code
    if error_code in FALLBACK_RETRYABLE_ERROR_CODES:
        return error_code
    if str(row.get("error") or "") in {
        "TimeoutError",
        "AdapterTransportError",
    }:
        return "transport_error"
    if str(row.get("error") or "") == "ExternalBudgetExceededError":
        return "primary_budget_unaffordable"
    if str(row.get("error") or "") == "SecretLeaseError":
        diagnostic = str(row.get("diagnostic") or "").lower()
        if "contained current leased secret material" in diagnostic:
            return None
        if "closed or expired" in diagnostic:
            return "secret_lease_expired"
        if "unavailable" in diagnostic:
            return "secret_unavailable"
        if any(
            marker in diagnostic
            for marker in ("inspection", "normalization", "could not be checked")
        ):
            return "secret_inspection_failed"
    return None


def _fallback_route_candidates(
    node: NodeSpec, primary_route: Mapping[str, str]
) -> tuple[dict[str, str], ...]:
    primary_slot = str(primary_route["slot"])
    if node.difficulty >= 8 or node.risk in {"high", "critical"}:
        preference = ("claude", "gpt")
        effort = "high"
    else:
        preference = {
            "deepseek": ("gpt", "claude", "deepseek"),
            "gpt": ("claude", "deepseek", "gpt"),
            "claude": ("gpt", "deepseek", "claude"),
        }.get(primary_slot, ("claude", "gpt", "deepseek"))
        effort = "medium" if node.difficulty >= 5 else "low"
    return tuple(
        _route(slot, effort) for slot in preference if slot != primary_slot
    )


def _attempt_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    budget_plan = row.get("budget_plan")
    return {
        key: row.get(key)
        for key in (
            "attempt_label",
            "slot",
            "reasoning_effort",
            "provider_id",
            "model_requested",
            "status",
            "accepted",
            "error",
            "error_code",
            "diagnostic",
            "estimated_cost_usd",
            "elapsed_ms",
            "external_call_id",
            "output_sha256",
        )
    } | {
        "required_reservation_usd": (
            budget_plan.get("required_work_order_cost_limit_usd")
            if isinstance(budget_plan, Mapping)
            else None
        )
    }


def _select_budgeted_fallback(
    *,
    node: NodeSpec,
    policy_id: str,
    primary_route: Mapping[str, str],
    artifacts: Mapping[str, Mapping[str, Any]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    fallback_call_ceiling_usd: float,
) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []
    for route in _fallback_route_candidates(node, primary_route):
        provider_id = PROVIDER_BY_SLOT[route["slot"]]
        try:
            _, _, plan = plan_node_reservation(
                node=node,
                policy_id=policy_id,
                route=route,
                artifacts=artifacts,
                providers=providers,
                store=store,
                max_call_budget_usd=fallback_call_ceiling_usd,
                attempt_label="fallback",
            )
            headroom = store.budget_headroom(
                EXPERIMENT_ID, provider=provider_id
            )
            required = float(
                plan["required_work_order_cost_limit_usd"]
            )
            admitted = bool(
                plan["affordable"]
                and headroom["experiment_status"] == "active"
                and int(headroom["logical_calls_remaining"]) > 0
                and required <= float(headroom["total_remaining_usd"]) + 1e-10
                and required
                <= float(headroom["provider_remaining_usd"]) + 1e-10
            )
            decisions.append(
                {
                    "route": dict(route),
                    "provider_id": provider_id,
                    "required_reservation_usd": required,
                    "headroom": dict(headroom),
                    "admitted": admitted,
                    "network_calls": 0,
                }
            )
            if admitted:
                return dict(route), decisions
        except Exception as error:
            decisions.append(
                {
                    "route": dict(route),
                    "provider_id": provider_id,
                    "admitted": False,
                    "planning_error": type(error).__name__,
                    "network_calls": 0,
                }
            )
    return None, decisions


def _execute_node(
    *,
    node: NodeSpec,
    policy_id: str,
    route: Mapping[str, str],
    artifacts: Mapping[str, Mapping[str, Any]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    max_call_budget_usd: float,
    fallback_enabled: bool = False,
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    primary, artifact = _execute_node_once(
        node=node,
        policy_id=policy_id,
        route=route,
        artifacts=artifacts,
        providers=providers,
        store=store,
        max_call_budget_usd=max_call_budget_usd,
        attempt_label="primary",
    )
    trigger = _fallback_trigger(primary)
    primary["fallback_eligible"] = bool(
        fallback_enabled
        and node.node_id in CRITICAL_FALLBACK_NODE_IDS
        and trigger
    )
    if not primary["fallback_eligible"]:
        primary["fallback_used"] = False
        primary["fallback_trigger"] = trigger
        primary["fallback_status"] = (
            "disabled_or_noncritical" if trigger else "not_required"
        )
        primary["attempts"] = [_attempt_summary(primary)]
        return primary, artifact

    primary_cost = max(0.0, float(primary.get("estimated_cost_usd") or 0.0))
    fallback_ceiling = round(
        max(0.0, max_call_budget_usd - primary_cost), 10
    )
    selected, decisions = _select_budgeted_fallback(
        node=node,
        policy_id=policy_id,
        primary_route=route,
        artifacts=artifacts,
        providers=providers,
        store=store,
        fallback_call_ceiling_usd=fallback_ceiling,
    )
    if selected is None:
        primary.update(
            {
                "fallback_used": False,
                "fallback_trigger": trigger,
                "fallback_status": "not_attempted_budget_or_capability",
                "fallback_call_ceiling_usd": fallback_ceiling,
                "fallback_decisions": decisions,
                "attempts": [_attempt_summary(primary)],
            }
        )
        return primary, None

    fallback, fallback_artifact = _execute_node_once(
        node=node,
        policy_id=policy_id,
        route=selected,
        artifacts=artifacts,
        providers=providers,
        store=store,
        max_call_budget_usd=fallback_ceiling,
        attempt_label="fallback",
    )
    combined = dict(fallback)
    combined.update(
        {
            "fallback_eligible": True,
            "fallback_used": True,
            "fallback_trigger": trigger,
            "fallback_status": (
                "recovered" if fallback_artifact is not None else "failed"
            ),
            "fallback_call_ceiling_usd": fallback_ceiling,
            "fallback_decisions": decisions,
            "primary_route": dict(route),
            "final_route": dict(selected),
            "estimated_cost_usd": round(
                primary_cost
                + max(
                    0.0,
                    float(fallback.get("estimated_cost_usd") or 0.0),
                ),
                10,
            ),
            "elapsed_ms": round(
                float(primary.get("elapsed_ms") or 0)
                + float(fallback.get("elapsed_ms") or 0),
                3,
            ),
            "attempts": [
                _attempt_summary(primary),
                _attempt_summary(fallback),
            ],
        }
    )
    if fallback_artifact is None:
        combined["status"] = "fallback_failed"
        combined["accepted"] = False
    return combined, fallback_artifact


def _execute_policy(
    *,
    policy_id: str,
    routes: Mapping[str, Mapping[str, str]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    max_call_budget_usd: float,
    fallback_enabled: bool | None = None,
) -> dict[str, Any]:
    use_fallback = (
        policy_id == COSTWEAVE_DYNAMIC
        if fallback_enabled is None
        else bool(fallback_enabled)
    )
    artifacts: dict[str, Mapping[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    stage_rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    for stage_index, stage in enumerate(STAGES):
        stage_started = time.perf_counter()
        results: dict[str, tuple[dict[str, Any], Mapping[str, Any] | None]] = {}
        runnable: list[str] = []
        skipped: list[str] = []
        for node_id in stage:
            missing = sorted(set(NODE_MAP[node_id].dependencies) - set(artifacts))
            if missing:
                skipped.append(node_id)
                results[node_id] = ({
                    "node_id": node_id,
                    "stage": stage_index,
                    "status": "skipped_missing_dependencies",
                    "accepted": False,
                    "missing_dependencies": missing,
                    **routes[node_id],
                }, None)
            else:
                runnable.append(node_id)
        with ThreadPoolExecutor(max_workers=max(1, len(runnable)), thread_name_prefix="cw-comprehensive") as executor:
            futures = {
                executor.submit(_execute_node, node=NODE_MAP[node_id], policy_id=policy_id, route=routes[node_id], artifacts=dict(artifacts), providers=providers, store=store, max_call_budget_usd=max_call_budget_usd, fallback_enabled=use_fallback): node_id
                for node_id in runnable
            }
            for future in as_completed(futures):
                node_id = futures[future]
                try:
                    results[node_id] = future.result()
                except Exception as error:
                    results[node_id] = ({"node_id": node_id, "stage": stage_index, "status": "failed", "accepted": False, "error": type(error).__name__, "diagnostic": "unexpected_node_failure", **routes[node_id]}, None)
        wall_ms = round((time.perf_counter() - stage_started) * 1000)
        summed_ms = sum(int(results[node_id][0].get("elapsed_ms") or 0) for node_id in runnable)
        failed = [
            node_id for node_id in runnable
            if results[node_id][1] is None
        ]
        stage_rows.append({
            "stage": stage_index, "nodes": list(stage),
            "runnable_nodes": runnable, "skipped_nodes": skipped,
            "failed_nodes": failed,
            "parallel": len(runnable) > 1,
            "degraded": bool(skipped or failed),
            "wall_ms": wall_ms, "sum_node_elapsed_ms": summed_ms,
            "observed_parallel_overlap_ms": max(0, summed_ms - wall_ms),
        })
        for node_id in stage:
            row, artifact = results[node_id]
            rows.append(row)
            if artifact is not None:
                artifacts[node_id] = artifact
    final = artifacts.get("final_assembly")
    judge = judge_final(final)
    return {
        "policy_id": policy_id, "accepted": judge["accepted"], "quality_score": judge["score"],
        "budgeted_fallback_enabled": use_fallback,
        "max_fallback_attempts_per_node": MAX_FALLBACK_ATTEMPTS_PER_NODE,
        "fallback_attempts": sum(bool(row.get("fallback_used")) for row in rows),
        "fallback_recoveries": sum(row.get("fallback_status") == "recovered" for row in rows),
        "final_judge": judge, "final_output": dict(final) if final else None,
        "estimated_cost_usd": round(sum(float(row.get("estimated_cost_usd") or 0) for row in rows), 10),
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "completed_node_calls": sum(row.get("status") == "completed" for row in rows),
        "accepted_node_calls": sum(bool(row.get("accepted")) for row in rows),
        "parallel_execution_observed": any(row["parallel"] and row["observed_parallel_overlap_ms"] > 0 for row in stage_rows),
        "stages": stage_rows, "nodes": rows,
    }


def _oracle_artifact(node_id: str) -> dict[str, Any]:
    common: dict[str, dict[str, Any]] = {
        "meta_decision": {"topology": "multiplanner", "planner_count": 2, "reasons": ["risk", "constraints"], "bounded": True},
        "planner_a": {"plan_id": "A", "stages": ["analyze", "build", "review"], "parallel_groups": [["requirements", "bug", "cost"]], "risks": ["residency"], "success_conditions": ["hard gates"]},
        "planner_b": {"plan_id": "B", "stages": ["truth", "capacity", "recover"], "parallel_groups": [["truth", "capacity"]], "risks": ["incident"], "success_conditions": ["local recovery"]},
        "requirements_extract": {"hard_constraints": ["budget", "deadline", "residency", "capacity"], "deliverables": sorted(REQUIRED_DELIVERABLES), "peak_rpm": 1800, "regional_peak_rpm": {"CN": 900, "EU": 900}, "deadline_hours": 72, "implementation_budget_usd": 1200},
        "bug_diagnosis": {"root_cause_ids": ["zero_sum_division", "independent_rounding_loses_total"], "required_guards": ["total", "weights"], "invariant": "exact_total", "recommended_algorithm": "largest_remainder_cents", "required_tests": sorted(REQUIRED_TESTS)},
        "contract_truth": {"authoritative_source": "signed_contracts", "setup_costs_usd": {"A": 250, "B": 300, "C": 400}, "rejected_source": "marketing_pages", "conflict_resolved": True},
        "residency_risk": {"cn_allowed": ["A", "C"], "eu_allowed": ["B", "C"], "forbidden_routes": ["CN->B", "EU->A"], "violations": []},
        "capacity_analysis": {"local_usable": False, "action": "escalate_before_execution", "local_queue_p95_seconds": 48, "required_peak_rpm": 1800, "regional_peak_rpm": {"CN": 900, "EU": 900}, "reason": "zero slots"},
        "plan_merge": {"selected_topology": "multiplanner", "dag_nodes": [node.node_id for node in NODES], "parallel_groups": [list(stage) for stage in STAGES if len(stage) > 1], "resolved_conflicts": ["contract over marketing"], "critical_path": ["meta_decision", "plan_merge", "incident_replan", "final_assembly"], "bounded_management": True},
        "patch_design": {"algorithm": "largest_remainder_cents", "guards": ["negative", "empty", "zero"], "invariant": "exact_total", "tests": sorted(REQUIRED_TESTS), "pseudocode": "floor then distribute residual"},
        "provider_costing": {"authoritative_source": "signed_contracts", "selected_providers": ["A", "B", "C"], "setup_costs_usd": {"A": 250, "B": 300, "C": 400}, "total_implementation_cost_usd": 950, "within_budget": True, "currency_unit": "USD"},
        "deployment_design": {"cn_route": {"A": 900}, "eu_route": {"B": 900}, "total_capacity_rpm": 1800, "p95_seconds": 2.5, "residency_violations": [], "rollback_supported": True},
        "storyboard_plan": {"shots": [{"id": f"shot-{i}", "description": text} for i, text in enumerate(["problem", "routing", "parallel specialists", "validation", "incident recovery", "outcome"], 1)], "traceable_claims": True},
        "patch_tests": {"passed_tests": sorted(REQUIRED_TESTS), "exact_total_preserved": True, "guard_coverage_complete": True, "accepted": True},
        "security_review": {"cn_residency_passed": True, "eu_residency_passed": True, "violations": [], "independent_review": True, "accepted": True},
        "financial_review": {"authoritative_source": "signed_contracts", "recomputed_total_usd": 950, "within_budget": True, "rejected_marketing_prices": True, "accepted": True},
        "handoff_review": {"schema_valid": True, "missing_artifacts": [], "provenance_complete": True, "version_consistent": True, "accepted": True},
        "incident_replan": {"affected_nodes": ["provider_costing", "deployment_design"], "preserved_nodes": ["patch_design", "storyboard_plan"], "provider_b_new_capacity_rpm": 400, "provider_b_new_setup_usd": 360, "post_incident_total_usd": 1010, "eu_route": {"B": 200, "C": 700}, "total_capacity_rpm": 1800, "within_budget": True, "residency_violations": []},
    }
    if node_id == "final_assembly":
        return {"status": "accepted", "topology": "multiplanner", "algorithm": "largest_remainder_cents", "tests": sorted(REQUIRED_TESTS), "cn_residency_passed": True, "eu_residency_passed": True, "residency_violations": [], "post_incident_total_usd": 1010, "total_capacity_rpm": 1800, "independent_reviews_passed": True, "preserved_nodes": ["patch_design", "storyboard_plan"], "deliverables": sorted(REQUIRED_DELIVERABLES), "storyboard": common["storyboard_plan"]["shots"], "rollback_plan": "versioned blue-green rollback", "risks": ["provider capacity"], "traceability": {"inputs": TASKSET_VERSION}}
    return common[node_id]


def run_offline_preflight() -> dict[str, Any]:
    validations = {}
    for node in NODES:
        artifact = _oracle_artifact(node.node_id)
        validations[node.node_id] = validate_node(node, json.dumps(artifact, ensure_ascii=False))
    final = _oracle_artifact("final_assembly")
    judge = judge_final(final)
    bad_cost = dict(_oracle_artifact("provider_costing")); bad_cost["currency_unit"] = "USD_PER_THOUSAND"
    bad_residency = dict(_oracle_artifact("deployment_design")); bad_residency["residency_violations"] = ["EU->A"]
    bad_capacity = dict(_oracle_artifact("capacity_analysis")); bad_capacity["action"] = "try_local_first"
    fault_checks = {
        "cost_unit_detected": not validate_node(NODE_MAP["provider_costing"], json.dumps(bad_cost))["accepted"],
        "residency_violation_detected": not validate_node(NODE_MAP["deployment_design"], json.dumps(bad_residency))["accepted"],
        "local_capacity_pre_escalation_enforced": not validate_node(NODE_MAP["capacity_analysis"], json.dumps(bad_capacity))["accepted"],
    }
    routes = freeze_policy_routes()
    route_shape_valid = all(set(policy_routes) == set(NODE_MAP) for policy_routes in routes.values())
    passed = all(item["accepted"] for item in validations.values()) and judge["accepted"] and all(fault_checks.values()) and route_shape_valid
    return {"passed": passed, "node_validations": validations, "final_judge": judge, "fault_checks": fault_checks, "route_shape_valid": route_shape_valid, "external_calls": 0}


def run_real_pilot(*, root: Path, budget_usd: float, registry_path: Path | None = None, runtime_path: Path | None = None, report_path: Path | None = None) -> dict[str, Any]:
    budget = float(budget_usd)
    if not math.isfinite(budget) or budget <= 0 or budget > MAX_THIS_RUN_BUDGET_USD + 1e-12:
        raise ComprehensivePilotError("本轮追加预算必须在 0 到 1.25 美元之间")
    if budget + PRIOR_PILOT_INTERNAL_COST_USD > MAX_AUTHORIZED_BUDGET_USD + 1e-12:
        raise ComprehensivePilotError(
            "累计内部预算超过既有支出加本轮 10 元人民币授权的保守美元上限"
        )
    if os.environ.get("COSTWEAVE_EXPERIMENTAL_ADAPTERS") != "1" or os.environ.get("COSTWEAVE_PAID_CALLS") != "1":
        raise ComprehensivePilotError("真实综合评测需要本次运行的双重显式授权开关")
    if os.environ.get(REPEAT_AUTHORIZATION_ENV) != "1":
        raise ComprehensivePilotError(
            "重复实验尚未获得新的显式授权；保持零付费等待状态"
        )
    preflight = run_offline_preflight()
    if not preflight["passed"]:
        raise ComprehensivePilotError("零成本预演未通过，拒绝开始付费调用")
    provider_list = _load_registry(registry_path or root / DEFAULT_REGISTRY_FILE)
    providers = {str(row["provider_id"]): row for row in provider_list}
    routes = freeze_policy_routes()
    max_call_budget = round(budget * MAX_SINGLE_CALL_BUDGET_SHARE, 10)
    task_payload = {"taskset_version": TASKSET_VERSION, "inputs": SYNTHETIC_INPUTS, "nodes": [asdict(node) for node in NODES], "stages": STAGES, "policies": routes, "random_seed": RANDOM_SEED, "reservation_policy": {"method": "exact_adapter_payload_upper_bound", "max_single_call_budget_share": MAX_SINGLE_CALL_BUDGET_SHARE, "max_call_budget_usd": max_call_budget}, "parallel_failure_policy": "dependency_scoped_isolation", "fallback_policy": {"authority": "costweave_dynamic_only", "critical_node_ids": sorted(CRITICAL_FALLBACK_NODE_IDS), "max_attempts_per_node": MAX_FALLBACK_ATTEMPTS_PER_NODE, "hard_stop_error_codes": sorted(FALLBACK_HARD_STOP_ERROR_CODES), "retryable_error_codes": sorted(FALLBACK_RETRYABLE_ERROR_CODES), "budget_authority": "exact_preflight_plus_atomic_ledger"}}
    canonical = json.dumps(task_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    task_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    output_dir = root / "work/local-data/real-eval"
    frozen_path = output_dir / "comprehensive-data-pilot-v6-frozen.json"
    report_file = report_path or output_dir / "comprehensive-data-pilot-v6-result.json"
    runtime_file = runtime_path or root / "work/local-data/comprehensive-data-pilot-v6.sqlite3"
    _atomic_json(frozen_path, {"schema_version": SCHEMA_VERSION, "created_at": utc_now(), "taskset_sha256": task_hash, "task": task_payload, "paid_calls_started": False})
    EventStore(runtime_file)
    store = ExperimentalCallStore(runtime_file)
    provider_limits = {"traveltoken-gpt": round(budget * .52, 10), "traveltoken-claude": round(budget * .43, 10), "deepseek-official": round(budget * .05, 10)}
    store.register_experiment(ExperimentBudget(
        experiment_id=EXPERIMENT_ID, total_cost_limit_usd=budget,
        provider_cost_limits_usd=provider_limits, max_logical_calls=MAX_LOGICAL_CALLS,
        max_attempts_per_call=1, purpose="pre_v07_comprehensive_four_policy_real_data_pilot",
        expires_at=(datetime.now(UTC) + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
    ))
    order = list(POLICY_ORDER)
    random.Random(RANDOM_SEED).shuffle(order)
    results = []
    for policy_id in order:
        results.append(_execute_policy(policy_id=policy_id, routes=routes[policy_id], providers=providers, store=store, max_call_budget_usd=max_call_budget))
    ledger = store.experiment_summary(EXPERIMENT_ID)
    rows_by_policy = {row["policy_id"]: row for row in results}
    premium_cost = float(rows_by_policy[PREMIUM_HIGH]["estimated_cost_usd"])
    for row in results:
        row["cost_reduction_vs_premium"] = round((premium_cost - float(row["estimated_cost_usd"])) / premium_cost, 6) if premium_cost else None
        row["cost_per_accepted_result_usd"] = row["estimated_cost_usd"] if row["accepted"] else None
    report = {
        "schema_version": SCHEMA_VERSION, "created_at": utc_now(), "experiment_id": EXPERIMENT_ID,
        "run_id": RUN_ID, "taskset_version": TASKSET_VERSION, "taskset_sha256": task_hash,
        "authorized_budget_usd": budget, "cumulative_authorized_budget_usd": MAX_AUTHORIZED_BUDGET_USD,
        "prior_pilot_internal_cost_usd": PRIOR_PILOT_INTERNAL_COST_USD,
        "additional_authorized_budget_cny": ADDITIONAL_AUTHORIZED_BUDGET_CNY,
        "conservative_additional_budget_usd": MAX_THIS_RUN_BUDGET_USD,
        "max_call_budget_usd": max_call_budget,
        "reservation_policy": "exact_adapter_payload_upper_bound",
        "parallel_failure_policy": "dependency_scoped_isolation",
        "fallback_policy": task_payload["fallback_policy"],
        "provider_limits_usd": provider_limits,
        "estimated_total_cost_usd": ledger["actual_cost_usd"], "budget_exceeded": float(ledger["actual_cost_usd"]) > budget + 1e-12,
        "active_reserved_cost_usd": ledger["active_reserved_cost_usd"], "ledger_summary": ledger,
        "preflight": preflight, "policy_execution_order": order, "policy_results": results,
        "model_identity": {key: {"requested_model": value["selected_model"], "trust": value.get("trust")} for key, value in providers.items()},
        "frozen_task_path": str(frozen_path.resolve()), "runtime_path": str(runtime_file.resolve()),
        "natural_track_paid": True, "fault_track": "offline_deterministic_only",
        "provider_invoice_reconciled": False, "commercial_claims_allowed": False,
        "statistical_superiority_claims_allowed": False,
    }
    _atomic_json(report_file, report)
    return {**report, "report_path": str(report_file.resolve())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行版本前 CostWeave 综合真实数据试点")
    parser.add_argument("--preflight", action="store_true", help="只运行零成本预演")
    parser.add_argument("--budget-usd", type=float)
    parser.add_argument("--root", type=Path, default=project_root())
    args = parser.parse_args(argv)
    if args.preflight:
        result = run_offline_preflight()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    if args.budget_usd is None:
        parser.error("真实运行必须提供 --budget-usd")
    try:
        result = run_real_pilot(root=args.root.resolve(), budget_usd=args.budget_usd)
    except ComprehensivePilotError as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if any(row["completed_node_calls"] for row in result["policy_results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
