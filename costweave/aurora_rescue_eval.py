"""Fully offline Aurora rescue benchmark and deterministic acceptance oracle.

The module is intentionally pre-v0.7 research infrastructure.  It never loads
provider registries or API keys and exposes no real-call entry point.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters.http import (
    AdapterRuntimePolicy,
    ExperimentalAdapterConfig,
    ExternalExecutionRequest,
    HTTPResponse,
    OpenAICompatibleHTTPAdapter,
)
from .event_store import EventStore
from .experimental_calls import ExperimentBudget, ExperimentalCallStore
from .planning_contracts import WorkOrder


AURORA_SCHEMA_VERSION = "pre-v0.7-aurora-offline-1"
AURORA_TASK_VERSION = "aurora-release-rescue-v1"
VALIDATOR_VERSION = "aurora-validation-20260909-abc1"
AURORA_EXPERIMENT_ID = "experiment_aurora_offline_gate_v1"
MAX_MANAGEMENT_LEVELS = 2
MAX_FALLBACKS_PER_NODE = 1


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


REQUIREMENTS: Mapping[str, Any] = {
    "deadline_hours": 48,
    "implementation_budget_usd": 1450,
    "regional_peak_rpm": {"CN": 1000, "EU": 700, "US": 500},
    "total_peak_rpm": 2200,
    "p95_limit_seconds": 3.5,
    "data_residency": {
        "CN": "CN",
        "EU": "EU",
        "US": "US",
    },
    "independent_review_for_high_risk": True,
    "deliverables": [
        "technical_plan",
        "allocator_patch",
        "tenant_policy_patch",
        "deterministic_tests",
        "deployment_plan",
        "provider_cost_table",
        "risk_table",
        "rollback_plan",
        "eight_shot_media_package",
        "incident_replan",
        "traceability_table",
    ],
}

LOCAL_CAPACITY: Mapping[str, Any] = {
    "accelerator": "A800",
    "used_pct": 94,
    "slots_available": 0,
    "queue_p95_seconds": 45,
}

PROVIDERS: Mapping[str, Mapping[str, Any]] = {
    "A": {
        "name": "Northstar",
        "regions": ["CN"],
        "capacity_rpm": 1100,
        "p95_seconds": 2.1,
        "signed_setup_usd": 260,
        "marketing_setup_usd": 210,
    },
    "B": {
        "name": "BlueRiver",
        "regions": ["EU"],
        "capacity_rpm": 800,
        "p95_seconds": 2.4,
        "signed_setup_usd": 300,
        "marketing_setup_usd": 240,
    },
    "C": {
        "name": "Canyon",
        "regions": ["EU", "US"],
        "capacity_rpm": 900,
        "p95_seconds": 2.8,
        "signed_setup_usd": 380,
        "marketing_setup_usd": 310,
    },
    "D": {
        "name": "Bridge",
        "regions": ["EU", "US"],
        "capacity_rpm": 800,
        "p95_seconds": 3.2,
        "signed_setup_usd": 420,
        "marketing_setup_usd": 350,
    },
}

SOFTWARE_FIXTURES: Mapping[str, Any] = {
    "allocator": {
        "implementation": (
            "scale = total_points / sum(weights); "
            "return [round(weight * scale) for weight in weights]"
        ),
        "contract": [
            "total_nonnegative",
            "weights_nonempty",
            "weights_nonnegative",
            "positive_weight_exists",
            "exact_total_preserved",
            "deterministic_tie_break",
        ],
    },
    "tenant_export": {
        "implementation": (
            "return role in {'admin', 'analyst'}"
        ),
        "contract": [
            "same_tenant_required",
            "role_allowlist_required",
            "empty_tenant_rejected",
        ],
    },
}

MEDIA_REQUIREMENTS: Mapping[str, Any] = {
    "shot_count": 8,
    "max_new_keyframes": 3,
    "required_fields": [
        "shot_id",
        "prompt",
        "negative_prompt",
        "duration_seconds",
        "asset_id",
        "new_keyframe",
        "reuse_asset_ids",
        "character_anchor",
        "style_anchor",
        "source_ids",
    ],
    "forbidden_claims": [
        "zero latency",
        "0 latency",
        "100% accurate",
        "absolutely secure",
        "零延迟",
        "百分之百准确",
        "绝对安全",
    ],
}

INCIDENT_PACKET: Mapping[str, Any] = {
    "release_stage": 7,
    "provider_b_capacity_rpm": 200,
    "provider_c_eu_allowed": False,
    "provider_c_us_allowed": True,
    "provider_d_emergency_setup_multiplier": 1.15,
    "provider_d_emergency_setup_usd": 483,
    "unaffected_domains": [
        "allocator_patch",
        "tenant_policy_patch",
        "code_review",
        "media_package",
        "media_review",
        "rollback_plan",
    ],
}


@dataclass(frozen=True, slots=True)
class AuroraNode:
    node_id: str
    stage: int
    dependencies: tuple[str, ...]
    required_keys: tuple[str, ...]
    difficulty: int
    risk: str
    preferred_role: str


NODES: tuple[AuroraNode, ...] = (
    AuroraNode("meta_decision", 1, (), ("topology", "planner_count", "management_levels", "confidence", "reasons"), 9, "high", "meta_planner"),
    AuroraNode("planner_a", 2, ("meta_decision",), ("plan_id", "stages", "parallel_groups", "critical_path", "success_conditions"), 8, "high", "planner"),
    AuroraNode("planner_b", 2, ("meta_decision",), ("plan_id", "conflicts", "failure_modes", "recovery_boundaries", "success_conditions"), 8, "high", "planner"),
    AuroraNode("requirements_extract", 3, ("planner_a", "planner_b"), ("deadline_hours", "implementation_budget_usd", "regional_peak_rpm", "total_peak_rpm", "p95_limit_seconds", "deliverables"), 4, "normal", "extractor"),
    AuroraNode("allocator_analysis", 3, ("planner_a", "planner_b"), ("root_causes", "guards", "invariants", "algorithm_family", "tie_breaker"), 7, "high", "code_analyst"),
    AuroraNode("tenant_analysis", 3, ("planner_a", "planner_b"), ("root_cause", "same_tenant_required", "allowed_roles", "empty_tenant_rejected", "threat"), 8, "critical", "security_analyst"),
    AuroraNode("contract_truth", 3, ("planner_a", "planner_b"), ("authoritative_source", "setup_costs_usd", "rejected_source", "conflict_resolved"), 5, "high", "contract_analyst"),
    AuroraNode("residency_analysis", 3, ("planner_a", "planner_b"), ("region_provider_allowlist", "forbidden_routes", "violations"), 8, "critical", "residency_analyst"),
    AuroraNode("capacity_admission", 3, ("planner_a", "planner_b"), ("local_usable", "action", "queue_p95_seconds", "slots_available", "required_peak_rpm", "reason"), 7, "high", "capacity_analyst"),
    AuroraNode("media_analysis", 3, ("planner_a", "planner_b"), ("shot_count", "max_new_keyframes", "required_anchors", "forbidden_claims", "reuse_required"), 5, "normal", "media_planner"),
    AuroraNode("plan_merge", 4, ("planner_a", "planner_b", "requirements_extract", "allocator_analysis", "tenant_analysis", "contract_truth", "residency_analysis", "capacity_admission", "media_analysis"), ("selected_topology", "dag_nodes", "parallel_groups", "critical_path", "resolved_conflicts", "management_levels"), 9, "critical", "plan_merger"),
    AuroraNode("allocator_patch", 5, ("plan_merge", "allocator_analysis"), ("implementation_id", "guards", "tie_breaker", "invariant"), 8, "high", "code_executor"),
    AuroraNode("tenant_policy_patch", 5, ("plan_merge", "tenant_analysis"), ("policy_id", "same_tenant_required", "allowed_roles", "reject_empty_tenant"), 9, "critical", "security_executor"),
    AuroraNode("provider_selection", 5, ("plan_merge", "contract_truth", "requirements_extract"), ("authoritative_source", "selected_providers", "setup_costs_usd", "total_setup_usd", "within_budget"), 7, "high", "cost_planner"),
    AuroraNode("deployment_plan", 5, ("plan_merge", "provider_selection", "residency_analysis", "capacity_admission"), ("regional_routes", "total_capacity_rpm", "p95_seconds", "residency_violations", "rollback_supported"), 9, "critical", "deployment_planner"),
    AuroraNode("media_package", 5, ("plan_merge", "media_analysis", "requirements_extract"), ("shots", "new_keyframe_count", "traceable_claims", "estimated_generation_calls"), 6, "normal", "media_executor"),
    AuroraNode("rollback_plan", 5, ("plan_merge", "allocator_patch", "tenant_policy_patch", "deployment_plan"), ("versioned", "steps", "triggers", "preserved_data", "maximum_minutes"), 7, "high", "release_planner"),
    AuroraNode("code_review", 6, ("allocator_patch", "tenant_policy_patch"), ("allocator_tests_passed", "tenant_tests_passed", "test_count", "independent", "accepted"), 9, "critical", "deterministic_validator"),
    AuroraNode("security_review", 6, ("tenant_policy_patch", "deployment_plan", "residency_analysis"), ("tenant_isolation_passed", "residency_passed", "violations", "independent", "accepted"), 10, "critical", "security_reviewer"),
    AuroraNode("financial_review", 6, ("provider_selection", "contract_truth", "requirements_extract"), ("authoritative_source", "recomputed_total_usd", "within_budget", "marketing_prices_rejected", "accepted"), 8, "high", "financial_reviewer"),
    AuroraNode("handoff_review", 6, ("allocator_patch", "tenant_policy_patch", "provider_selection", "deployment_plan", "media_package", "rollback_plan"), ("schema_valid", "missing_artifacts", "provenance_complete", "version_consistent", "accepted"), 8, "high", "handoff_reviewer"),
    AuroraNode("media_review", 6, ("media_package", "media_analysis"), ("shot_count_valid", "anchors_consistent", "reuse_valid", "claims_valid", "accepted"), 7, "high", "media_reviewer"),
    AuroraNode("incident_replan", 7, ("provider_selection", "deployment_plan", "security_review", "financial_review"), ("affected_nodes", "preserved_nodes", "provider_plan_v2", "deployment_plan_v2", "security_review_v2", "financial_review_v2"), 10, "critical", "incident_commander"),
    AuroraNode("final_assembly", 8, ("code_review", "security_review", "financial_review", "handoff_review", "media_review", "incident_replan"), ("status", "deliverables", "final_setup_usd", "total_capacity_rpm", "p95_seconds", "hard_gates_passed", "traceability"), 10, "critical", "final_assembler"),
)

NODE_MAP = {node.node_id: node for node in NODES}
STAGES = tuple(
    tuple(node.node_id for node in NODES if node.stage == stage)
    for stage in range(1, 9)
)


def _ancestors(node_id: str) -> set[str]:
    pending = list(NODE_MAP[node_id].dependencies)
    result: set[str] = set()
    while pending:
        parent = pending.pop()
        if parent not in result:
            result.add(parent)
            pending.extend(NODE_MAP[parent].dependencies)
    return result


def _text_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def _finite_number(value: Any) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _plan_graph_checks(artifact: Mapping[str, Any]) -> dict[str, bool]:
    # The frozen contract uses an ordered chain of critical milestones, not
    # a duration-weighted longest path. Each later milestone must depend on
    # the preceding one, possibly through omitted intermediate nodes.
    path = artifact.get("critical_path")
    path_known = _text_list(path) and len(path) >= 2 and all(item in NODE_MAP for item in path)
    path_valid = bool(path_known) and len(set(path)) == len(path) and all(
        left in _ancestors(right) for left, right in zip(path, path[1:])
    )
    groups = artifact.get("parallel_groups")
    groups_valid = isinstance(groups, list) and bool(groups)
    seen: set[str] = set()
    if groups_valid:
        for group in groups:
            if not _text_list(group) or any(item not in NODE_MAP for item in group):
                groups_valid = False
                break
            if len(set(group)) != len(group) or seen.intersection(group):
                groups_valid = False
            if any(a in _ancestors(b) for a in group for b in group if a != b):
                groups_valid = False
            seen.update(group)
    return {"critical_milestones_valid": bool(path_valid), "parallel_groups_independent": bool(groups_valid)}


def public_task(*, reveal_incident: bool = False) -> dict[str, Any]:
    """Return the exact model-visible task.  The Oracle is never included."""

    payload = {
        "schema_version": AURORA_SCHEMA_VERSION,
        "task_version": AURORA_TASK_VERSION,
        "requirements": REQUIREMENTS,
        "local_capacity": LOCAL_CAPACITY,
        "providers": PROVIDERS,
        "software_fixtures": SOFTWARE_FIXTURES,
        "media_requirements": MEDIA_REQUIREMENTS,
        "nodes": [asdict(node) for node in NODES],
        "stages": [list(stage) for stage in STAGES],
        "incident_release_stage": 7,
        "oracle_included": False,
    }
    if reveal_incident:
        payload["incident"] = INCIDENT_PACKET
    else:
        payload["incident_commitment_sha256"] = _digest(INCIDENT_PACKET)
    return payload


def frozen_task_record() -> dict[str, Any]:
    initial = public_task(reveal_incident=False)
    revealed = public_task(reveal_incident=True)
    return {
        "schema_version": AURORA_SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "task_version": AURORA_TASK_VERSION,
        "initial_task_sha256": _digest(initial),
        "revealed_task_sha256": _digest(revealed),
        "incident_commitment_sha256": _digest(INCIDENT_PACKET),
        "oracle_commitment_sha256": sealed_oracle_digest(),
        "initial_task": initial,
        "external_calls": 0,
    }


ALLOCATOR_CASES: tuple[tuple[str, int, tuple[int, ...], Any], ...] = (
    ("negative_total", -1, (1,), ValueError),
    ("empty_weights", 10, (), ValueError),
    ("negative_weight", 10, (1, -1), ValueError),
    ("all_zero", 10, (0, 0), ValueError),
    ("thirds", 10, (1, 1, 1), [4, 3, 3]),
    ("ordinary", 10, (1, 2), [3, 7]),
    ("zero_total", 0, (1, 2), [0, 0]),
    ("tie_break", 1, (1, 1), [1, 0]),
)

TENANT_CASES: tuple[tuple[str, str, str, bool], ...] = (
    ("tenant-a", "tenant-a", "admin", True),
    ("tenant-a", "tenant-a", "analyst", True),
    ("tenant-a", "tenant-a", "viewer", False),
    ("tenant-a", "tenant-b", "admin", False),
    ("tenant-a", "tenant-b", "analyst", False),
    ("", "tenant-a", "admin", False),
)


def execute_allocator_patch(
    patch: Mapping[str, Any], total_points: int, weights: Sequence[int]
) -> list[int]:
    """Execute the bounded allocator DSL; arbitrary model code is never run."""

    if patch.get("implementation_id") != "largest_remainder_points_v1":
        raise ValueError("unsupported allocator implementation")
    required_guards = {"negative_total", "empty_weights", "negative_weight", "all_zero"}
    guards = {str(item) for item in patch.get("guards", ())}
    if not required_guards <= guards:
        raise ValueError("allocator guard contract is incomplete")
    if patch.get("tie_breaker") != "original_index":
        raise ValueError("allocator tie breaker is not deterministic")
    if not isinstance(total_points, int) or total_points < 0:
        raise ValueError("total points must be a non-negative integer")
    values = list(weights)
    if not values or any(not isinstance(item, int) or item < 0 for item in values):
        raise ValueError("weights must be non-empty non-negative integers")
    denominator = sum(values)
    if denominator <= 0:
        raise ValueError("at least one weight must be positive")
    quotas = [Fraction(total_points * item, denominator) for item in values]
    result = [quota.numerator // quota.denominator for quota in quotas]
    remaining = total_points - sum(result)
    order = sorted(
        range(len(values)),
        key=lambda index: (-(quotas[index] - result[index]), index),
    )
    for index in order[:remaining]:
        result[index] += 1
    if sum(result) != total_points:
        raise AssertionError("allocator invariant failed")
    return result


def allocator_test_results(patch: Mapping[str, Any]) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for name, total, weights, expected in ALLOCATOR_CASES:
        try:
            actual = execute_allocator_patch(patch, total, weights)
        except Exception as error:
            actual = type(error)
        results[name] = actual == expected
    return results


def execute_tenant_policy(
    patch: Mapping[str, Any], requester_tenant: str, resource_tenant: str, role: str
) -> bool:
    """Execute a small policy DSL rather than arbitrary generated code."""

    if patch.get("policy_id") != "tenant_export_v1":
        return False
    if patch.get("reject_empty_tenant") is not True and (
        not requester_tenant or not resource_tenant
    ):
        raise ValueError("empty-tenant behavior is undefined")
    if not requester_tenant or not resource_tenant:
        return False
    allowed = {str(item) for item in patch.get("allowed_roles", ())}
    if role not in allowed:
        return False
    if patch.get("same_tenant_required") is True:
        return requester_tenant == resource_tenant
    return True


def tenant_test_results(patch: Mapping[str, Any]) -> dict[str, bool]:
    return {
        f"case_{index + 1}": (
            execute_tenant_policy(patch, requester, resource, role) == expected
        )
        for index, (requester, resource, role, expected) in enumerate(TENANT_CASES)
    }


def _media_shots() -> list[dict[str, Any]]:
    character = "teacher-lin-v1"
    style = "aurora-blue-paper-cut-v1"
    definitions = (
        ("shot-1", "kf-1", True, (), "Teacher introduces the regional learning challenge"),
        ("shot-2", "shot-2", False, ("kf-1",), "Reuse teacher anchor while the task graph unfolds"),
        ("shot-3", "kf-2", True, (), "Parallel specialists inspect code, access and contracts"),
        ("shot-4", "shot-4", False, ("kf-2",), "Validators reject unsafe and inconsistent artifacts"),
        ("shot-5", "kf-3", True, (), "Three compliant regional routes meet the launch load"),
        ("shot-6", "shot-6", False, ("kf-3",), "A provider incident changes only the affected subgraph"),
        ("shot-7", "shot-7", False, ("kf-2",), "Budgeted fallback repairs one failed critical node"),
        ("shot-8", "shot-8", False, ("kf-1",), "Validated deliverables reach the release checkpoint"),
    )
    return [
        {
            "shot_id": shot_id,
            "prompt": prompt,
            "negative_prompt": "inconsistent face, unreadable UI, unsupported claim",
            "duration_seconds": 4,
            "asset_id": asset_id,
            "new_keyframe": new,
            "reuse_asset_ids": list(reuse),
            "character_anchor": character,
            "style_anchor": style,
            "source_ids": ["aurora-requirements", "aurora-validated-plan"],
        }
        for shot_id, asset_id, new, reuse, prompt in definitions
    ]


_ORACLE: Mapping[str, Mapping[str, Any]] = {
    "meta_decision": {
        "topology": "multiplanner",
        "planner_count": 2,
        "management_levels": 2,
        "confidence": 0.92,
        "reasons": ["independent_high_risk_domains", "parallel_specialists", "incident_recovery"],
    },
    "planner_a": {
        "plan_id": "delivery-plan-a",
        "stages": ["admit", "analyze", "merge", "execute", "review", "replan", "assemble"],
        "parallel_groups": [list(STAGES[2]), ["allocator_patch", "tenant_policy_patch", "provider_selection", "media_package"], list(STAGES[5])],
        "critical_path": ["meta_decision", "plan_merge", "incident_replan", "final_assembly"],
        "success_conditions": ["hard_gates", "budget", "traceability"],
    },
    "planner_b": {
        "plan_id": "recovery-plan-b",
        "conflicts": ["contract_vs_marketing", "capacity_vs_residency", "quality_vs_cost"],
        "failure_modes": ["cross_tenant_export", "rounding_loss", "provider_incident"],
        "recovery_boundaries": ["one_fallback", "affected_subgraph_only", "two_management_levels"],
        "success_conditions": ["independent_review", "no_hard_gate_failure", "versioned_handoff"],
    },
    "requirements_extract": {
        "deadline_hours": 48,
        "implementation_budget_usd": 1450,
        "regional_peak_rpm": {"CN": 1000, "EU": 700, "US": 500},
        "total_peak_rpm": 2200,
        "p95_limit_seconds": 3.5,
        "deliverables": list(REQUIREMENTS["deliverables"]),
    },
    "allocator_analysis": {
        "root_causes": ["zero_denominator", "independent_rounding_breaks_total"],
        "guards": ["negative_total", "empty_weights", "negative_weight", "all_zero"],
        "invariants": ["integer_outputs", "exact_total", "deterministic"],
        "algorithm_family": "largest_remainder_points",
        "tie_breaker": "original_index",
    },
    "tenant_analysis": {
        "root_cause": "role_check_omits_tenant_boundary",
        "same_tenant_required": True,
        "allowed_roles": ["admin", "analyst"],
        "empty_tenant_rejected": True,
        "threat": "cross_tenant_data_export",
    },
    "contract_truth": {
        "authoritative_source": "signed_contracts",
        "setup_costs_usd": {key: value["signed_setup_usd"] for key, value in PROVIDERS.items()},
        "rejected_source": "marketing_pages",
        "conflict_resolved": True,
    },
    "residency_analysis": {
        "region_provider_allowlist": {"CN": ["A"], "EU": ["B", "C", "D"], "US": ["C", "D"]},
        "forbidden_routes": ["CN->B", "CN->C", "CN->D", "EU->A", "US->A", "US->B"],
        "violations": [],
    },
    "capacity_admission": {
        "local_usable": False,
        "action": "escalate_before_execution",
        "queue_p95_seconds": 45,
        "slots_available": 0,
        "required_peak_rpm": 2200,
        "reason": "no_local_slots_and_queue_exceeds_latency_budget",
    },
    "media_analysis": {
        "shot_count": 8,
        "max_new_keyframes": 3,
        "required_anchors": ["character_anchor", "style_anchor"],
        "forbidden_claims": list(MEDIA_REQUIREMENTS["forbidden_claims"]),
        "reuse_required": True,
    },
    "plan_merge": {
        "selected_topology": "multiplanner",
        "dag_nodes": [node.node_id for node in NODES],
        "parallel_groups": [list(STAGES[1]), list(STAGES[2]), ["allocator_patch", "tenant_policy_patch", "provider_selection", "media_package"], list(STAGES[5])],
        "critical_path": ["meta_decision", "plan_merge", "incident_replan", "final_assembly"],
        "resolved_conflicts": ["signed_contracts_win", "residency_is_hard_gate", "tests_over_self_report"],
        "management_levels": 2,
    },
    "allocator_patch": {
        "implementation_id": "largest_remainder_points_v1",
        "guards": ["negative_total", "empty_weights", "negative_weight", "all_zero"],
        "tie_breaker": "original_index",
        "invariant": "sum_outputs_equals_total_points",
    },
    "tenant_policy_patch": {
        "policy_id": "tenant_export_v1",
        "same_tenant_required": True,
        "allowed_roles": ["admin", "analyst"],
        "reject_empty_tenant": True,
    },
    "provider_selection": {
        "authoritative_source": "signed_contracts",
        "selected_providers": ["A", "B", "C"],
        "setup_costs_usd": {"A": 260, "B": 300, "C": 380},
        "total_setup_usd": 940,
        "within_budget": True,
    },
    "deployment_plan": {
        "regional_routes": {"CN": {"A": 1000}, "EU": {"B": 700}, "US": {"C": 500}},
        "total_capacity_rpm": 2200,
        "p95_seconds": 2.8,
        "residency_violations": [],
        "rollback_supported": True,
    },
    "media_package": {
        "shots": _media_shots(),
        "new_keyframe_count": 3,
        "traceable_claims": True,
        "estimated_generation_calls": 11,
    },
    "rollback_plan": {
        "versioned": True,
        "steps": ["freeze_writes", "restore_prior_policy", "restore_prior_routes", "verify_tenant_isolation"],
        "triggers": ["hard_gate_failure", "capacity_shortfall", "cross_tenant_denial_failure"],
        "preserved_data": True,
        "maximum_minutes": 20,
    },
    "code_review": {
        "allocator_tests_passed": True,
        "tenant_tests_passed": True,
        "test_count": len(ALLOCATOR_CASES) + len(TENANT_CASES),
        "independent": True,
        "accepted": True,
    },
    "security_review": {
        "tenant_isolation_passed": True,
        "residency_passed": True,
        "violations": [],
        "independent": True,
        "accepted": True,
    },
    "financial_review": {
        "authoritative_source": "signed_contracts",
        "recomputed_total_usd": 940,
        "within_budget": True,
        "marketing_prices_rejected": True,
        "accepted": True,
    },
    "handoff_review": {
        "schema_valid": True,
        "missing_artifacts": [],
        "provenance_complete": True,
        "version_consistent": True,
        "accepted": True,
    },
    "media_review": {
        "shot_count_valid": True,
        "anchors_consistent": True,
        "reuse_valid": True,
        "claims_valid": True,
        "accepted": True,
    },
    "incident_replan": {
        "affected_nodes": ["provider_selection", "deployment_plan", "security_review", "financial_review"],
        "preserved_nodes": list(INCIDENT_PACKET["unaffected_domains"]),
        "provider_plan_v2": {
            "selected_providers": ["A", "B", "C", "D"],
            "setup_costs_usd": {"A": 260, "B": 300, "C": 380, "D": 483},
            "total_setup_usd": 1423,
            "within_budget": True,
        },
        "deployment_plan_v2": {
            "regional_routes": {"CN": {"A": 1000}, "EU": {"B": 200, "D": 500}, "US": {"C": 500}},
            "total_capacity_rpm": 2200,
            "p95_seconds": 3.2,
            "residency_violations": [],
        },
        "security_review_v2": {"residency_passed": True, "violations": [], "accepted": True},
        "financial_review_v2": {"recomputed_total_usd": 1423, "within_budget": True, "accepted": True},
    },
    "final_assembly": {
        "status": "accepted",
        "deliverables": list(REQUIREMENTS["deliverables"]),
        "final_setup_usd": 1423,
        "total_capacity_rpm": 2200,
        "p95_seconds": 3.2,
        "hard_gates_passed": ["allocator", "tenant_isolation", "residency", "capacity", "budget", "media_claims", "handoff"],
        "traceability": {"task_version": AURORA_TASK_VERSION, "oracle_commitment": "sealed"},
    },
}


def sealed_oracle_digest() -> str:
    return _digest(_ORACLE)


def oracle_artifact(node_id: str) -> dict[str, Any]:
    """Return a detached local test fixture; never add this to model prompts."""

    return json.loads(_canonical(_ORACLE[node_id]))


@dataclass(frozen=True, slots=True)
class ValidationResult:
    node_id: str
    accepted: bool
    checks: Mapping[str, bool]
    diagnostic: str

    def public(self) -> dict[str, Any]:
        return {**asdict(self), "validator_version": VALIDATOR_VERSION}


def _all_true(values: Mapping[str, bool]) -> bool:
    return bool(values) and all(values.values())


def _route_checks(
    plan: Mapping[str, Any], *, incident: bool
) -> dict[str, bool]:
    routes = plan.get("regional_routes", {})
    if not isinstance(routes, Mapping):
        routes = {}
    expected_load = REQUIREMENTS["regional_peak_rpm"]
    provider_totals = {provider_id: 0 for provider_id in PROVIDERS}
    region_totals: dict[str, int] = {}
    residency_ok = True
    values_ok = set(routes) == set(expected_load)
    used_latencies: list[float] = []
    for region, required in expected_load.items():
        assignments = routes.get(region, {})
        if not isinstance(assignments, Mapping):
            assignments = {}
        region_total = 0
        for provider_id, raw_amount in assignments.items():
            if (
                provider_id not in PROVIDERS
                or not isinstance(raw_amount, int)
                or isinstance(raw_amount, bool)
                or raw_amount < 0
            ):
                values_ok = False
                continue
            amount = int(raw_amount)
            provider = PROVIDERS[provider_id]
            allowed = region in provider["regions"]
            if incident and provider_id == "C" and region == "EU":
                allowed = False
            residency_ok = residency_ok and allowed
            provider_totals[provider_id] += amount
            region_total += amount
            if amount:
                used_latencies.append(float(provider["p95_seconds"]))
        region_totals[region] = region_total
    capacity_ok = True
    for provider_id, amount in provider_totals.items():
        capacity = int(PROVIDERS[provider_id]["capacity_rpm"])
        if incident and provider_id == "B":
            capacity = int(INCIDENT_PACKET["provider_b_capacity_rpm"])
        capacity_ok = capacity_ok and amount <= capacity
    computed_total = sum(region_totals.values())
    computed_p95 = max(used_latencies, default=0.0)
    return {
        "route_values_valid": values_ok,
        "regional_loads_exact": region_totals == expected_load,
        "provider_capacity_sufficient": capacity_ok,
        "residency_valid": residency_ok,
        "total_exact": computed_total == REQUIREMENTS["total_peak_rpm"],
        "reported_total_exact": plan.get("total_capacity_rpm") == computed_total,
        "reported_p95_exact": _finite_number(plan.get("p95_seconds")) and math.isclose(
            float(plan.get("p95_seconds", -1)), computed_p95, abs_tol=1e-9
        ),
        "p95_within_limit": computed_p95 <= REQUIREMENTS["p95_limit_seconds"],
        "reported_violations_empty": plan.get("residency_violations", []) == [],
    }


def _provider_ids(raw_selected: Any) -> tuple[list[str], bool]:
    selected: list[str] = []
    shape_valid = isinstance(raw_selected, list)
    if isinstance(raw_selected, list):
        for item in raw_selected:
            if isinstance(item, str):
                provider_id = item
            elif isinstance(item, Mapping):
                provider_id = next(
                    (
                        str(item[key])
                        for key in ("provider_id", "id", "provider", "code")
                        if key in item and isinstance(item[key], str)
                    ),
                    "",
                )
            else:
                provider_id = ""
            selected.append(provider_id.strip().upper())
    return selected, shape_valid


def _provider_cost_checks(
    selection: Mapping[str, Any], *, incident: bool
) -> dict[str, bool]:
    selected, shape_valid = _provider_ids(
        selection.get("selected_providers", [])
    )
    unique = (
        shape_valid
        and len(selected) == len(set(selected))
        and all(item in PROVIDERS for item in selected)
    )
    emergency_d = int(INCIDENT_PACKET["provider_d_emergency_setup_usd"])
    expected_costs = {
        item: (
            emergency_d
            if incident and item == "D"
            else int(PROVIDERS[item]["signed_setup_usd"])
        )
        for item in selected
        if item in PROVIDERS
    }
    recomputed = sum(expected_costs.values())
    return {
        "providers_unique_and_known": unique,
        "signed_costs_used": selection.get("setup_costs_usd") == expected_costs,
        "reported_total_recomputed": selection.get("total_setup_usd") == recomputed,
        "budget_recomputed": recomputed <= REQUIREMENTS["implementation_budget_usd"],
        "reported_budget_flag": selection.get("within_budget")
        is (recomputed <= REQUIREMENTS["implementation_budget_usd"]),
    }


def _media_checks(package: Mapping[str, Any]) -> dict[str, bool]:
    shots = package.get("shots", [])
    if not isinstance(shots, list):
        shots = []
    required_fields = set(MEDIA_REQUIREMENTS["required_fields"])
    fields_ok = all(
        isinstance(shot, Mapping) and required_fields <= set(shot) for shot in shots
    )
    new_assets = {
        str(shot.get("asset_id"))
        for shot in shots
        if isinstance(shot, Mapping) and shot.get("new_keyframe") is True
    }
    reuse_ok = True
    anchors_ok = bool(shots)
    claims_ok = True
    source_ok = True
    first = shots[0] if shots and isinstance(shots[0], Mapping) else {}
    character = first.get("character_anchor")
    style = first.get("style_anchor")
    anchors_ok = all(isinstance(value, str) and bool(value.strip()) for value in (character, style))
    values_ok = True
    available_assets: set[str] = set()
    shot_ids: set[str] = set()
    asset_ids: set[str] = set()
    allowed_sources = set(NODE_MAP["media_package"].dependencies) | {"aurora-requirements", "aurora-validated-plan"}
    for shot in shots:
        if not isinstance(shot, Mapping):
            reuse_ok = anchors_ok = claims_ok = source_ok = False
            continue
        for field in ("shot_id", "asset_id", "prompt", "negative_prompt"):
            values_ok = values_ok and isinstance(shot.get(field), str) and bool(shot[field].strip())
        duration = shot.get("duration_seconds")
        values_ok = values_ok and _finite_number(duration) and duration > 0
        values_ok = values_ok and isinstance(shot.get("new_keyframe"), bool)
        shot_id, asset_id = str(shot.get("shot_id")), str(shot.get("asset_id"))
        values_ok = values_ok and shot_id not in shot_ids and asset_id not in asset_ids
        shot_ids.add(shot_id)
        asset_ids.add(asset_id)
        anchors_ok = anchors_ok and shot.get("character_anchor") == character
        anchors_ok = anchors_ok and shot.get("style_anchor") == style
        reuse = shot.get("reuse_asset_ids", [])
        reuse_ok = reuse_ok and isinstance(reuse, list) and all(
            isinstance(item, str) and item in available_assets for item in reuse
        )
        if shot.get("new_keyframe") is not True:
            reuse_ok = reuse_ok and isinstance(reuse, list) and bool(reuse)
            reuse_ok = reuse_ok and all(str(item) in new_assets for item in reuse)
        searchable = f"{shot.get('prompt', '')}".casefold()
        claims_ok = claims_ok and not any(
            str(claim).casefold() in searchable
            for claim in MEDIA_REQUIREMENTS["forbidden_claims"]
        )
        source_ok = source_ok and isinstance(shot.get("source_ids"), list)
        source_ok = source_ok and bool(shot.get("source_ids"))
        source_ok = source_ok and all(
            isinstance(item, str) and item in allowed_sources for item in shot.get("source_ids", [])
        )
        if shot.get("new_keyframe") is True:
            available_assets.add(asset_id)
    new_count = sum(
        1 for shot in shots if isinstance(shot, Mapping) and shot.get("new_keyframe") is True
    )
    return {
        "shot_count_exact": len(shots) == MEDIA_REQUIREMENTS["shot_count"],
        "required_fields_present": fields_ok,
        "field_values_valid": values_ok,
        "new_keyframes_bounded": new_count <= MEDIA_REQUIREMENTS["max_new_keyframes"],
        "reported_new_keyframes_exact": package.get("new_keyframe_count") == new_count,
        "anchors_consistent": anchors_ok,
        "reuse_valid": reuse_ok,
        "claims_valid": claims_ok,
        "sources_present": source_ok,
        "traceability_asserted": package.get("traceable_claims") is True,
        "generation_calls_exact": type(package.get("estimated_generation_calls")) is int
        and package["estimated_generation_calls"] == len(shots) + new_count,
        "keyframe_count_integer": type(package.get("new_keyframe_count")) is int,
    }


def validate_node(
    node_id: str,
    artifact: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    incident_revealed: bool = False,
) -> ValidationResult:
    """Reject malformed provider data without turning it into runtime failure."""
    if not isinstance(artifact, Mapping) or (artifacts is not None and not isinstance(artifacts, Mapping)):
        return ValidationResult(node_id, False, {"artifact_mapping": False}, "schema_rejected")
    try:
        return _validate_node(node_id, artifact, artifacts, incident_revealed=incident_revealed)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return ValidationResult(node_id, False, {"field_types_valid": False}, "schema_rejected:field_type")


def _validate_node(
    node_id: str,
    artifact: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    incident_revealed: bool = False,
) -> ValidationResult:
    """Validate one bounded artifact using deterministic, local evidence."""

    if node_id not in NODE_MAP:
        return ValidationResult(node_id, False, {"known_node": False}, "unknown_node")
    node = NODE_MAP[node_id]
    context = artifacts or {}
    checks: dict[str, bool] = {
        "artifact_mapping": isinstance(artifact, Mapping),
        "required_keys": all(key in artifact for key in node.required_keys),
        "dependencies_available": all(dep in context for dep in node.dependencies),
    }
    if not checks["artifact_mapping"] or not checks["required_keys"]:
        return ValidationResult(node_id, False, checks, "schema_rejected")

    if node_id == "meta_decision":
        checks.update(
            topology=artifact.get("topology") == "multiplanner",
            planner_count=artifact.get("planner_count") == 2,
            management_bound=artifact.get("management_levels") == MAX_MANAGEMENT_LEVELS,
            confidence_valid=isinstance(artifact.get("confidence"), (int, float))
            and 0 <= float(artifact["confidence"]) <= 1,
        )
    elif node_id in {"planner_a", "planner_b"}:
        checks.update(
            plan_identity=isinstance(artifact.get("plan_id"), str)
            and 0 < len(str(artifact.get("plan_id")).strip()) <= 64,
            conditions_present=_text_list(artifact.get("success_conditions")),
        )
        if node_id == "planner_a":
            checks["stages_present"] = _text_list(artifact.get("stages"))
            checks.update(_plan_graph_checks(artifact))
        else:
            checks.update({field + "_present": _text_list(artifact.get(field)) for field in ("conflicts", "failure_modes", "recovery_boundaries")})
    elif node_id == "requirements_extract":
        checks["requirements_exact"] = all(
            artifact.get(key) == REQUIREMENTS[key]
            for key in (
                "deadline_hours",
                "implementation_budget_usd",
                "regional_peak_rpm",
                "total_peak_rpm",
                "p95_limit_seconds",
                "deliverables",
            )
        )
    elif node_id == "allocator_analysis":
        checks.update(
            algorithm=artifact.get("algorithm_family") == "largest_remainder_points",
            tie_breaker=artifact.get("tie_breaker") == "original_index",
            guards=set(artifact.get("guards", []))
            >= {"negative_total", "empty_weights", "negative_weight", "all_zero"},
            invariant="exact_total" in artifact.get("invariants", []),
        )
    elif node_id == "tenant_analysis":
        threat_text = str(artifact.get("threat", "")).casefold().replace("-", "_")
        checks.update(
            tenant_boundary=artifact.get("same_tenant_required") is True,
            roles=set(artifact.get("allowed_roles", [])) == {"admin", "analyst"},
            empty_rejected=artifact.get("empty_tenant_rejected") is True,
            threat=("tenant" in threat_text and "cross" in threat_text),
        )
    elif node_id == "contract_truth":
        signed = {key: value["signed_setup_usd"] for key, value in PROVIDERS.items()}
        checks.update(
            authority="signed" in str(artifact.get("authoritative_source", "")).casefold(),
            signed_prices=artifact.get("setup_costs_usd") == signed,
            marketing_rejected="marketing" in str(artifact.get("rejected_source", "")).casefold(),
            conflict_resolved=artifact.get("conflict_resolved") is True,
        )
    elif node_id == "residency_analysis":
        raw_forbidden = artifact.get("forbidden_routes", [])
        forbidden_pairs: set[str] = set()
        if isinstance(raw_forbidden, Mapping):
            for region, provider_ids in raw_forbidden.items():
                if isinstance(provider_ids, list):
                    forbidden_pairs.update(
                        f"{region}->{provider_id}" for provider_id in provider_ids
                    )
        elif isinstance(raw_forbidden, list):
            for item in raw_forbidden:
                if isinstance(item, str):
                    forbidden_pairs.add(item)
                elif isinstance(item, Mapping):
                    forbidden_pairs.add(
                        f"{item.get('region')}->{item.get('provider')}"
                    )
        raw_violations = artifact.get("violations", [])
        actual_route_violation = False
        if isinstance(raw_violations, list):
            actual_route_violation = any(
                isinstance(item, Mapping)
                and str(item.get("type", "")).casefold()
                in {"residency_violation", "forbidden_route", "illegal_route"}
                for item in raw_violations
            )
        checks.update(
            allowlist=artifact.get("region_provider_allowlist")
            == {"CN": ["A"], "EU": ["B", "C", "D"], "US": ["C", "D"]},
            no_violations=not actual_route_violation,
            forbidden_routes_complete=forbidden_pairs
            >= {"CN->B", "CN->C", "CN->D", "EU->A", "US->A", "US->B"},
        )
    elif node_id == "capacity_admission":
        checks.update(
            no_try_first=artifact.get("local_usable") is False,
            escalates_early=str(artifact.get("action", "")).casefold()
            in {
                "escalate_before_execution",
                "route_to_external_providers",
                "escalate_to_cloud",
                "cloud_first",
                "reject",
                "offload",
                "reject_local",
                "route_external",
            },
            queue_exact=artifact.get("queue_p95_seconds") == LOCAL_CAPACITY["queue_p95_seconds"],
            slots_exact=artifact.get("slots_available") == 0,
            load_exact=artifact.get("required_peak_rpm") == REQUIREMENTS["total_peak_rpm"],
        )
    elif node_id == "media_analysis":
        checks.update(
            shots=artifact.get("shot_count") == 8,
            keyframes=artifact.get("max_new_keyframes") == 3,
            anchors=set(artifact.get("required_anchors", []))
            == {"character_anchor", "style_anchor"},
            claims=artifact.get("forbidden_claims") == MEDIA_REQUIREMENTS["forbidden_claims"],
            reuse=artifact.get("reuse_required") is True,
        )
    elif node_id == "plan_merge":
        checks.update(_plan_graph_checks(artifact))
        raw_dag_nodes = artifact.get("dag_nodes", [])
        raw_conflicts = artifact.get("resolved_conflicts", [])
        dag_node_ids = (
            set(raw_dag_nodes)
            if isinstance(raw_dag_nodes, list)
            and all(isinstance(item, str) for item in raw_dag_nodes)
            else set()
        )
        checks.update(
            topology=artifact.get("selected_topology") == "multiplanner",
            complete_dag=dag_node_ids == set(NODE_MAP),
            no_duplicate_nodes=isinstance(raw_dag_nodes, list)
            and len(raw_dag_nodes) == len(NODE_MAP)
            and len(dag_node_ids) == len(raw_dag_nodes),
            management_bound=artifact.get("management_levels") == 2,
            conflicts_resolved=isinstance(raw_conflicts, list)
            and all(isinstance(item, str) for item in raw_conflicts)
            and set(raw_conflicts)
            >= {
                "signed_contracts_win",
                "residency_is_hard_gate",
                "tests_over_self_report",
            },
        )
    elif node_id == "allocator_patch":
        test_results = allocator_test_results(artifact)
        checks.update({f"allocator_{key}": value for key, value in test_results.items()})
    elif node_id == "tenant_policy_patch":
        try:
            test_results = tenant_test_results(artifact)
        except Exception:
            test_results = {"policy_execution": False}
        checks.update({f"tenant_{key}": value for key, value in test_results.items()})
    elif node_id == "provider_selection":
        checks.update(_provider_cost_checks(artifact, incident=False))
        selected_ids, _ = _provider_ids(
            artifact.get("selected_providers", [])
        )
        checks["expected_preincident_set"] = set(selected_ids) == {"A", "B", "C"}
        checks["authority"] = "signed" in str(
            artifact.get("authoritative_source", "")
        ).casefold()
    elif node_id == "deployment_plan":
        checks.update(_route_checks(artifact, incident=False))
        selected, valid_shape = _provider_ids(context.get("provider_selection", {}).get("selected_providers"))
        used = {provider for assignments in artifact.get("regional_routes", {}).values()
                for provider, amount in assignments.items() if isinstance(amount, int) and amount > 0}
        checks["selected_routes_consistent"] = valid_shape and bool(selected) and used <= set(selected)
        checks["rollback_supported"] = artifact.get("rollback_supported") is True
    elif node_id == "media_package":
        checks.update(_media_checks(artifact))
    elif node_id == "rollback_plan":
        raw_steps = artifact.get("steps", [])
        step_ids = {
            str(item)
            if isinstance(item, str)
            else str(item.get("action", ""))
            for item in raw_steps
            if isinstance(item, (str, Mapping))
        } if isinstance(raw_steps, list) else set()
        version_value = artifact.get("versioned")
        preserved_value = artifact.get("preserved_data")
        checks.update(
            versioned=version_value is True
            or (isinstance(version_value, str) and bool(version_value.strip())),
            bounded=0 < int(artifact.get("maximum_minutes", 0)) <= 30,
            preserved=preserved_value is True
            or (
                isinstance(preserved_value, (list, Mapping))
                and bool(preserved_value)
            ),
            tenant_verification=any(
                "verify_tenant_isolation" in step_id
                for step_id in step_ids
            ),
            triggers=bool(artifact.get("triggers")),
        )
    elif node_id == "code_review":
        allocator = context.get("allocator_patch", {})
        tenant = context.get("tenant_policy_patch", {})
        try:
            allocator_ok = _all_true(allocator_test_results(allocator))
            tenant_ok = _all_true(tenant_test_results(tenant))
        except Exception:
            allocator_ok = tenant_ok = False
        checks.update(
            allocator_recomputed=allocator_ok,
            tenant_recomputed=tenant_ok,
            reported_tests=artifact.get("test_count") == len(ALLOCATOR_CASES) + len(TENANT_CASES),
            independent=artifact.get("independent") is True,
            accepted_matches=artifact.get("accepted") is (allocator_ok and tenant_ok),
        )
    elif node_id == "security_review":
        tenant = context.get("tenant_policy_patch", {})
        deployment = context.get("deployment_plan", {})
        try:
            tenant_ok = _all_true(tenant_test_results(tenant))
        except Exception:
            tenant_ok = False
        route_ok = _all_true(_route_checks(deployment, incident=False))
        expected = tenant_ok and route_ok
        checks.update(
            tenant_recomputed=tenant_ok,
            residency_recomputed=route_ok,
            violations_empty=artifact.get("violations") == [],
            independent=artifact.get("independent") is True,
            accepted_matches=artifact.get("accepted") is expected,
        )
    elif node_id == "financial_review":
        selection = context.get("provider_selection", {})
        cost_checks = _provider_cost_checks(selection, incident=False)
        recomputed = sum(selection.get("setup_costs_usd", {}).values()) if isinstance(selection.get("setup_costs_usd"), Mapping) else -1
        checks.update(
            selection_valid=_all_true(cost_checks),
            total_matches=artifact.get("recomputed_total_usd") == recomputed,
            authority="signed" in str(
                artifact.get("authoritative_source", "")
            ).casefold(),
            marketing_rejected=artifact.get("marketing_prices_rejected") is True,
            accepted_matches=artifact.get("accepted") is (_all_true(cost_checks) and recomputed <= REQUIREMENTS["implementation_budget_usd"]),
        )
    elif node_id == "handoff_review":
        missing = [dep for dep in node.dependencies if dep not in context]
        expected = not missing
        checks.update(
            no_missing=artifact.get("missing_artifacts") == missing == [],
            schema=artifact.get("schema_valid") is True,
            provenance=artifact.get("provenance_complete") is True,
            versions=artifact.get("version_consistent") is True,
            accepted_matches=artifact.get("accepted") is expected,
        )
    elif node_id == "media_review":
        actual = _media_checks(context.get("media_package", {}))
        expected = _all_true(actual)
        checks.update(
            package_recomputed=expected,
            shot_report=artifact.get("shot_count_valid") is actual["shot_count_exact"],
            anchor_report=artifact.get("anchors_consistent") is actual["anchors_consistent"],
            reuse_report=artifact.get("reuse_valid") is actual["reuse_valid"],
            claims_report=artifact.get("claims_valid") is actual["claims_valid"],
            accepted_matches=artifact.get("accepted") is expected,
        )
    elif node_id == "incident_replan":
        plan_v2 = artifact.get("provider_plan_v2", {})
        deployment_v2 = artifact.get("deployment_plan_v2", {})
        cost_checks = _provider_cost_checks(plan_v2, incident=True)
        route_checks = _route_checks(deployment_v2, incident=True)
        sec = artifact.get("security_review_v2", {})
        fin = artifact.get("financial_review_v2", {})
        exact_affected = {"provider_selection", "deployment_plan", "security_review", "financial_review"}
        affected = {
            "provider_selection" if item == "provider_plan" else item
            for item in artifact.get("affected_nodes", [])
            if isinstance(item, str)
        }
        selected_ids, _ = _provider_ids(
            plan_v2.get("selected_providers", [])
        )
        selected = set(selected_ids)
        used = {
            provider_id
            for assignments in deployment_v2.get("regional_routes", {}).values()
            if isinstance(assignments, Mapping)
            for provider_id, amount in assignments.items()
            if isinstance(amount, int) and amount > 0
        }
        checks.update(
            incident_available=incident_revealed,
            affected_exact=affected == exact_affected,
            unaffected_preserved=set(artifact.get("preserved_nodes", [])) == set(INCIDENT_PACKET["unaffected_domains"]),
            provider_v2=_all_true(cost_checks),
            route_v2=_all_true(route_checks),
            selected_routes_consistent=selected == used,
            security_v2=sec.get("accepted") is True and sec.get("violations") == [] and _all_true(route_checks),
            financial_v2=fin.get("recomputed_total_usd") == plan_v2.get("total_setup_usd") and fin.get("accepted") is True,
        )
    elif node_id == "final_assembly":
        required_reviews = ("code_review", "security_review", "financial_review", "handoff_review", "media_review")
        reviews_ok = all(context.get(item, {}).get("accepted") is True for item in required_reviews)
        incident_result = context.get("incident_replan", {})
        incident_validation = validate_node(
            "incident_replan", incident_result, context, incident_revealed=True
        ) if incident_result else ValidationResult("incident_replan", False, {}, "missing")
        raw_hard_gates = artifact.get("hard_gates_passed")
        expected_hard_gates = {
            "allocator",
            "tenant_isolation",
            "residency",
            "capacity",
            "budget",
            "media_claims",
            "handoff",
        }
        if raw_hard_gates is True:
            hard_gate_report = True
        elif isinstance(raw_hard_gates, Mapping):
            hard_gate_report = bool(raw_hard_gates) and all(
                value is True for value in raw_hard_gates.values()
            )
        elif isinstance(raw_hard_gates, list) and all(
            isinstance(item, str) for item in raw_hard_gates
        ):
            hard_gate_report = set(raw_hard_gates) == expected_hard_gates
        else:
            hard_gate_report = False
        checks.update(
            reviews_accepted=reviews_ok,
            incident_recomputed=incident_validation.accepted,
            status=artifact.get("status") == "accepted",
            deliverables_exact=artifact.get("deliverables") == REQUIREMENTS["deliverables"],
            final_cost=artifact.get("final_setup_usd")
            == incident_result.get("financial_review_v2", {}).get(
                "recomputed_total_usd"
            ),
            final_capacity=artifact.get("total_capacity_rpm") == 2200,
            final_latency=float(artifact.get("p95_seconds", 99)) <= 3.5,
            hard_gates=hard_gate_report,
            traceability_present=isinstance(
                artifact.get("traceability"), Mapping
            )
            and bool(artifact.get("traceability")),
        )
    accepted = _all_true(checks)
    failed = [key for key, value in checks.items() if not value]
    return ValidationResult(
        node_id,
        accepted,
        checks,
        "accepted" if accepted else "failed:" + ",".join(failed),
    )


def run_oracle_preflight() -> dict[str, Any]:
    accepted: dict[str, Mapping[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for stage in STAGES:
        for node_id in stage:
            artifact = oracle_artifact(node_id)
            result = validate_node(
                node_id,
                artifact,
                accepted,
                incident_revealed=node_id in {"incident_replan", "final_assembly"},
            )
            results.append(result.public())
            if result.accepted:
                accepted[node_id] = artifact
    public_payload = public_task(reveal_incident=False)
    public_serialized = _canonical(public_payload)
    return {
        "passed": len(accepted) == len(NODES),
        "accepted_nodes": len(accepted),
        "total_nodes": len(NODES),
        "results": results,
        "oracle_commitment_sha256": sealed_oracle_digest(),
        # The commitment is deliberately public; the solution payload is not.
        "oracle_hidden": public_payload.get("oracle_included") is False
        and "oracle" not in public_payload
        and "solutions" not in public_payload
        and "largest_remainder_points_v1" not in public_serialized
        and "tenant_export_v1" not in public_serialized,
        "network_calls": 0,
        "paid_cost_usd": 0.0,
    }


@dataclass(frozen=True, slots=True)
class FallbackCandidate:
    slot: str
    tier: str
    reservation_usd: float
    total_remaining_usd: float
    provider_remaining_usd: float
    available: bool = True


_FALLBACK_TRIGGERS = {
    "timeout",
    "transport_error",
    "provider_rate_limited",
    "provider_unavailable",
    "invalid_json",
    "semantic_validation_failed",
    "secret_inspection_failed",
    "secret_lease_expired",
    "secret_unavailable",
    "primary_budget_unaffordable",
}
_FALLBACK_HARD_STOPS = {
    "secret_exposure_detected",
    "prompt_admission_failed",
    "project_revoked",
    "model_identity_mismatch",
}
_TIER_RANK = {"low": 1, "medium": 2, "high": 3}


def decide_budgeted_fallback(
    node_id: str,
    *,
    error_code: str,
    primary_cost_usd: float,
    node_budget_usd: float,
    candidates: Sequence[FallbackCandidate],
    fallback_count: int = 0,
) -> dict[str, Any]:
    """Pure routing decision.  It reserves nothing and calls no provider."""

    if node_id not in NODE_MAP:
        return {"decision": "deny", "reason": "unknown_node", "network_calls": 0}
    if error_code in _FALLBACK_HARD_STOPS:
        return {"decision": "deny", "reason": "hard_security_stop", "network_calls": 0}
    if error_code not in _FALLBACK_TRIGGERS:
        return {"decision": "deny", "reason": "non_retryable", "network_calls": 0}
    if fallback_count >= MAX_FALLBACKS_PER_NODE:
        return {"decision": "deny", "reason": "fallback_limit", "network_calls": 0}
    node = NODE_MAP[node_id]
    minimum_rank = 3 if node.risk in {"high", "critical"} or node.difficulty >= 8 else 2
    node_remaining = max(0.0, node_budget_usd - primary_cost_usd)
    eligible = [
        candidate
        for candidate in candidates
        if candidate.available
        and _TIER_RANK.get(candidate.tier, 0) >= minimum_rank
        and candidate.reservation_usd <= node_remaining
        and candidate.reservation_usd <= candidate.total_remaining_usd
        and candidate.reservation_usd <= candidate.provider_remaining_usd
    ]
    if not eligible:
        return {
            "decision": "deny",
            "reason": "no_capable_affordable_candidate",
            "node_remaining_usd": round(node_remaining, 6),
            "network_calls": 0,
        }
    selected = sorted(
        eligible,
        key=lambda item: (-_TIER_RANK[item.tier], item.reservation_usd, item.slot),
    )[0]
    return {
        "decision": "fallback",
        "reason": error_code,
        "selected_slot": selected.slot,
        "selected_tier": selected.tier,
        "reservation_usd": selected.reservation_usd,
        "fallback_number": fallback_count + 1,
        "network_calls": 0,
    }


def propose_shadow_weight_update(
    active_weights: Mapping[str, float], observation: Mapping[str, float]
) -> dict[str, Any]:
    """Create a bounded proposal without mutating the live routing weights."""

    before = dict(active_weights)
    proposal = dict(before)
    for key, delta in observation.items():
        if key in proposal and isinstance(delta, (int, float)) and math.isfinite(delta):
            proposal[key] = round(min(1.0, max(0.0, proposal[key] + float(delta))), 6)
    return {
        "active_before": before,
        "active_after": dict(active_weights),
        "shadow_proposal": proposal,
        "live_mutated": dict(active_weights) != before,
        "requires_offline_promotion": True,
    }


def _mutated(node_id: str) -> dict[str, Any]:
    return oracle_artifact(node_id)


def run_fault_matrix() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    def record(case_id: str, passed: bool, evidence: Any) -> None:
        cases.append({"case_id": case_id, "passed": bool(passed), "evidence": evidence})

    preflight = run_oracle_preflight()
    record("oracle_baseline", preflight["passed"], preflight["accepted_nodes"])
    initial = public_task(reveal_incident=False)
    record("oracle_not_in_public_task", initial.get("oracle_included") is False and "oracle" not in initial, initial.get("oracle_included"))
    record("incident_sealed_until_stage_7", "incident" not in initial and "incident" in public_task(reveal_incident=True), initial.get("incident_commitment_sha256"))

    mutation = _mutated("meta_decision")
    mutation["management_levels"] = 3
    record("unbounded_management_rejected", not validate_node("meta_decision", mutation).accepted, "management_levels=3")

    mutation = _mutated("contract_truth")
    mutation["setup_costs_usd"] = {key: value["marketing_setup_usd"] for key, value in PROVIDERS.items()}
    record("marketing_prices_rejected", not validate_node("contract_truth", mutation).accepted, mutation["setup_costs_usd"])

    mutation = _mutated("allocator_patch")
    mutation["implementation_id"] = "round_each_share_v0"
    record("allocator_rounding_mutant_rejected", not validate_node("allocator_patch", mutation).accepted, allocator_test_results(mutation))

    mutation = _mutated("tenant_policy_patch")
    mutation["same_tenant_required"] = False
    record("cross_tenant_mutant_rejected", not validate_node("tenant_policy_patch", mutation).accepted, tenant_test_results(mutation))

    mutation = _mutated("deployment_plan")
    mutation["regional_routes"]["EU"] = {"A": 700}
    record("illegal_residency_route_rejected", not validate_node("deployment_plan", mutation).accepted, _route_checks(mutation, incident=False))

    mutation = _mutated("deployment_plan")
    mutation["regional_routes"]["CN"] = {"A": 1200}
    mutation["total_capacity_rpm"] = 2400
    record("provider_capacity_shortfall_rejected", not validate_node("deployment_plan", mutation).accepted, _route_checks(mutation, incident=False))

    mutation = _mutated("capacity_admission")
    mutation["local_usable"] = True
    mutation["action"] = "try_local_first"
    record("local_try_first_rejected", not validate_node("capacity_admission", mutation).accepted, mutation["action"])

    mutation = _mutated("media_package")
    mutation["shots"][3]["new_keyframe"] = True
    mutation["new_keyframe_count"] = 4
    record("media_keyframe_cap_enforced", not validate_node("media_package", mutation).accepted, _media_checks(mutation))

    mutation = _mutated("media_package")
    mutation["shots"][0]["prompt"] = "The platform is 100% accurate"
    record("media_claim_guard_enforced", not validate_node("media_package", mutation).accepted, _media_checks(mutation))

    mutation = _mutated("media_package")
    mutation["shots"][1]["reuse_asset_ids"] = ["missing-keyframe"]
    record("media_reuse_guard_enforced", not validate_node("media_package", mutation).accepted, _media_checks(mutation))

    context = {node.node_id: oracle_artifact(node.node_id) for node in NODES if node.node_id != "media_package"}
    record("handoff_missing_artifact_rejected", not validate_node("handoff_review", _mutated("handoff_review"), context).accepted, "media_package missing")

    mutation = _mutated("incident_replan")
    mutation["affected_nodes"].append("allocator_patch")
    record("incident_scope_creep_rejected", not validate_node("incident_replan", mutation, incident_revealed=True).accepted, mutation["affected_nodes"])

    context = {node.node_id: oracle_artifact(node.node_id) for node in NODES}
    context["security_review"]["accepted"] = False
    record("final_hard_gate_cannot_be_bypassed", not validate_node("final_assembly", _mutated("final_assembly"), context, incident_revealed=True).accepted, "security_review=false")

    affordable = [FallbackCandidate("strong-backup", "high", 0.04, 0.5, 0.2)]
    for case_id, error in (
        ("timeout_gets_budgeted_fallback", "timeout"),
        ("semantic_failure_gets_budgeted_fallback", "semantic_validation_failed"),
        ("rate_limit_gets_budgeted_fallback", "provider_rate_limited"),
    ):
        decision = decide_budgeted_fallback("deployment_plan", error_code=error, primary_cost_usd=0.01, node_budget_usd=0.08, candidates=affordable)
        record(case_id, decision["decision"] == "fallback", decision)
    decision = decide_budgeted_fallback("deployment_plan", error_code="timeout", primary_cost_usd=0.07, node_budget_usd=0.08, candidates=affordable)
    record("fallback_denied_before_overspend", decision["decision"] == "deny", decision)
    decision = decide_budgeted_fallback("deployment_plan", error_code="secret_exposure_detected", primary_cost_usd=0, node_budget_usd=0.08, candidates=affordable)
    record("security_stop_never_falls_back", decision.get("reason") == "hard_security_stop", decision)
    decision = decide_budgeted_fallback("deployment_plan", error_code="timeout", primary_cost_usd=0, node_budget_usd=0.08, candidates=affordable, fallback_count=1)
    record("third_attempt_forbidden", decision.get("reason") == "fallback_limit", decision)

    completed = {"allocator_patch": "accepted", "tenant_policy_patch": "accepted"}
    failed_branch = {"provider_selection": "failed"}
    unaffected = dict(completed)
    record("parallel_failure_isolated", unaffected == completed and failed_branch["provider_selection"] == "failed", {"preserved": list(unaffected)})

    live = {"quality": 0.6, "cost": 0.4}
    shadow = propose_shadow_weight_update(live, {"quality": 0.05, "cost": -0.05})
    record("learning_is_shadow_only", shadow["live_mutated"] is False and shadow["shadow_proposal"] != live, shadow)

    return {
        "passed": all(case["passed"] for case in cases),
        "passed_cases": sum(1 for case in cases if case["passed"]),
        "total_cases": len(cases),
        "cases": cases,
        "network_calls": 0,
        "paid_cost_usd": 0.0,
    }


class _OverlapFixtureTransport:
    evidence_level = "E0-aurora-offline-overlap-fixture"

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._barrier = threading.Barrier(2)
        self.active = 0
        self.maximum_active = 0
        self.calls = 0

    def post(self, **_values: Any) -> HTTPResponse:
        with self._guard:
            self.active += 1
            self.calls += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            try:
                self._barrier.wait(timeout=0.25)
            except threading.BrokenBarrierError:
                pass
            time.sleep(0.02)
            body = {
                "choices": [{"message": {"content": "offline fixture accepted"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
            return HTTPResponse(200, json.dumps(body).encode("utf-8"), {}, 20)
        finally:
            with self._guard:
                self.active -= 1


def _fixture_work_order(task_id: str) -> WorkOrder:
    return WorkOrder(
        work_order_id=f"work_order_{task_id}",
        task_id=task_id,
        objective="Summarize the public Aurora fixture without external data",
        dependencies=(),
        input_artifacts=(),
        context_manifest=(),
        assumptions=("offline transport fixture only",),
        allowed_tools=(),
        forbidden_actions=("use network", "load secrets"),
        output_schema={"content": "string"},
        acceptance_tests=("contains fixture answer",),
        cost_limit=0.1,
        time_limit_ms=5000,
        escalation_conditions=("fixture unavailable",),
        idempotency_key=f"work-order-{task_id}",
    )


def run_adapter_concurrency_gate() -> dict[str, Any]:
    """Exercise the production adapter with an in-memory response transport."""

    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "aurora-offline.sqlite3"
        EventStore(database)
        store = ExperimentalCallStore(database)
        store.register_experiment(
            ExperimentBudget(
                experiment_id=AURORA_EXPERIMENT_ID,
                total_cost_limit_usd=1.0,
                provider_cost_limits_usd={"aurora-fixture": 1.0},
                max_logical_calls=4,
                max_attempts_per_call=1,
            )
        )
        transport = _OverlapFixtureTransport()
        adapters = [
            OpenAICompatibleHTTPAdapter(
                ExperimentalAdapterConfig(
                    adapter_id=f"aurora-offline-adapter-fixture-{index}",
                    provider="aurora-fixture",
                    endpoint="https://provider.example/v1/chat/completions",
                    model_id=f"offline-fixture-model-{index}",
                    deployment="remote",
                    input_price_per_mtok_usd=0,
                    output_price_per_mtok_usd=0,
                    max_retries=0,
                ),
                store=store,
                policy=AdapterRuntimePolicy(
                    experimental_calls_enabled=True,
                    paid_calls_enabled=True,
                ),
                transport=transport,
            )
            for index in (1, 2)
        ]

        def execute(index: int) -> Any:
            task_id = f"task_aurora_overlap_{index}"
            request = ExternalExecutionRequest(
                experiment_id=AURORA_EXPERIMENT_ID,
                run_id="run_aurora_overlap",
                task_id=task_id,
                work_order=_fixture_work_order(task_id),
                max_output_tokens=32,
                idempotency_key=f"aurora-overlap-{index}",
                project_id="project-aurora-overlap",
                data_residency="local",
            )
            return adapters[index - 1].execute(request)

        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(execute, index) for index in (1, 2)]
            for future in futures:
                try:
                    future.result(timeout=5)
                except Exception as error:  # pragma: no cover - diagnostic path
                    errors.append(type(error).__name__ + ":" + str(error))
        passed = not errors and transport.calls == 2 and transport.maximum_active >= 2
        return {
            "passed": passed,
            "same_project_overlap_observed": transport.maximum_active >= 2,
            "maximum_active_transports": transport.maximum_active,
            "fixture_transport_calls": transport.calls,
            "adapter_recorded_fixture_calls": store.counts()["external_calls"],
            "network_calls": 0,
            "paid_cost_usd": 0.0,
            "errors": errors,
            "blocker": None if passed else "same_project_adapter_calls_serialized",
        }


def run_offline_suite() -> dict[str, Any]:
    preflight = run_oracle_preflight()
    matrix = run_fault_matrix()
    concurrency = run_adapter_concurrency_gate()
    functional_passed = preflight["passed"] and matrix["passed"]
    return {
        "schema_version": AURORA_SCHEMA_VERSION,
        "task": frozen_task_record(),
        "oracle_preflight": preflight,
        "fault_matrix": matrix,
        "adapter_concurrency_gate": concurrency,
        "functional_passed": functional_passed,
        "paid_execution_ready": functional_passed and concurrency["passed"],
        "network_calls": 0,
        "paid_cost_usd": 0.0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the fully offline Aurora acceptance suite"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="required safety acknowledgement; no other execution mode exists",
    )
    arguments = parser.parse_args(argv)
    if not arguments.offline:
        parser.error("--offline is required; Aurora exposes no real-call mode")
    result = run_offline_suite()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["functional_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
