"""Authorized, hard-budgeted real-model pilot for the frozen Aurora task.

This entry point is deliberately separate from ``aurora_rescue_eval`` so the
offline benchmark can never gain a paid-call mode by accident.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters import (
    AdapterRuntimePolicy,
    DeepSeekChatHTTPAdapter,
    ExperimentalAdapterConfig,
    ExternalExecutionRequest,
    OpenAICompatibleHTTPAdapter,
)
from .aurora_checkpoint import CHECKPOINT_VERSION, analyze_report, terminal_checkpoint
from .aurora_rescue_eval import (
    ALLOCATOR_CASES,
    AURORA_TASK_VERSION,
    INCIDENT_PACKET,
    MEDIA_REQUIREMENTS,
    NODE_MAP,
    NODES,
    PROVIDERS,
    REQUIREMENTS,
    STAGES,
    TENANT_CASES,
    VALIDATOR_VERSION,
    _all_true,
    _provider_cost_checks,
    _route_checks,
    frozen_task_record,
    oracle_artifact,
    public_task,
    run_offline_suite,
    validate_node,
)
from .billing_calibration import (
    BillingCalibrationError,
    calibration_receipt,
    estimate_billed_cost_cny,
    load_billing_calibration,
    load_call_invoices,
    reconcile_provider_invoice,
)
from .event_store import EventStore
from .experimental_calls import ExperimentBudget, ExperimentalCallStore
from .planning_contracts import WorkOrder
from .provider_setup import DEFAULT_REGISTRY_FILE, project_root, utc_now
from .real_smoke_eval import PRICE_ASSUMPTIONS, _extract_json_object, _load_registry


SCHEMA_VERSION = "pre-v0.7-aurora-real-pilot-12-adaptive-execution-1"
EXPERIMENT_ID = "experiment_aurora_real_pilot_20261119_v12_adaptive_execution"
RUN_ID = "run_aurora_real_pilot_20261119_v12_adaptive_execution"
PILOT_SUFFIX = "v12-adaptive-execution-20261119"
AUTHORIZED_BUDGET_CNY: float | None = None
AGGREGATE_AUTHORIZED_BUDGET_USD = 4.90
PRIOR_INTERNAL_COST_USD = 2.61085564
# Provider dashboards are authoritative for completed historical calls. This
# reconciled CNY amount covers all three real-evaluation provider accounts,
# whereas PRIOR_INTERNAL_COST_USD is retained only as a legacy conservative
# reference and must never again be presented as an invoice amount.
PRIOR_RECONCILED_BILLED_COST_CNY = 1.640709
# The launcher supplies the explicitly authorized remaining caps for a fresh run.
MAX_BUDGET_USD = AGGREGATE_AUTHORIZED_BUDGET_USD
DEEPSEEK_REPORTED_BALANCE_CNY = 18.18
RELAY_REPORTED_BALANCE_CNY = 18.73
CNY_HARD_CAP_REFERENCE_MULTIPLIER = 6.759094
# The aggregate experiment ledger remains the authoritative hard cap. These
# limits only prevent one malformed work order or an infinite retry loop; they
# must not turn a healthy multi-dollar authorization into a tiny experiment.
MAX_CALL_BUDGET_USD = 0.75
MAX_LOGICAL_CALLS = 128
RANDOM_SEED = 20260901
PAID_AUTHORIZATION_ENV = "COSTWEAVE_AURORA_PAID_AUTHORIZED"

ALL_PREMIUM = "all_premium"
MANUAL_STATIC = "manual_static"
COSTWEAVE_DYNAMIC = "costweave_dynamic"
POLICIES = (ALL_PREMIUM, MANUAL_STATIC, COSTWEAVE_DYNAMIC)

BUDGET_CRITICAL_NODES = frozenset(
    {
        "meta_decision",
        "planner_a",
        "planner_b",
        "plan_merge",
        "provider_selection",
        "deployment_plan",
        "security_review",
        "financial_review",
        "incident_replan",
        "final_assembly",
    }
)

SAFE_OUTPUT_ALIASES: Mapping[str, Mapping[str, str]] = {
    "provider_selection": {
        "provider_ids": "selected_providers",
        "selected_provider_ids": "selected_providers",
        "total_cost_usd": "total_setup_usd",
    },
    "deployment_plan": {
        "routes": "regional_routes",
        "total_rpm": "total_capacity_rpm",
        "worst_p95_seconds": "p95_seconds",
    },
    "financial_review": {
        "total_setup_usd": "recomputed_total_usd",
    },
    "handoff_review": {
        "missing": "missing_artifacts",
    },
    "final_assembly": {
        "deliverable_ids": "deliverables",
        "final_cost_usd": "final_setup_usd",
        "final_capacity_rpm": "total_capacity_rpm",
        "final_p95_seconds": "p95_seconds",
    },
}

# Frozen after four semantically identical failures following stabilization of
# the provider-selection contract.  The failure remains visible, but is not
# paid for again.
LEARNED_TERMINAL_FAILURES: Mapping[str, Mapping[str, str]] = {
    MANUAL_STATIC: {
        "provider_selection": "repeated_capacity_insufficient_A_C_selection",
    }
}

PROVIDER_BY_SLOT = {
    "deepseek": "deepseek-official",
    "gpt": "traveltoken-gpt",
    "claude": "traveltoken-claude",
}


class AuroraRealPilotError(RuntimeError):
    """Fail-closed preflight or execution error."""


def _publish_new_json(path: Path, value: Mapping[str, Any]) -> str:
    """Publish complete bytes once. A second runner cannot replace the winner."""
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent)
    temporary_path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if os.name == "nt":
                # Windows rename fails if the target exists. POSIX rename replaces it.
                os.rename(temporary_path, path)
            else:
                os.link(temporary_path, path)
        except FileExistsError as error:
            raise AuroraRealPilotError("Aurora artifact already exists") from error
    finally:
        temporary_path.unlink(missing_ok=True)
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class RealNode:
    node_id: str
    stage: int
    dependencies: tuple[str, ...]
    required_keys: tuple[str, ...]
    difficulty: int
    risk: str
    max_output_tokens: int


OUTPUT_LIMITS = {
    "meta_decision": 160,
    "planner_a": 1024,
    "planner_b": 1024,
    "plan_merge": 448,
    "media_package": 2048,
    "rollback_plan": 320,
    "incident_replan": 640,
    "final_assembly": 896,
    "requirements_extract": 320,
}

REAL_NODES = {
    node.node_id: RealNode(
        node.node_id,
        node.stage,
        node.dependencies,
        node.required_keys,
        node.difficulty,
        node.risk,
        OUTPUT_LIMITS.get(node.node_id, 160),
    )
    for node in NODES
}

NODE_INSTRUCTIONS: Mapping[str, str] = {
    "meta_decision": "Apply this frozen management policy: use multiplanner with exactly two independent planners when the task has at least two independent high-risk decision domains or parallel specialist branches requiring independent review; use planner only for one coherent planning domain; use direct only for a bounded low-risk task; escalate only when planning itself cannot be safely completed. Planner count means independent whole-task plan proposals, not one planner per region. management_levels must be the integer 2, confidence must be a number from 0 to 1, and reasons must be short IDs.",
    "planner_a": "Create an independent delivery/dependency plan. Use a short plan_id, bounded stages, genuine parallel groups, critical path, and measurable success conditions.",
    "planner_b": "Create an independent constraint-conflict and recovery plan. Do not assume planner A's answer. Identify failure modes and local recovery boundaries.",
    "requirements_extract": "Extract the exact hard numerical requirements and deliverable IDs. Do not infer or round values.",
    "allocator_analysis": "Diagnose the supplied allocator and recommend a corrected algorithm, not the defective current algorithm. algorithm_family must be one of largest_remainder_points, independent_rounding, equal_split; guards must use applicable IDs negative_total, empty_weights, negative_weight, all_zero; invariants must include exact_total; tie_breaker must be one of original_index, largest_weight, random.",
    "tenant_analysis": "Analyze the export authorization boundary. Identify the root cause, tenant requirement, allowed roles, empty-tenant behavior, and concrete threat.",
    "contract_truth": "Resolve signed-contract versus marketing conflicts. The signed source is authoritative; preserve exact provider IDs and amounts.",
    "residency_analysis": "Derive exact provider allowlists and forbidden routes for CN, EU, and US from the frozen provider regions.",
    "capacity_admission": "Decide before execution whether the frozen local A800 snapshot can accept this workload. Return exact observed queue, slots, and required load.",
    "media_analysis": "Derive shot count, new-keyframe ceiling, required anchors, forbidden claims, and reuse requirement from the media contract.",
    "plan_merge": "Merge both independent plans and validated specialist evidence into the complete 24-node DAG. selected_topology must be exactly multiplanner. dag_nodes must be exactly the supplied dag_node_ids as a flat array of 24 string IDs: do not replace strings with node objects and do not redesign the frozen DAG. management_levels must be the integer 2. resolved_conflicts must include signed_contracts_win, residency_is_hard_gate, and tests_over_self_report. Keep critical_path and parallel_groups compact.",
    "allocator_patch": "Return a bounded allocator DSL patch, not executable source. implementation_id must be one of largest_remainder_points_v1, round_each_share_v0, equal_split_v0. Use guard IDs negative_total, empty_weights, negative_weight, all_zero where required; tie_breaker is a short ID.",
    "tenant_policy_patch": "Return a bounded policy DSL object. policy_id must be tenant_export_v1. Set the tenant, role, and empty-identifier controls based on the analyzed security contract.",
    "provider_selection": "Select the smallest pre-incident provider set that simultaneously covers each regional peak RPM under each provider's regions, max_rpm, and p95_seconds without overbooking, and stays within budget. authoritative_source must be exactly signed_contracts. Use signed_setup_usd, never marketing_setup_usd, and show exact selected-provider arithmetic.",
    "deployment_plan": "Produce exact pre-incident RPM assignments per region and provider. total_capacity_rpm means the sum of assigned route RPM in regional_routes, not the sum of providers' maximum installed capacities; it must equal the required 2200 RPM. p95_seconds is the worst P95 among used providers. residency_violations must be empty and rollback_supported must be true.",
    "media_package": "Return exactly eight very compact shot objects with every required media field. Keep prompt and negative_prompt under 18 words each. Create exactly three new keyframes; every other shot must reuse an asset_id from those keyframes. Keep character/style anchors identical. Every source_ids item must name a supplied validated dependency. traceable_claims must be the boolean true, not a list. estimated_generation_calls must count three keyframes plus eight shots and equal 11.",
    "rollback_plan": "Create a bounded rollback plan. For the compact contract, versioned must be boolean true, preserved_data must be boolean true, maximum_minutes must be at most 30, triggers must be non-empty, and steps must be a compact array of string action IDs that includes verify_tenant_isolation.",
    "code_review": "Independently recompute every supplied frozen case against both bounded DSL patches using only the JSON in this prompt; no tool or external-code execution is needed or claimed. Report booleans for both suites, test_count equal to the number of supplied cases, independent true, and accepted true only when all cases pass.",
    "security_review": "Independently recompute tenant isolation and regional residency from the supplied patch and deployment. List violations and accept only if none remain.",
    "financial_review": "Independently recompute selected-provider signed setup cost and budget compliance. Explicitly reject marketing prices.",
    "handoff_review": "Review only this node's six supplied dependency artifacts: allocator_patch, tenant_policy_patch, provider_selection, deployment_plan, media_package, rollback_plan. Do not treat final deliverables, later incident outputs, reviews, technical_plan, deterministic_tests, risk_table, or traceability_table as missing here. If all six dependencies are supplied and internally consistent, missing_artifacts must be [], schema_valid/provenance_complete/version_consistent must be true, and accepted must be true.",
    "media_review": "Independently verify shot count, anchor consistency, valid asset reuse, and forbidden claims from the supplied media package.",
    "incident_replan": "The incident is now revealed. affected_nodes must use exactly provider_selection, deployment_plan, security_review, financial_review. preserved_nodes must contain exactly the incident's unaffected_domains. Produce a mutually consistent updated provider set, deployment routes, security review, and financial review; every positively routed provider must be selected and every selected provider must carry positive routed load. Prefer the lowest signed incident setup cost that still meets all regional capacity, residency, P95, and budget gates. Do not change unaffected software, rollback, or media work.",
    "final_assembly": "Assemble only validated artifacts. Do not claim acceptance if any hard gate fails. Return the complete deliverable IDs, final incident-adjusted cost/capacity/P95, hard gates, and traceability.",
}

VIRTUAL_SPECS: Mapping[str, tuple[tuple[str, ...], int, str]] = {
    "provider_selection_v2": (("selected_providers", "setup_costs_usd", "total_setup_usd", "within_budget"), 256, "Recompute only the lowest-cost incident-adjusted provider selection that can support a valid regional deployment, using signed prices and the stated emergency D price."),
    "deployment_plan_v2": (("regional_routes", "total_capacity_rpm", "p95_seconds", "residency_violations"), 320, "Recompute only incident-adjusted regional routing, capacity, residency, and P95."),
    "security_review_v2": (("residency_passed", "violations", "accepted"), 160, "Independently review the supplied incident-adjusted deployment for residency and capacity safety."),
    "financial_review_v2": (("recomputed_total_usd", "within_budget", "accepted"), 160, "Independently recompute the supplied incident-adjusted provider cost against the implementation budget."),
}


def _route(slot: str, effort: str) -> dict[str, str]:
    return {"slot": slot, "reasoning_effort": effort}


def freeze_routes() -> dict[str, dict[str, dict[str, str]]]:
    premium: dict[str, dict[str, str]] = {}
    manual: dict[str, dict[str, str]] = {}
    dynamic: dict[str, dict[str, str]] = {}
    simple = {"requirements_extract", "contract_truth", "capacity_admission", "media_analysis"}
    code = {"allocator_analysis", "allocator_patch", "code_review"}
    critical = {"meta_decision", "plan_merge", "tenant_analysis", "tenant_policy_patch", "deployment_plan", "security_review", "incident_replan", "final_assembly"}
    for node in NODES:
        premium[node.node_id] = _route("claude", "high")
        if node.node_id in simple:
            manual[node.node_id] = _route("deepseek", "low")
        elif node.node_id in code:
            manual[node.node_id] = _route("gpt", "low")
        else:
            manual[node.node_id] = _route("claude", "high")
        if node.node_id == "handoff_review":
            dynamic[node.node_id] = _route("gpt", "low")
        elif node.node_id in simple:
            dynamic[node.node_id] = _route("deepseek", "low")
        elif node.node_id in {"allocator_analysis", "provider_selection", "financial_review"}:
            dynamic[node.node_id] = _route("gpt", "low")
        elif node.node_id in critical or node.difficulty >= 8 or node.risk in {"high", "critical"}:
            dynamic[node.node_id] = _route("claude", "high")
        else:
            dynamic[node.node_id] = _route("claude", "medium")
    for virtual in VIRTUAL_SPECS:
        premium[virtual] = _route("claude", "high")
        manual[virtual] = _route("claude", "high")
        dynamic[virtual] = _route("claude", "high" if "review" in virtual or "deployment" in virtual else "medium")
    return {ALL_PREMIUM: premium, MANUAL_STATIC: manual, COSTWEAVE_DYNAMIC: dynamic}


def _base_fixture(node_id: str, *, reveal_incident: bool) -> dict[str, Any]:
    source = public_task(reveal_incident=reveal_incident)
    fixture: dict[str, Any] = {"task_version": source["task_version"]}
    all_context = {"meta_decision", "planner_a", "planner_b"}
    requirement_context = {"requirements_extract", "plan_merge", "rollback_plan", "handoff_review", "final_assembly"}
    allocator_context = {"allocator_analysis", "allocator_patch", "code_review"}
    tenant_context = {"tenant_analysis", "tenant_policy_patch", "code_review", "security_review"}
    provider_context = {"contract_truth", "provider_selection", "financial_review", "deployment_plan", "residency_analysis"}
    capacity_context = {"capacity_admission", "deployment_plan"}
    media_context = {"media_analysis", "media_package", "media_review"}
    incident_context = {"incident_replan", *VIRTUAL_SPECS}
    if node_id in all_context:
        fixture.update(
            requirements=source["requirements"],
            local_capacity=source["local_capacity"],
            providers=source["providers"],
            software_fixtures=source["software_fixtures"],
            media_requirements=source["media_requirements"],
        )
    else:
        if node_id in requirement_context:
            fixture["requirements"] = source["requirements"]
        if node_id in allocator_context:
            fixture["allocator_fixture"] = source["software_fixtures"]["allocator"]
        if node_id in tenant_context:
            fixture["tenant_export_fixture"] = source["software_fixtures"]["tenant_export"]
        if node_id in provider_context:
            fixture["providers"] = source["providers"]
            fixture["implementation_budget_usd"] = source["requirements"]["implementation_budget_usd"]
            fixture["regional_peak_rpm"] = source["requirements"]["regional_peak_rpm"]
            fixture["p95_limit_seconds"] = source["requirements"]["p95_limit_seconds"]
        if node_id in capacity_context:
            fixture["local_capacity"] = source["local_capacity"]
            fixture["total_peak_rpm"] = source["requirements"]["total_peak_rpm"]
        if node_id in media_context:
            fixture["media_requirements"] = source["media_requirements"]
    if node_id in incident_context:
        fixture["requirements"] = source["requirements"]
        fixture["providers"] = source["providers"]
        fixture["incident"] = source["incident"]
    elif not reveal_incident:
        fixture["incident_commitment_sha256"] = source["incident_commitment_sha256"]
    if node_id in {"planner_a", "planner_b", "plan_merge"}:
        fixture["dag_node_ids"] = [node.node_id for node in NODES]
        fixture["dag_dependencies"] = {node.node_id: list(node.dependencies) for node in NODES}
        fixture["planning_contract"] = "parallel_groups must contain only independent known node IDs; critical_path is an ordered chain of at least two unique critical milestone IDs, each transitively depending on the previous milestone. stages and success_conditions must be nonempty lists of nonempty strings."
    if node_id == "code_review":
        fixture["allocator_test_cases"] = [
            {
                "id": name,
                "total": total,
                "weights": list(weights),
                "expected": (
                    expected.__name__
                    if isinstance(expected, type)
                    else expected
                ),
            }
            for name, total, weights, expected in ALLOCATOR_CASES
        ]
        fixture["tenant_test_cases"] = [
            {
                "requester_tenant": requester,
                "resource_tenant": resource,
                "role": role,
                "expected": expected,
            }
            for requester, resource, role, expected in TENANT_CASES
        ]
    return fixture


def _objective(
    node_id: str,
    *,
    dependencies: Mapping[str, Mapping[str, Any]],
    reveal_incident: bool,
) -> str:
    required = (
        list(REAL_NODES[node_id].required_keys)
        if node_id in REAL_NODES
        else list(VIRTUAL_SPECS[node_id][0])
    )
    instruction = NODE_INSTRUCTIONS.get(node_id, VIRTUAL_SPECS.get(node_id, ((), 0, ""))[2])
    payload = {
        "instruction": instruction,
        "required_output_keys": required,
        "frozen_synthetic_fixture": _base_fixture(node_id, reveal_incident=reveal_incident),
        "validated_dependencies": dependencies,
    }
    if node_id in {"provider_selection", "provider_selection_v2"}:
        payload["provider_constraint_contract"] = {
            "selected_providers_type": "array_of_unique_provider_id_strings",
            "allowed_provider_ids": sorted(PROVIDERS),
            "authoritative_price_field": "signed_setup_usd",
            "hard_constraints": [
                "regional_peak_rpm",
                "provider_region_allowlist",
                "provider_capacity_rpm",
                "p95_limit_seconds",
                "implementation_budget_usd",
            ],
            "optimization_order": [
                "fewest_providers",
                "lowest_signed_setup_cost",
                "provider_id_lexical_order",
            ],
        }
    return (
        "Return exactly one compact valid JSON object. No Markdown, preamble, commentary, or trailing text. "
        "Use numbers for numeric values and booleans for boolean values. Do not access tools, files, network, secrets, personal data, or project data.\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _adapter(
    provider_id: str,
    provider: Mapping[str, Any],
    route: Mapping[str, str],
    store: ExperimentalCallStore,
) -> Any:
    price = PRICE_ASSUMPTIONS[provider_id]
    adapter_type = DeepSeekChatHTTPAdapter if provider_id == "deepseek-official" else OpenAICompatibleHTTPAdapter
    return adapter_type(
        ExperimentalAdapterConfig(
            adapter_id=f"aurora-{provider_id}-{route['reasoning_effort']}",
            provider=provider_id,
            endpoint=str(provider["chat_completions_url"]),
            model_id=str(provider["selected_model"]),
            deployment="remote",
            input_price_per_mtok_usd=price.input_per_mtok_usd,
            output_price_per_mtok_usd=price.output_per_mtok_usd,
            timeout_seconds=90,
            max_retries=0,
            secret_env=str(provider["secret_env"]),
            reasoning_effort=str(route["reasoning_effort"]),
            input_reservation_multiplier=4.0 if provider.get("trust") != "official" else 1.0,
        ),
        store=store,
        policy=AdapterRuntimePolicy(True, True),
    )


def _request(
    *,
    node_id: str,
    policy_id: str,
    objective: str,
    max_output_tokens: int,
    cost_limit: float,
    attempt: str,
) -> tuple[WorkOrder, ExternalExecutionRequest]:
    task_id = f"task_aurora_{policy_id}_{node_id}_{attempt}"
    required = list(
        REAL_NODES[node_id].required_keys
        if node_id in REAL_NODES
        else VIRTUAL_SPECS[node_id][0]
    )
    output_schema: dict[str, Any] = {
        "type": "object",
        "required": required,
    }
    if node_id == "media_package":
        output_schema.update(
            additionalProperties=False,
            properties={
                "shots": {
                    "type": "array",
                    "minItems": MEDIA_REQUIREMENTS["shot_count"],
                    "maxItems": MEDIA_REQUIREMENTS["shot_count"],
                    "items": {
                        "type": "object",
                        "required": list(MEDIA_REQUIREMENTS["required_fields"]),
                        "additionalProperties": False,
                    },
                },
                "new_keyframe_count": {"type": "integer"},
                "traceable_claims": {"type": "boolean"},
                "estimated_generation_calls": {"type": "integer"},
            },
        )
    order = WorkOrder(
        work_order_id=f"work_order_{task_id}",
        task_id=task_id,
        objective=objective,
        dependencies=(),
        input_artifacts=(),
        context_manifest=(),
        assumptions=("All visible inputs are frozen synthetic fixtures.",),
        allowed_tools=(),
        forbidden_actions=("Do not use tools.", "Do not access external data or secrets.", "Return JSON only."),
        output_schema=output_schema,
        acceptance_tests=("valid compact JSON",),
        cost_limit=cost_limit,
        time_limit_ms=180_000,
        escalation_conditions=("required validated dependency missing",),
        idempotency_key=f"work-aurora-{policy_id}-{node_id}-{attempt}",
    )
    request = ExternalExecutionRequest(
        experiment_id=EXPERIMENT_ID,
        run_id=RUN_ID,
        task_id=task_id,
        work_order=order,
        max_output_tokens=max_output_tokens,
        idempotency_key=f"call-aurora-{policy_id}-{node_id}-{attempt}",
        project_id="project-aurora-real-pilot",
        data_residency="local",
        structured_output=node_id in {
            "planner_a",
            "planner_b",
            "media_package",
        },
    )
    return order, request


def _planned_call(
    *,
    node_id: str,
    policy_id: str,
    route: Mapping[str, str],
    dependencies: Mapping[str, Mapping[str, Any]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    attempt: str = "primary",
    reveal_incident: bool = False,
) -> tuple[Any, ExternalExecutionRequest, dict[str, Any]]:
    provider_id = PROVIDER_BY_SLOT[route["slot"]]
    maximum = REAL_NODES[node_id].max_output_tokens if node_id in REAL_NODES else VIRTUAL_SPECS[node_id][1]
    objective = _objective(node_id, dependencies=dependencies, reveal_incident=reveal_incident)
    _, provisional = _request(node_id=node_id, policy_id=policy_id, objective=objective, max_output_tokens=maximum, cost_limit=MAX_CALL_BUDGET_USD, attempt=attempt)
    adapter = _adapter(provider_id, providers[provider_id], route, store)
    estimate = dict(adapter.estimate_reservation(provisional))
    required = float(estimate["required_work_order_cost_limit_usd"])
    _, request = _request(node_id=node_id, policy_id=policy_id, objective=objective, max_output_tokens=maximum, cost_limit=required, attempt=attempt)
    return adapter, request, {
        **estimate,
        "node_id": node_id,
        "task_id": request.task_id,
        "policy_id": policy_id,
        "provider_id": provider_id,
        "requested_model": providers[provider_id]["selected_model"],
        "reasoning_effort": route["reasoning_effort"],
        "affordable": required <= MAX_CALL_BUDGET_USD + 1e-12,
    }


def _preflight_budget_gate(
    rows: Sequence[Mapping[str, Any]], *, budget_usd: float
) -> dict[str, Any]:
    """Separate hard affordability from conservative planning projections.

    Reservations shrink to actual cost after each call. Requiring every primary
    and every hypothetical fallback to fit simultaneously therefore rejects
    runs that the atomic ledger can safely execute in smaller waves.
    """

    requirements = [
        float(row["required_work_order_cost_limit_usd"]) for row in rows
    ]
    projected_worst = round(sum(requirements), 10)
    largest_single = round(max(requirements, default=0.0), 10)
    all_calls_individually_affordable = all(
        bool(row.get("affordable")) for row in rows
    )
    hard_gate_passed = bool(
        all_calls_individually_affordable
        and largest_single <= budget_usd + 1e-12
    )
    staged_admission_required = projected_worst > budget_usd + 1e-12
    warnings = []
    if staged_admission_required:
        warnings.append(
            "projected_all-primary-and-fallback-reservations_exceed_budget; "
            "runtime_will_use_staged_atomic_admission"
        )
    return {
        "hard_gate_passed": hard_gate_passed,
        "largest_single_reservation_usd": largest_single,
        "projected_worst_case_usd": projected_worst,
        "staged_admission_required": staged_admission_required,
        "soft_warnings": warnings,
    }


def _select_reservation_wave(
    runnable: Sequence[str],
    plans: Mapping[str, Mapping[str, Any]],
    *,
    total_remaining_usd: float,
    provider_remaining_usd: Mapping[str, float],
    logical_calls_remaining: float = math.inf,
    max_workers: int | None = None,
) -> tuple[list[str], list[str]]:
    """Admit a safe useful sub-wave instead of reserving the entire DAG."""

    original_index = {node_id: index for index, node_id in enumerate(runnable)}
    ordered = sorted(
        runnable,
        key=lambda node_id: (
            0 if node_id in BUDGET_CRITICAL_NODES else 1,
            float(plans[node_id]["required_work_order_cost_limit_usd"]),
            original_index[node_id],
        ),
    )
    total_left = max(0.0, float(total_remaining_usd))
    provider_left = {
        provider: max(0.0, float(value))
        for provider, value in provider_remaining_usd.items()
    }
    admitted: list[str] = []
    deferred: list[str] = []
    for node_id in ordered:
        if max_workers is not None and len(admitted) >= max_workers:
            break
        plan = plans[node_id]
        provider = str(plan["provider_id"])
        required = float(plan["required_work_order_cost_limit_usd"])
        if (
            required <= total_left + 1e-12
            and required <= provider_left.get(provider, 0.0) + 1e-12
            and (provider == "local-deterministic" or logical_calls_remaining > 0)
        ):
            admitted.append(node_id)
            total_left -= required
            provider_left[provider] = provider_left.get(provider, 0.0) - required
            if provider != "local-deterministic":
                logical_calls_remaining -= 1
        else:
            deferred.append(node_id)
    admitted.sort(key=original_index.__getitem__)
    deferred.sort(key=original_index.__getitem__)
    return admitted, deferred


def _admit_runtime_wave(
    *,
    runnable: Sequence[str],
    policy_id: str,
    routes: Mapping[str, Mapping[str, str]],
    snapshot: Mapping[str, Mapping[str, Any]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    max_workers: int,
    pending_reservations: Mapping[str, tuple[str, float]],
) -> tuple[list[str], list[str], dict[str, Mapping[str, Any]]]:
    """Plan a sub-wave; fail open while atomic reservations remain authoritative."""

    if not hasattr(store, "budget_headroom"):
        return list(runnable[:max_workers]), [], {}
    try:
        plans: dict[str, Mapping[str, Any]] = {}
        for node_id in runnable:
            if (
                policy_id == COSTWEAVE_DYNAMIC
                and _deterministic_artifact(node_id) is not None
            ):
                plans[node_id] = {
                    "provider_id": "local-deterministic",
                    "required_work_order_cost_limit_usd": 0.0,
                }
                continue
            _, _, plan = _planned_call(
                node_id=node_id,
                policy_id=policy_id,
                route=routes[node_id],
                dependencies=_call_dependencies(node_id, snapshot),
                providers=providers,
                store=store,
                reveal_incident=node_id == "incident_replan" or node_id in VIRTUAL_SPECS,
            )
            plans[node_id] = plan
        paid_providers = {
            str(plan["provider_id"])
            for plan in plans.values()
            if str(plan["provider_id"]) != "local-deterministic"
        }
        snapshots = {
            provider: store.budget_headroom(
                EXPERIMENT_ID, provider=provider, pending_reservations=pending_reservations
            )
            for provider in paid_providers
        }
        total_remaining = min(
            (float(item["total_remaining_usd"]) for item in snapshots.values()),
            default=math.inf,
        )
        provider_remaining = {
            provider: float(item["provider_remaining_usd"])
            for provider, item in snapshots.items()
        }
        provider_remaining["local-deterministic"] = math.inf
        admitted, deferred = _select_reservation_wave(
            runnable,
            plans,
            total_remaining_usd=total_remaining,
            provider_remaining_usd=provider_remaining,
            logical_calls_remaining=min(
                (item["logical_calls_remaining"] for item in snapshots.values()), default=math.inf
            ),
            max_workers=max_workers,
        )
        return admitted, deferred, plans
    except Exception:
        # Admission planning is advisory. The atomic store still blocks any
        # real overspend, so diagnostic failure must not stop a healthy run.
        return list(runnable[:max_workers]), [], {}


def _validate_virtual(
    node_id: str,
    value: Mapping[str, Any],
    dependencies: Mapping[str, Mapping[str, Any]],
) -> bool:
    required = set(VIRTUAL_SPECS[node_id][0])
    if not required <= set(value):
        return False
    if node_id == "provider_selection_v2":
        raw_selected = value.get("selected_providers", [])
        selected = {
            (
                item.strip().upper()
                if isinstance(item, str)
                else next(
                    (
                        str(item[key]).strip().upper()
                        for key in ("provider_id", "id", "provider", "code")
                        if isinstance(item, Mapping)
                        and key in item
                        and isinstance(item[key], str)
                    ),
                    "",
                )
            )
            for item in raw_selected
        } if isinstance(raw_selected, list) else set()
        return (
            _all_true(_provider_cost_checks(value, incident=True))
            and {"A", "C", "D"} <= selected <= set(PROVIDERS)
        )
    if node_id == "deployment_plan_v2":
        return _all_true(_route_checks(value, incident=True))
    if node_id == "security_review_v2":
        deployment = dependencies.get("deployment_plan_v2", {})
        route_ok = _all_true(_route_checks(deployment, incident=True))
        return (
            route_ok
            and value.get("residency_passed") is True
            and value.get("violations") == []
            and value.get("accepted") is True
        )
    selection = dependencies.get("provider_selection_v2", {})
    expected = selection.get("total_setup_usd")
    return (
        _all_true(_provider_cost_checks(selection, incident=True))
        and value.get("recomputed_total_usd") == expected
        and value.get("within_budget") is (float(expected or 0) <= REQUIREMENTS["implementation_budget_usd"])
        and value.get("accepted") is True
    )


def _normalize_model_value(
    node_id: str, value: Mapping[str, Any] | None
) -> tuple[Mapping[str, Any] | None, tuple[str, ...]]:
    """Apply only lossless structural repairs before considering a paid retry."""

    if value is None:
        return None, ()
    current: Mapping[str, Any] = value
    unwrapped: list[str] = []
    required = set(
        REAL_NODES[node_id].required_keys
        if node_id in REAL_NODES
        else VIRTUAL_SPECS[node_id][0]
    )
    aliases = SAFE_OUTPUT_ALIASES.get(node_id, {})
    for wrapper in ("result", "output", "data"):
        candidate = current.get(wrapper)
        candidate_keys = set(candidate) if isinstance(candidate, Mapping) else set()
        repairable_keys = candidate_keys | {
            target for source, target in aliases.items() if source in candidate_keys
        }
        if (
            isinstance(candidate, Mapping)
            and not required <= set(current)
            and required <= repairable_keys
        ):
            current = candidate
            unwrapped.append(f"unwrap:{wrapper}")
            break
    normalized = dict(current)
    repairs = list(unwrapped)
    for source, target in aliases.items():
        if target not in normalized and source in normalized:
            normalized[target] = normalized[source]
            repairs.append(f"alias:{source}->{target}")
    if node_id in {"deployment_plan", "deployment_plan_v2"}:
        raw_routes = normalized.get("regional_routes")
        if isinstance(raw_routes, list):
            nested_routes: dict[str, dict[str, int]] = {}
            lossless = bool(raw_routes)
            for row in raw_routes:
                if (
                    not isinstance(row, Mapping)
                    or set(row) != {"region", "provider", "rpm"}
                ):
                    lossless = False
                    break
                region = row.get("region")
                provider_id = row.get("provider")
                rpm = row.get("rpm")
                if (
                    not isinstance(region, str)
                    or not isinstance(provider_id, str)
                    or not isinstance(rpm, int)
                    or isinstance(rpm, bool)
                    or rpm < 0
                    or provider_id in nested_routes.get(region, {})
                ):
                    lossless = False
                    break
                nested_routes.setdefault(region, {})[provider_id] = rpm
            if lossless:
                normalized["regional_routes"] = nested_routes
                repairs.append("shape:regional_route_rows->mapping")
    return normalized, tuple(repairs)


def _extract_repaired_json_object(
    content: str,
) -> tuple[Mapping[str, Any] | None, tuple[str, ...]]:
    """Parse JSON, then apply only bounded lexical/closure repairs.

    Semantic values are never invented here.  A repaired object still passes
    the same independent node validator before it can enter the DAG.
    """

    direct = _extract_json_object(content)
    if direct is not None:
        return direct, ()
    text = str(content).strip().lstrip("\ufeff")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```\s*$", "", text)
    left = text.find("{")
    if left < 0:
        return None, ()
    candidate = text[left:]
    repairs: list[str] = []
    # Never rewrite quoted values. Ambiguous smart-quote delimiters are
    # rejected rather than risking changes to provider-generated prose/code.
    cleaned: list[str] = []
    quoted = escaped_character = False
    for index, character in enumerate(candidate):
        if quoted:
            cleaned.append(character)
            if escaped_character:
                escaped_character = False
            elif character == "\\":
                escaped_character = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        if character == "," and candidate[index + 1:].lstrip().startswith(("}", "]")):
            continue
        cleaned.append(character)
    without_trailing = "".join(cleaned)
    if without_trailing != candidate:
        repairs.append("json:trailing_comma")
        candidate = without_trailing

    stack: list[str] = []
    in_string = False
    escaped = False
    malformed = False
    for character in candidate:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            stack.append(character)
        elif character in "]}":
            expected = "[" if character == "]" else "{"
            if not stack or stack.pop() != expected:
                malformed = True
                break
    if malformed or in_string:
        return None, tuple(repairs)
    if stack:
        if candidate.rstrip().endswith(","):
            candidate = candidate.rstrip()[:-1]
            if "json:trailing_comma" not in repairs:
                repairs.append("json:trailing_comma")
        candidate += "".join("}" if item == "{" else "]" for item in reversed(stack))
        repairs.append("json:balanced_closure")
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        return None, tuple(repairs)
    return (value, tuple(repairs)) if isinstance(value, Mapping) else (None, tuple(repairs))


def _provider_subset_feasible(
    selected: Sequence[str], *, incident: bool
) -> bool:
    """Solve the tiny regional capacity problem as a deterministic max flow."""

    source = "source"
    sink = "sink"
    capacities: dict[str, dict[str, int]] = {}

    def add_edge(left: str, right: str, capacity: int) -> None:
        capacities.setdefault(left, {})[right] = int(capacity)
        capacities.setdefault(right, {}).setdefault(left, 0)

    total = 0
    for region, demand_value in REQUIREMENTS["regional_peak_rpm"].items():
        demand = int(demand_value)
        total += demand
        region_node = f"region:{region}"
        add_edge(source, region_node, demand)
        for provider_id in selected:
            provider = PROVIDERS.get(provider_id)
            if provider is None or region not in provider["regions"]:
                continue
            if (
                incident
                and provider_id == "C"
                and region == "EU"
                and INCIDENT_PACKET["provider_c_eu_allowed"] is False
            ):
                continue
            if float(provider["p95_seconds"]) > float(
                REQUIREMENTS["p95_limit_seconds"]
            ):
                continue
            add_edge(region_node, f"provider:{provider_id}", demand)
    for provider_id in selected:
        if provider_id not in PROVIDERS:
            return False
        capacity = int(PROVIDERS[provider_id]["capacity_rpm"])
        if incident and provider_id == "B":
            capacity = int(INCIDENT_PACKET["provider_b_capacity_rpm"])
        add_edge(f"provider:{provider_id}", sink, capacity)

    flow = 0
    while True:
        parent: dict[str, str | None] = {source: None}
        queue = [source]
        for node in queue:
            for neighbor, remaining in capacities.get(node, {}).items():
                if remaining > 0 and neighbor not in parent:
                    parent[neighbor] = node
                    queue.append(neighbor)
                    if neighbor == sink:
                        break
            if sink in parent:
                break
        if sink not in parent:
            break
        amount = total
        cursor = sink
        while parent[cursor] is not None:
            previous = str(parent[cursor])
            amount = min(amount, capacities[previous][cursor])
            cursor = previous
        cursor = sink
        while parent[cursor] is not None:
            previous = str(parent[cursor])
            capacities[previous][cursor] -= amount
            capacities[cursor][previous] += amount
            cursor = previous
        flow += amount
    return flow == total


def _deterministic_provider_selection(*, incident: bool) -> dict[str, Any]:
    """Return the lexically stable minimum-cardinality, minimum-cost set."""

    provider_ids = tuple(sorted(PROVIDERS))

    def signed_cost(provider_id: str) -> int:
        if incident and provider_id == "D":
            return int(INCIDENT_PACKET["provider_d_emergency_setup_usd"])
        return int(PROVIDERS[provider_id]["signed_setup_usd"])

    feasible: list[tuple[int, int, tuple[str, ...]]] = []
    for size in range(1, len(provider_ids) + 1):
        for subset in combinations(provider_ids, size):
            total_cost = sum(signed_cost(item) for item in subset)
            if (
                total_cost <= int(REQUIREMENTS["implementation_budget_usd"])
                and _provider_subset_feasible(subset, incident=incident)
            ):
                feasible.append((size, total_cost, subset))
        if feasible:
            break
    if not feasible:
        raise AuroraRealPilotError("no provider set satisfies the frozen constraints")
    _, total_cost, selected = min(feasible)
    result: dict[str, Any] = {
        "selected_providers": list(selected),
        "setup_costs_usd": {item: signed_cost(item) for item in selected},
        "total_setup_usd": total_cost,
        "within_budget": total_cost <= int(REQUIREMENTS["implementation_budget_usd"]),
    }
    if not incident:
        result["authoritative_source"] = "signed_contracts"
    return result


def _apply_deterministic_provider_constraints(
    node_id: str, value: Mapping[str, Any]
) -> tuple[Mapping[str, Any], str]:
    expected = _deterministic_provider_selection(
        incident=node_id == "provider_selection_v2"
    )
    corrected = dict(value)
    changed = any(
        corrected.get(key) != expected_value
        for key, expected_value in expected.items()
    )
    corrected.update(expected)
    return corrected, (
        "deterministic:provider_set_corrected"
        if changed
        else "deterministic:provider_set_verified"
    )


def _apply_deterministic_deployment_arithmetic(
    node_id: str, value: Mapping[str, Any]
) -> tuple[Mapping[str, Any], str]:
    """Recompute reported totals only after the proposed routes are valid."""

    incident = node_id == "deployment_plan_v2"
    route_checks = _route_checks(value, incident=incident)
    allocation_checks = (
        "route_values_valid",
        "regional_loads_exact",
        "provider_capacity_sufficient",
        "residency_valid",
        "total_exact",
    )
    if not all(route_checks.get(check) for check in allocation_checks):
        return dict(value), "deterministic:deployment_arithmetic_not_applied"
    routes = value.get("regional_routes", {})
    total = 0
    used_latencies: list[float] = []
    for assignments in routes.values():
        for provider_id, rpm in assignments.items():
            total += int(rpm)
            if int(rpm) > 0:
                used_latencies.append(float(PROVIDERS[provider_id]["p95_seconds"]))
    expected = {
        "total_capacity_rpm": total,
        "p95_seconds": max(used_latencies, default=0.0),
    }
    corrected = dict(value)
    changed = any(corrected.get(key) != amount for key, amount in expected.items())
    corrected.update(expected)
    return corrected, (
        "deterministic:deployment_arithmetic_corrected"
        if changed
        else "deterministic:deployment_arithmetic_verified"
    )


def _failure_category(row: Mapping[str, Any]) -> str:
    status = str(row.get("status") or "")
    diagnostic = str(row.get("diagnostic") or "").lower()
    error = str(row.get("error") or "").lower()
    finish_reasons = {
        str(reason).strip().lower() for reason in (row.get("finish_reasons") or ())
    }
    if error == "promptinjectionerror" or finish_reasons & {"content_filter", "refusal"}:
        return "hard_security"
    if any(
        marker in diagnostic
        for marker in (
            "secret_exposure_detected",
            "credential_exposure",
            "prompt_injection_confirmed",
            "requires a separate security approval",
        )
    ):
        return "hard_security"
    if status.startswith("blocked_budget") or "budget" in diagnostic:
        return "budget"
    if diagnostic == "truncated_output" or finish_reasons.intersection(
        {"length", "max_tokens", "max_output_tokens"}
    ):
        return "truncated"
    if "timeout" in diagnostic or "transport" in error or "rate" in diagnostic:
        return "transport"
    if diagnostic in {"invalid_json", "missing_required_keys"}:
        return "format"
    if status == "completed" and not row.get("accepted"):
        return "semantic"
    return "unknown"


def _deterministic_artifact(node_id: str) -> Mapping[str, Any] | None:
    """Solve bounded arithmetic/extraction nodes locally without model tokens."""

    source = public_task(reveal_incident=False)
    requirements = source["requirements"]
    providers = source["providers"]
    if node_id == "requirements_extract":
        return {
            key: requirements[key]
            for key in (
                "deadline_hours",
                "deliverables",
                "implementation_budget_usd",
                "p95_limit_seconds",
                "regional_peak_rpm",
                "total_peak_rpm",
            )
        }
    if node_id == "contract_truth":
        return {
            "authoritative_source": "signed_contracts",
            "conflict_resolved": True,
            "rejected_source": "marketing_pages",
            "setup_costs_usd": {
                provider_id: provider["signed_setup_usd"]
                for provider_id, provider in providers.items()
            },
        }
    if node_id == "capacity_admission":
        local = source["local_capacity"]
        usable = bool(
            int(local["slots_available"]) > 0
            and float(local["queue_p95_seconds"])
            <= float(requirements["p95_limit_seconds"])
        )
        return {
            "action": "use_local" if usable else "escalate_before_execution",
            "local_usable": usable,
            "queue_p95_seconds": local["queue_p95_seconds"],
            "reason": (
                "local_capacity_and_queue_accepted"
                if usable
                else "no_local_slots_and_queue_exceeds_latency_budget"
            ),
            "required_peak_rpm": requirements["total_peak_rpm"],
            "slots_available": local["slots_available"],
        }
    if node_id == "residency_analysis":
        allowlist: dict[str, list[str]] = {}
        forbidden: list[str] = []
        for region in requirements["regional_peak_rpm"]:
            allowed = sorted(
                provider_id
                for provider_id, provider in providers.items()
                if region in provider["regions"]
            )
            allowlist[region] = allowed
            forbidden.extend(
                f"{region}->{provider_id}"
                for provider_id in sorted(providers)
                if provider_id not in allowed
            )
        return {
            "forbidden_routes": forbidden,
            "region_provider_allowlist": allowlist,
            "violations": [],
        }
    if node_id == "media_analysis":
        media = source["media_requirements"]
        return {
            "forbidden_claims": list(media["forbidden_claims"]),
            "max_new_keyframes": media["max_new_keyframes"],
            "required_anchors": [
                field
                for field in media["required_fields"]
                if field in {"character_anchor", "style_anchor"}
            ],
            "reuse_required": True,
            "shot_count": media["shot_count"],
        }
    return None


def _execute_once(
    *,
    node_id: str,
    policy_id: str,
    route: Mapping[str, str],
    dependencies: Mapping[str, Mapping[str, Any]],
    all_artifacts: Mapping[str, Mapping[str, Any]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    attempt: str,
    reveal_incident: bool,
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    started = time.perf_counter()
    row: dict[str, Any] = {
        "node_id": node_id,
        "policy_id": policy_id,
        "attempt": attempt,
        "slot": route["slot"],
        "reasoning_effort": route["reasoning_effort"],
        "accepted": False,
        "status": "failed",
    }
    try:
        adapter, request, budget_plan = _planned_call(
            node_id=node_id,
            policy_id=policy_id,
            route=route,
            dependencies=dependencies,
            providers=providers,
            store=store,
            attempt=attempt,
            reveal_incident=reveal_incident,
        )
        row["budget_plan"] = budget_plan
        row["provider_id"] = budget_plan["provider_id"]
        row["model_requested"] = budget_plan["requested_model"]
        if not budget_plan["affordable"]:
            row.update(status="blocked_budget_preflight", diagnostic="single_call_ceiling")
            return row, None
        receipt = adapter.execute(request)
        content = str(receipt.result.output.get("content", ""))
        telemetry = dict(receipt.telemetry)
        finish_reasons = {
            str(reason).strip().lower()
            for reason in telemetry.get("finish_reasons", [])
            if reason is not None
        }
        truncated = bool(
            finish_reasons.intersection(
                {"length", "max_tokens", "max_output_tokens"}
            )
        )
        if truncated:
            # A prefix cut at the model's output ceiling is not repairable
            # evidence.  Treating it as malformed JSON hid the real failure and
            # could accidentally accept an incomplete plan.
            value = None
            all_repairs: list[str] = []
        else:
            extracted, json_repairs = _extract_repaired_json_object(content)
            value, normalization_repairs = _normalize_model_value(node_id, extracted)
            all_repairs = [*json_repairs, *normalization_repairs]
        if (
            policy_id == COSTWEAVE_DYNAMIC
            and node_id in {"provider_selection", "provider_selection_v2"}
            and value is not None
        ):
            value, provider_repair = _apply_deterministic_provider_constraints(
                node_id, value
            )
            all_repairs.append(provider_repair)
        if (
            policy_id == COSTWEAVE_DYNAMIC
            and node_id in {"deployment_plan", "deployment_plan_v2"}
            and value is not None
        ):
            value, deployment_repair = _apply_deterministic_deployment_arithmetic(
                node_id, value
            )
            all_repairs.append(deployment_repair)
        if truncated:
            accepted = False
            diagnostic = "truncated_output"
            checks = {"finish_reason_complete": False}
        elif node_id in VIRTUAL_SPECS:
            accepted = bool(
                value and _validate_virtual(node_id, value, dependencies)
            )
            diagnostic = "accepted" if accepted else "virtual_oracle_rejected"
            checks: Mapping[str, bool] = {"virtual_oracle": accepted}
        elif value is None:
            accepted = False
            diagnostic = "invalid_json"
            checks = {"valid_json": False}
        else:
            validation = validate_node(
                node_id,
                value,
                all_artifacts,
                incident_revealed=reveal_incident or node_id == "final_assembly",
            )
            accepted = validation.accepted
            diagnostic = validation.diagnostic
            checks = validation.checks
        row.update(
            status="completed",
            validator_version=VALIDATOR_VERSION,
            accepted=accepted,
            diagnostic=diagnostic,
            checks=dict(checks),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            provider_latency_ms=telemetry.get("request_latency_ms"),
            input_tokens=telemetry.get("input_tokens"),
            output_tokens=telemetry.get("output_tokens"),
            reasoning_tokens=telemetry.get("reasoning_tokens"),
            cached_input_tokens=telemetry.get("cached_input_tokens", 0),
            cache_read_input_tokens=telemetry.get("cache_read_input_tokens", 0),
            cache_creation_input_tokens=telemetry.get(
                "cache_creation_input_tokens", 0
            ),
            estimated_cache_read_cost_cny=telemetry.get(
                "estimated_cache_read_cost_cny"
            ),
            estimated_cache_creation_cost_cny=telemetry.get(
                "estimated_cache_creation_cost_cny"
            ),
            provider_billed_cache_read_cost_cny=telemetry.get(
                "provider_billed_cache_read_cost_cny"
            ),
            provider_billed_cache_creation_cost_cny=telemetry.get(
                "provider_billed_cache_creation_cost_cny"
            ),
            actual_cost_usd=telemetry.get("actual_cost_usd"),
            external_call_id=telemetry.get("external_call_id"),
            estimated_billed_cost_cny=telemetry.get(
                "estimated_billed_cost_cny"
            ),
            provider_billed_cost_cny=telemetry.get(
                "provider_billed_cost_cny"
            ),
            billing_residual_cny=telemetry.get("billing_residual_cny"),
            billing_currency=telemetry.get("billing_currency", "CNY"),
            invoice_reconciled=bool(telemetry.get("invoice_reconciled", False)),
            reported_models=telemetry.get("reported_models", []),
            finish_reasons=telemetry.get("finish_reasons", []),
            output_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            raw_output_persisted=False,
            normalization_repairs=all_repairs,
        )
        return row, value if accepted else None
    except Exception as error:
        row.update(
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            error=type(error).__name__,
            diagnostic=str(error),
            actual_cost_usd=0.0,
        )
        return row, None


def _fallback_route(node_id: str, primary: Mapping[str, str]) -> dict[str, str] | None:
    if primary["slot"] != "claude":
        return _route(
            "claude",
            "high" if node_id in BUDGET_CRITICAL_NODES else "medium",
        )
    if node_id in {
        "requirements_extract",
        "contract_truth",
        "media_analysis",
        "media_package",
    }:
        return _route("gpt", "low")
    if node_id in {"planner_a", "planner_b"}:
        return _route("gpt", "low")
    if node_id in {
        "deployment_plan",
        "rollback_plan",
        "security_review",
        "handoff_review",
        "incident_replan",
        "final_assembly",
    }:
        return _route("gpt", "high")
    return None


def _fallback_admission(
    *,
    node_id: str,
    policy_id: str,
    primary: Mapping[str, Any],
    fallback_route: Mapping[str, str],
    dependencies: Mapping[str, Mapping[str, Any]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    reveal_incident: bool,
) -> dict[str, Any]:
    """Admit one useful fallback only when its cost can be bounded."""

    category = _failure_category(primary)
    if category in {"hard_security", "budget"}:
        return {"admitted": False, "reason": f"non_retryable_{category}"}
    try:
        _, _, plan = _planned_call(
            node_id=node_id,
            policy_id=policy_id,
            route=fallback_route,
            dependencies=dependencies,
            providers=providers,
            store=store,
            attempt="fallback",
            reveal_incident=reveal_incident,
        )
        provider = str(plan["provider_id"])
        required = float(plan["required_work_order_cost_limit_usd"])
        headroom = store.budget_headroom(EXPERIMENT_ID, provider=provider)
        affordable = (
            required <= float(headroom["total_remaining_usd"]) + 1e-12
            and required <= float(headroom["provider_remaining_usd"]) + 1e-12
            and int(headroom["logical_calls_remaining"]) > 0
        )
        return {
            "admitted": affordable,
            "reason": "bounded_recovery_value" if affordable else "insufficient_headroom",
            "failure_category": category,
            "required_reservation_usd": required,
            "provider_id": provider,
        }
    except Exception as error:
        return {
            "admitted": False,
            "reason": "fallback_cost_unavailable",
            "failure_category": category,
            "planning_error": type(error).__name__,
        }


def _execute_node(
    *,
    node_id: str,
    policy_id: str,
    route: Mapping[str, str],
    dependencies: Mapping[str, Mapping[str, Any]],
    all_artifacts: Mapping[str, Mapping[str, Any]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    reveal_incident: bool,
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    if policy_id == COSTWEAVE_DYNAMIC:
        deterministic = _deterministic_artifact(node_id)
        if deterministic is not None:
            validation = validate_node(
                node_id,
                deterministic,
                all_artifacts,
                incident_revealed=reveal_incident,
            )
            if validation.accepted:
                encoded = json.dumps(
                    deterministic,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                return (
                    {
                        "node_id": node_id,
                        "policy_id": policy_id,
                        "attempt": "local_deterministic",
                        "slot": "local",
                        "reasoning_effort": "deterministic",
                        "accepted": True,
                        "status": "completed_local_deterministic",
                        "diagnostic": "accepted_without_model_call",
                        "checks": dict(validation.checks),
                        "actual_cost_usd": 0.0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "output_sha256": hashlib.sha256(encoded).hexdigest(),
                        "fallback_used": False,
                        "attempts": [],
                    },
                    deterministic,
                )
    primary, artifact = _execute_once(
        node_id=node_id,
        policy_id=policy_id,
        route=route,
        dependencies=dependencies,
        all_artifacts=all_artifacts,
        providers=providers,
        store=store,
        attempt="primary",
        reveal_incident=reveal_incident,
    )
    primary["fallback_used"] = False
    primary["attempts"] = [dict(primary)]
    if artifact is not None or policy_id != COSTWEAVE_DYNAMIC:
        return primary, artifact
    fallback_route = _fallback_route(node_id, route)
    if fallback_route is None:
        return primary, None
    fallback_decision = _fallback_admission(
        node_id=node_id,
        policy_id=policy_id,
        primary=primary,
        fallback_route=fallback_route,
        dependencies=dependencies,
        providers=providers,
        store=store,
        reveal_incident=reveal_incident,
    )
    primary["fallback_decision"] = fallback_decision
    if not fallback_decision["admitted"]:
        primary["fallback_status"] = "not_admitted"
        return primary, None
    fallback, fallback_artifact = _execute_once(
        node_id=node_id,
        policy_id=policy_id,
        route=fallback_route,
        dependencies=dependencies,
        all_artifacts=all_artifacts,
        providers=providers,
        store=store,
        attempt="fallback",
        reveal_incident=reveal_incident,
    )
    combined = dict(fallback)
    combined["fallback_used"] = True
    combined["fallback_status"] = "recovered" if fallback_artifact is not None else "failed"
    combined["fallback_decision"] = fallback_decision
    combined["primary_route"] = dict(route)
    combined["final_route"] = dict(fallback_route)
    combined["attempts"] = [primary, fallback]
    combined["actual_cost_usd"] = round(
        float(primary.get("actual_cost_usd") or 0)
        + float(fallback.get("actual_cost_usd") or 0),
        10,
    )
    combined["elapsed_ms"] = round(
        float(primary.get("elapsed_ms") or 0)
        + float(fallback.get("elapsed_ms") or 0),
        3,
    )
    return combined, fallback_artifact


def _virtual_dependencies(
    node_id: str, artifacts: Mapping[str, Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    base: dict[str, Mapping[str, Any]] = {
        "incident_command": artifacts["incident_replan"],
        "incident": INCIDENT_PACKET,
    }
    if node_id == "provider_selection_v2":
        base["provider_selection_v1"] = artifacts["provider_selection"]
    elif node_id == "deployment_plan_v2":
        base["deployment_plan_v1"] = artifacts["deployment_plan"]
    elif node_id == "security_review_v2":
        base["deployment_plan_v2"] = artifacts["deployment_plan_v2"]
    elif node_id == "financial_review_v2":
        base["provider_selection_v2"] = artifacts["provider_selection_v2"]
    return base


def _call_dependencies(
    node_id: str, artifacts: Mapping[str, Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    if node_id in VIRTUAL_SPECS:
        return _virtual_dependencies(node_id, artifacts)
    return {dep: artifacts[dep] for dep in REAL_NODES[node_id].dependencies}


def _run_ready_nodes(
    dependencies: Mapping[str, Sequence[str]],
    *,
    policy_id: str,
    routes: Mapping[str, Mapping[str, str]],
    artifacts: dict[str, Mapping[str, Any]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    max_workers: int,
    origin: float,
    rows: dict[str, dict[str, Any]],
    dispatches: list[list[str]],
    deferrals: list[dict[str, Any]],
) -> None:
    """Only the coordinator publishes accepted artifacts and releases successors."""
    pending = {node_id: deps for node_id, deps in dependencies.items()
               if node_id not in artifacts and node_id not in rows}
    running = {}
    active_plans: dict[str, Mapping[str, Any]] = {}
    ready_at: dict[str, float] = {}

    def execute(node_id, snapshot):
        started = time.perf_counter()
        row, artifact = _execute_node(
            node_id=node_id, policy_id=policy_id, route=routes[node_id],
            dependencies=_call_dependencies(node_id, snapshot), all_artifacts=snapshot,
            providers=providers, store=store,
            reveal_incident=node_id == "incident_replan" or node_id in VIRTUAL_SPECS,
        )
        finished = time.perf_counter()
        return {**row, "started_at_ms": (started - origin) * 1000,
                "finished_at_ms": (finished - origin) * 1000}, artifact

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"aurora-{policy_id}") as pool:
        while pending or running:
            ready = [node for node, deps in pending.items() if all(dep in artifacts for dep in deps)]
            now = (time.perf_counter() - origin) * 1000
            for node in ready:
                ready_at.setdefault(node, now)
            slots = max_workers - len(running)
            if ready and slots:
                snapshot = dict(artifacts)
                admitted, deferred, plans = _admit_runtime_wave(
                    runnable=ready, policy_id=policy_id, routes=routes, snapshot=snapshot,
                    providers=providers, store=store, max_workers=slots,
                    pending_reservations={
                        plan["task_id"]: (plan["provider_id"], plan["required_work_order_cost_limit_usd"])
                        for plan in active_plans.values()
                    },
                )
                if deferred:
                    deferrals.append({
                        "deferred": deferred, "admitted": admitted,
                        "required_reservations_usd": {
                            node: plan["required_work_order_cost_limit_usd"] for node, plan in plans.items()
                        },
                    })
                if admitted:
                    dispatches.append(admitted)
                    for node in admitted:
                        plan = plans.get(node, {})
                        if "task_id" in plan:
                            active_plans[node] = plan
                        running[pool.submit(execute, node, snapshot)] = node
                        del pending[node]
                elif not running:
                    # No outstanding reservation can settle and restore headroom.
                    for node in ready:
                        rows[node] = {
                            "node_id": node, "status": "blocked_budget_hard_cap", "accepted": False,
                            "actual_cost_usd": 0.0, "diagnostic": "no_single_call_reservation_fits_remaining_budget",
                            **routes[node],
                        }
                        del pending[node]
            if not running:
                break
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                node = running.pop(future)
                row, artifact = future.result()
                active_plans.pop(node, None)
                row["ready_at_ms"] = ready_at[node]
                rows[node] = row
                if artifact is not None:
                    artifacts[node] = artifact
    for node, deps in pending.items():
        rows[node] = {
            "node_id": node, "status": "skipped_missing_dependencies", "accepted": False,
            "missing_dependencies": sorted(set(deps) - artifacts.keys()), **routes[node],
        }


def _run_virtual_revisions(
    *,
    policy_id: str,
    routes: Mapping[str, Mapping[str, str]],
    artifacts: dict[str, Mapping[str, Any]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    origin: float | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    dependencies = {
        "provider_selection_v2": ("incident_replan", "provider_selection"),
        "deployment_plan_v2": ("incident_replan", "deployment_plan"),
        "security_review_v2": ("incident_replan", "deployment_plan_v2"),
        "financial_review_v2": ("incident_replan", "provider_selection_v2"),
    }
    outcomes: dict[str, dict[str, Any]] = {}
    _run_ready_nodes(
        dependencies, policy_id=policy_id, routes=routes, artifacts=artifacts, providers=providers,
        store=store, max_workers=2, origin=time.perf_counter() if origin is None else origin,
        rows=outcomes, dispatches=[], deferrals=[],
    )
    rows = [outcomes[node] for node in dependencies if node in outcomes]
    if not all(node in artifacts for node in dependencies):
        return rows, False
    command = dict(artifacts["incident_replan"])
    command["provider_plan_v2"] = dict(artifacts["provider_selection_v2"])
    command["deployment_plan_v2"] = dict(artifacts["deployment_plan_v2"])
    command["security_review_v2"] = dict(artifacts["security_review_v2"])
    command["financial_review_v2"] = dict(artifacts["financial_review_v2"])
    validation = validate_node("incident_replan", command, artifacts, incident_revealed=True)
    if validation.accepted:
        artifacts["incident_replan"] = command
    return rows, validation.accepted


def _load_prior_checkpoints(
    root: Path,
) -> dict[str, dict[str, Mapping[str, Any]]]:
    """Revalidate earlier paid outputs and reuse only accepted artifacts."""

    result = {policy_id: {} for policy_id in POLICIES}
    contents: dict[str, Mapping[str, Any]] = {}
    for version in (
        "v2",
        "v3",
        "v4",
        "v5",
        "v6",
        "v7",
        "v8",
        "v9",
        "v10-clean",
    ):
        path = root / f"work/local-data/aurora-real-pilot-{version}.sqlite3"
        if not path.is_file():
            continue
        with sqlite3.connect(path) as connection:
            rows = connection.execute(
                "SELECT task_id,result_json FROM external_calls WHERE status='completed'"
            ).fetchall()
        for task_id, raw in rows:
            try:
                envelope = json.loads(str(raw))
                content = str(
                    envelope.get("result", {})
                    .get("output", {})
                    .get("content", "")
                )
                value = _extract_json_object(content)
            except (TypeError, ValueError, json.JSONDecodeError):
                value = None
            if value is not None:
                contents[str(task_id)] = value
    for policy_id in POLICIES:
        artifacts = result[policy_id]
        for stage in STAGES:
            attempted: set[str] = set()
            while True:
                snapshot = dict(artifacts)
                runnable = [
                    node_id
                    for node_id in stage
                    if node_id not in artifacts
                    and node_id not in attempted
                    and f"task_aurora_{policy_id}_{node_id}_primary" in contents
                    and all(
                        dependency in snapshot
                        for dependency in REAL_NODES[node_id].dependencies
                    )
                ]
                if not runnable:
                    break
                for node_id in runnable:
                    attempted.add(node_id)
                    task_id = f"task_aurora_{policy_id}_{node_id}_primary"
                    value = contents[task_id]
                    try:
                        validation = validate_node(
                            node_id,
                            value,
                            snapshot,
                            incident_revealed=node_id == "incident_replan",
                        )
                    except (TypeError, ValueError, KeyError):
                        continue
                    if validation.accepted:
                        artifacts[node_id] = value
    return result


def _load_virtual_verification_checkpoints(root: Path) -> dict[str, bool]:
    accepted = {policy_id: set() for policy_id in POLICIES}
    for version in (
        "v2",
        "v3",
        "v4",
        "v5",
        "v6",
        "v7",
        "v8",
        "v9",
        "v10-clean",
    ):
        path = (
            root
            / "work/local-data/real-eval"
            / f"aurora-real-pilot-{version}-result.json"
        )
        if not path.is_file():
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        for policy in report.get("policy_results", []):
            policy_id = str(policy.get("policy_id", ""))
            if policy_id not in accepted:
                continue
            accepted[policy_id].update(
                str(row.get("node_id"))
                for row in policy.get("nodes", [])
                if row.get("node_id") in VIRTUAL_SPECS
                and row.get("accepted") is True
            )
    required = set(VIRTUAL_SPECS)
    return {
        policy_id: required <= node_ids
        for policy_id, node_ids in accepted.items()
    }


def _overlap_ms(rows: Sequence[Mapping[str, Any]]) -> float:
    """Return concurrent worker time, excluding idle gaps between independent branches."""
    intervals = sorted((row["started_at_ms"], row["finished_at_ms"])
                       for row in rows if "started_at_ms" in row)
    overlap = 0.0
    busy_until = 0.0
    for started, finished in intervals:
        overlap += max(0.0, min(busy_until, finished) - started)
        busy_until = max(busy_until, finished)
    return overlap


def _execute_policy(
    *,
    policy_id: str,
    routes: Mapping[str, Mapping[str, str]],
    providers: Mapping[str, Mapping[str, Any]],
    store: ExperimentalCallStore,
    seed_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    terminal_failures: Mapping[str, str] | None = None,
    virtual_revisions_verified: bool = False,
) -> dict[str, Any]:
    artifacts: dict[str, Mapping[str, Any]] = {}
    checkpoints = dict(seed_artifacts or {})
    learned_failures = dict(terminal_failures or {})
    outcomes: dict[str, dict[str, Any]] = {}
    dispatches: list[list[str]] = []
    deferrals: list[dict[str, Any]] = []
    virtual_rows: list[dict[str, Any]] = []
    verified_checkpoint = False
    started = time.perf_counter()

    # The frozen incident is disclosed only after all pre-incident work has
    # settled. All other stage numbers describe the report, not scheduling.
    release_stage = INCIDENT_PACKET["release_stage"]
    before_reveal = [node.node_id for node in NODES if node.stage < release_stage]
    after_reveal = [node.node_id for node in NODES if node.stage >= release_stage
                    and node.node_id != "incident_replan"]
    max_workers = max(len(stage) for stage in STAGES)
    for phase in (before_reveal, ["incident_replan"], after_reveal):
        for node in phase:
            artifact = checkpoints.get(node)
            deps = REAL_NODES[node].dependencies
            if artifact is not None and all(dep in artifacts for dep in deps):
                validation = validate_node(
                    node, artifact, artifacts, incident_revealed=node in {"incident_replan", "final_assembly"}
                )
                if validation.accepted:
                    artifacts[node] = artifact
                    outcomes[node] = {
                        "node_id": node, "status": "reused_prior_checkpoint", "accepted": True,
                        "actual_cost_usd": 0.0, **routes[node],
                        "checkpoint_sha256": hashlib.sha256(json.dumps(
                            artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ).encode("utf-8")).hexdigest(),
                    }
            if node not in artifacts and node in learned_failures:
                outcomes[node] = {
                    "node_id": node, "status": "skipped_learned_terminal_failure", "accepted": False,
                    "actual_cost_usd": 0.0, "reason": learned_failures[node], **routes[node],
                }

        _run_ready_nodes(
            {node: REAL_NODES[node].dependencies for node in phase},
            policy_id=policy_id, routes=routes, artifacts=artifacts, providers=providers,
            store=store, max_workers=max_workers, origin=started, rows=outcomes,
            dispatches=dispatches, deferrals=deferrals,
        )
        if phase == ["incident_replan"] and "incident_replan" in artifacts:
            verified_checkpoint = virtual_revisions_verified and (
                outcomes["incident_replan"]["status"] == "reused_prior_checkpoint"
            )
            if not verified_checkpoint:
                virtual_rows, accepted = _run_virtual_revisions(
                    policy_id=policy_id, routes=routes, artifacts=artifacts,
                    providers=providers, store=store, origin=started,
                )
                if not accepted:
                    del artifacts["incident_replan"]

    stage_rows = []
    for stage in STAGES:
        waves = [[node for node in batch if node in stage] for batch in dispatches]
        runnable = [node for batch in waves for node in batch]
        timings = [outcomes[node] for node in runnable]
        wall_ms = (max(row["finished_at_ms"] for row in timings)
                   - min(row["started_at_ms"] for row in timings)) if timings else 0.0
        sum_ms = sum(row["finished_at_ms"] - row["started_at_ms"] for row in timings)
        stage_rows.append({
            "stage": REAL_NODES[stage[0]].stage, "nodes": list(stage),
            "runnable": runnable, "waves": [batch for batch in waves if batch],
            "budget_deferrals": [item for item in deferrals if any(n in stage for n in item["deferred"])],
            "reused_checkpoints": [n for n in stage if outcomes[n]["status"] == "reused_prior_checkpoint"],
            "skipped": [n for n in stage if outcomes[n]["status"].startswith("skipped")],
            "wall_ms": round(wall_ms, 3), "sum_node_elapsed_ms": round(sum_ms, 3),
            "observed_parallel_overlap_ms": round(_overlap_ms(timings), 3),
        })
    rows = [outcomes[node] for node in before_reveal] + [outcomes["incident_replan"], *virtual_rows]
    rows.extend(outcomes[node] for node in after_reveal)
    final = artifacts.get("final_assembly")
    final_validation = validate_node("final_assembly", final, artifacts, incident_revealed=True) if final else None
    final_accepted = bool(final_validation and final_validation.accepted)
    actual_cost = round(sum(float(row.get("actual_cost_usd") or 0) for row in rows), 10)
    return {
        "policy_id": policy_id,
        "accepted": final_accepted,
        "final_repair_checkpoint": terminal_checkpoint(policy_id, rows, final_accepted=final_accepted),
        "oracle_score": round(100 * sum(1 for node in NODES if node.node_id in artifacts) / len(NODES), 3),
        "hard_gates_passed": final_accepted,
        "actual_cost_usd": actual_cost,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "accepted_primary_artifacts": sum(node.node_id in artifacts for node in NODES),
        "reused_checkpoint_artifacts": sum(
            row.get("status") == "reused_prior_checkpoint" for row in rows
        ),
        "completed_model_calls": sum(row.get("status") == "completed" for row in rows),
        "fallback_attempts": sum(bool(row.get("fallback_used")) for row in rows),
        "fallback_recoveries": sum(row.get("fallback_status") == "recovered" for row in rows),
        "virtual_reverification_skipped": verified_checkpoint,
        "scheduling_policy": "dependency_ready",
        "parallel_execution_observed": _overlap_ms(rows) > 0,
        "stages": stage_rows,
        "nodes": rows,
        "final_output": dict(final) if final else None,
    }


def _oracle_dependencies(node_id: str) -> dict[str, Mapping[str, Any]]:
    if node_id in REAL_NODES:
        return {dep: oracle_artifact(dep) for dep in REAL_NODES[node_id].dependencies}
    if node_id == "provider_selection_v2":
        return {"incident_command": oracle_artifact("incident_replan"), "incident": INCIDENT_PACKET, "provider_selection_v1": oracle_artifact("provider_selection")}
    if node_id == "deployment_plan_v2":
        return {"incident_command": oracle_artifact("incident_replan"), "incident": INCIDENT_PACKET, "deployment_plan_v1": oracle_artifact("deployment_plan")}
    if node_id == "security_review_v2":
        return {"incident_command": oracle_artifact("incident_replan"), "incident": INCIDENT_PACKET, "deployment_plan_v2": oracle_artifact("incident_replan")["deployment_plan_v2"]}
    return {"incident_command": oracle_artifact("incident_replan"), "incident": INCIDENT_PACKET, "provider_selection_v2": oracle_artifact("incident_replan")["provider_plan_v2"]}


def _ledger_policy_metrics(
    runtime_path: Path,
    policy_results: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Attribute every billed call, including transport failures, to a policy."""

    connection = sqlite3.connect(runtime_path)
    try:
        ledger_rows = connection.execute(
            "SELECT task_id,status,actual_cost FROM external_calls"
        ).fetchall()
        try:
            billing_rows = connection.execute(
                "SELECT ec.external_call_id,ec.task_id,ec.provider,ec.model_id,"
                "ec.actual_cost,ec.telemetry_json,a.ordinal,a.input_tokens,"
                "a.output_tokens,a.cached_input_tokens,a.actual_cost,a.recorded_at "
                "FROM external_calls ec LEFT JOIN external_call_attempts a "
                "ON a.external_call_id=ec.external_call_id"
            ).fetchall()
        except sqlite3.OperationalError:
            # Compatibility for old/minimal ledgers that predate attempt-level
            # provider telemetry. They remain valid for reference-cost tests,
            # but cannot make a defensible CNY invoice estimate.
            billing_rows = []
    finally:
        connection.close()
    billing_calibration = load_billing_calibration()
    invoices: dict[tuple[str, int], Mapping[str, Any]] = {}
    known_attempts = {(str(row[0]), int(row[6] or 0)): str(row[2]) for row in billing_rows}
    for invoice in load_call_invoices(runtime_path):
        key = (invoice.get("external_call_id"), invoice.get("attempt_ordinal"))
        if key not in known_attempts or known_attempts[key] != invoice.get("provider_id") or invoice.get("currency") != "CNY":
            raise BillingCalibrationError("逐调用账单不能匹配当前账本")
        if key in invoices and invoices[key] != invoice:
            raise BillingCalibrationError("逐调用账单重复且不一致")
        invoices[key] = invoice
    metrics: dict[str, dict[str, Any]] = {}
    for policy_id in POLICIES:
        prefix = f"task_aurora_{policy_id}_"
        owned = [row for row in ledger_rows if str(row[0]).startswith(prefix)]
        owned_billing = [
            row for row in billing_rows if str(row[1]).startswith(prefix)
        ]
        ledger_cost = round(sum(float(row[2]) for row in owned), 10)
        billed_estimates = []
        per_call_billing: list[dict[str, Any]] = []
        attempt_counts: dict[str, int] = {}
        for row in owned_billing:
            attempt_counts[str(row[0])] = attempt_counts.get(str(row[0]), 0) + 1
        for billing_row in owned_billing:
            try:
                telemetry = json.loads(str(billing_row[5] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                telemetry = {}
            if not isinstance(telemetry, Mapping):
                telemetry = {}
            invoice = invoices.get((str(billing_row[0]), int(billing_row[6] or 0)))
            # Legacy call-level telemetry is usable for one attempt only.
            # Never repeat an ambiguous logical-call invoice over all retries.
            if invoice is not None:
                telemetry = {**telemetry, **invoice}
            elif attempt_counts[str(billing_row[0])] > 1:
                telemetry = {}
            cached_tokens = int(billing_row[9] or 0)
            cache_read_tokens = int(
                telemetry.get("cache_read_input_tokens", cached_tokens) or 0
            )
            cache_creation_tokens = int(
                telemetry.get("cache_creation_input_tokens", 0) or 0
            )
            estimate = estimate_billed_cost_cny(
                str(billing_row[2]),
                reference_cost_usd=float(
                    billing_row[10]
                    if billing_row[10] is not None
                    else billing_row[4]
                ),
                input_tokens=int(billing_row[7] or 0),
                output_tokens=int(billing_row[8] or 0),
                cached_input_tokens=cached_tokens,
                cache_read_input_tokens=cache_read_tokens,
                cache_creation_input_tokens=cache_creation_tokens,
                occurred_at=billing_row[11],
                calibration=billing_calibration,
            )
            billed_estimates.append(estimate)
            task_id = str(billing_row[1])
            task_suffix = task_id[len(prefix) :]
            attempt_name = (
                "fallback" if task_suffix.endswith("_fallback") else "primary"
            )
            node_id = task_suffix.removesuffix(f"_{attempt_name}")
            provider_billed = telemetry.get("provider_billed_cost_cny")
            if isinstance(provider_billed, bool):
                raise BillingCalibrationError("逐调用费用不能是布尔值")
            provider_billed_cny = (
                float(provider_billed) if provider_billed is not None else None
            )
            effective = (reconcile_provider_invoice(
                str(billing_row[2]), estimated_cny=estimate.amount_cny,
                provider_observed_cny=provider_billed_cny,
            ).final_billed_cny if provider_billed_cny is not None else estimate.amount_cny)
            per_call_billing.append(
                {
                    "external_call_id": str(billing_row[0]),
                    "task_id": task_id,
                    "node_id": node_id,
                    "attempt": attempt_name,
                    "attempt_ordinal": int(billing_row[6] or 0),
                    "provider_id": str(billing_row[2]),
                    "model_id": str(billing_row[3]),
                    "input_tokens": int(billing_row[7] or 0),
                    "output_tokens": int(billing_row[8] or 0),
                    "cached_input_tokens": cached_tokens,
                    "cache_read_input_tokens": cache_read_tokens,
                    "cache_creation_input_tokens": cache_creation_tokens,
                    "estimated_cache_read_cost_cny": (
                        estimate.estimated_cache_read_cost_cny
                    ),
                    "estimated_cache_creation_cost_cny": (
                        estimate.estimated_cache_creation_cost_cny
                    ),
                    "provider_billed_cache_read_cost_cny": telemetry.get(
                        "provider_billed_cache_read_cost_cny"
                    ),
                    "provider_billed_cache_creation_cost_cny": telemetry.get(
                        "provider_billed_cache_creation_cost_cny"
                    ),
                    "reference_list_cost_usd": float(
                        billing_row[10]
                        if billing_row[10] is not None
                        else billing_row[4]
                    ),
                    "estimated_billed_cost_cny": estimate.amount_cny,
                    "estimated_billed_cost_cny_lower": estimate.lower_cny,
                    "estimated_billed_cost_cny_upper": estimate.upper_cny,
                    "provider_billed_cost_cny": provider_billed_cny,
                    "effective_billed_cost_cny": effective,
                    "effective_cost_source": "provider_invoice" if provider_billed_cny is not None else "estimate",
                    "effective_cost_lower_cny": effective if provider_billed_cny is not None else estimate.lower_cny,
                    "effective_cost_upper_cny": effective if provider_billed_cny is not None else estimate.upper_cny,
                    "billing_residual_cny": (
                        round(provider_billed_cny - estimate.amount_cny, 10)
                        if provider_billed_cny is not None
                        else None
                    ),
                    "billing_currency": "CNY",
                    "invoice_reconciled": provider_billed_cny is not None,
                }
            )
        billed_by_provider: dict[str, float] = {}
        for estimate in billed_estimates:
            billed_by_provider[estimate.provider_id] = round(
                billed_by_provider.get(estimate.provider_id, 0.0)
                + estimate.amount_cny,
                10,
            )
        result = next(
            (row for row in policy_results if row.get("policy_id") == policy_id),
            {},
        )
        attempts: list[Mapping[str, Any]] = []
        for node_row in result.get("nodes", []):
            node_attempts = node_row.get("attempts")
            attempts.extend(node_attempts if node_attempts else [node_row])
        reported_attempt_cost = round(
            sum(float(row.get("actual_cost_usd") or 0) for row in attempts), 10
        )
        rejected_attempt_cost = round(
            sum(
                float(row.get("actual_cost_usd") or 0)
                for row in attempts
                if not row.get("accepted")
            ),
            10,
        )
        unreported_transport_cost = round(
            max(0.0, ledger_cost - reported_attempt_cost), 10
        )
        leakage_cost = round(
            min(ledger_cost, rejected_attempt_cost + unreported_transport_cost),
            10,
        )
        primary_attempts = [row for row in attempts if row.get("attempt") == "primary"]
        metrics[policy_id] = {
            "effective_billed_cost_cny": round(sum(row["effective_billed_cost_cny"] for row in per_call_billing), 10),
            "effective_cost_lower_cny": round(sum(row["effective_cost_lower_cny"] for row in per_call_billing), 10),
            "effective_cost_upper_cny": round(sum(row["effective_cost_upper_cny"] for row in per_call_billing), 10),
            "effective_billed_cost_cny_by_provider": {
                provider: round(sum(row["effective_billed_cost_cny"] for row in per_call_billing if row["provider_id"] == provider), 10)
                for provider in billed_by_provider
            },
            "invoice_attempt_coverage": sum(row["invoice_reconciled"] for row in per_call_billing) / len(per_call_billing) if per_call_billing else 0.0,
            "cost_evidence_complete": len(owned_billing) >= len(owned) and all(row[6] is not None for row in owned_billing),
            "ledger_actual_cost_usd": ledger_cost,
            "ledger_cost_semantics": "conservative_reference_list_price_not_invoice",
            "estimated_billed_cost_cny": round(
                sum(row.amount_cny for row in billed_estimates), 10
            ),
            "estimated_billed_cost_cny_lower": round(
                sum(row.lower_cny for row in billed_estimates), 10
            ),
            "estimated_billed_cost_cny_upper": round(
                sum(row.upper_cny for row in billed_estimates), 10
            ),
            "estimated_billed_cost_cny_by_provider": billed_by_provider,
            "per_call_billing": per_call_billing,
            "provider_invoice_reconciled": bool(per_call_billing)
            and all(row["invoice_reconciled"] for row in per_call_billing),
            "ledger_calls": len(owned),
            "ledger_terminal_statuses": {
                status: sum(str(row[1]) == status for row in owned)
                for status in ("completed", "failed", "cancelled", "reserved")
            },
            "reported_attempt_cost_usd": reported_attempt_cost,
            "rejected_attempt_cost_usd": rejected_attempt_cost,
            "unreported_transport_cost_usd": unreported_transport_cost,
            "estimated_cost_leakage_usd": leakage_cost,
            "estimated_useful_model_cost_usd": round(
                max(0.0, ledger_cost - leakage_cost), 10
            ),
            "fallback_cost_usd": round(
                sum(
                    float(row.get("actual_cost_usd") or 0)
                    for row in attempts
                    if row.get("attempt") == "fallback"
                ),
                10,
            ),
            "wasted_output_tokens": sum(
                int(row.get("output_tokens") or 0)
                for row in attempts
                if not row.get("accepted")
            ),
            "first_pass_acceptance_rate": (
                round(
                    sum(bool(row.get("accepted")) for row in primary_attempts)
                    / len(primary_attempts),
                    6,
                )
                if primary_attempts
                else None
            ),
        }
    return metrics


def run_preflight(
    *,
    root: Path,
    budget_usd: float,
    budget_cny: float | None = None,
    registry_path: Path | None = None,
    fresh_replay: bool = False,
) -> dict[str, Any]:
    offline = run_offline_suite()
    provider_list = _load_registry(registry_path or root / DEFAULT_REGISTRY_FILE)
    providers = {str(item["provider_id"]): item for item in provider_list}
    missing = sorted(set(PROVIDER_BY_SLOT.values()) - set(providers))
    secret_missing = sorted(
        provider_id
        for provider_id, provider in providers.items()
        if provider_id in PROVIDER_BY_SLOT.values() and not os.environ.get(str(provider["secret_env"]))
    )
    routes = freeze_routes()
    checkpoints = (
        {policy_id: {} for policy_id in POLICIES}
        if fresh_replay
        else _load_prior_checkpoints(root)
    )
    virtual_verified = (
        {policy_id: False for policy_id in POLICIES}
        if fresh_replay
        else _load_virtual_verification_checkpoints(root)
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "preflight.sqlite3"
        EventStore(path)
        store = ExperimentalCallStore(path)
        rows: list[dict[str, Any]] = []
        planned_by_policy: dict[str, set[str]] = {
            policy_id: set() for policy_id in POLICIES
        }
        for policy_id in POLICIES:
            available = set(checkpoints[policy_id])
            blocked = (
                set()
                if fresh_replay
                else set(LEARNED_TERMINAL_FAILURES.get(policy_id, {}))
            )
            progress = True
            while progress:
                progress = False
                for node in NODES:
                    if node.node_id in available or node.node_id in blocked:
                        continue
                    if not set(node.dependencies) <= available:
                        continue
                    planned_by_policy[policy_id].add(node.node_id)
                    available.add(node.node_id)
                    progress = True
            for node_id in (
                node.node_id
                for node in NODES
                if node.node_id in planned_by_policy[policy_id]
            ):
                if (
                    policy_id == COSTWEAVE_DYNAMIC
                    and _deterministic_artifact(node_id) is not None
                ):
                    rows.append(
                        {
                            "required_work_order_cost_limit_usd": 0.0,
                            "node_id": node_id,
                            "policy_id": policy_id,
                            "provider_id": "local-deterministic",
                            "requested_model": None,
                            "reasoning_effort": "deterministic",
                            "affordable": True,
                        }
                    )
                    continue
                _, _, plan = _planned_call(
                    node_id=node_id,
                    policy_id=policy_id,
                    route=routes[policy_id][node_id],
                    dependencies=_oracle_dependencies(node_id),
                    providers=providers,
                    store=store,
                    reveal_incident=node_id == "incident_replan",
                )
                rows.append(plan)
            if "incident_replan" in available and not virtual_verified[policy_id]:
                for node_id in VIRTUAL_SPECS:
                    _, _, plan = _planned_call(
                        node_id=node_id,
                        policy_id=policy_id,
                        route=routes[policy_id][node_id],
                        dependencies=_oracle_dependencies(node_id),
                        providers=providers,
                        store=store,
                        reveal_incident=True,
                    )
                    rows.append(plan)
        fallback_rows: list[dict[str, Any]] = []
        for node in NODES:
            if node.node_id not in planned_by_policy[COSTWEAVE_DYNAMIC]:
                continue
            if _deterministic_artifact(node.node_id) is not None:
                continue
            primary_route = routes[COSTWEAVE_DYNAMIC][node.node_id]
            fallback_route = _fallback_route(node.node_id, primary_route)
            if fallback_route is None:
                continue
            _, _, plan = _planned_call(
                node_id=node.node_id,
                policy_id=COSTWEAVE_DYNAMIC,
                route=fallback_route,
                dependencies=_oracle_dependencies(node.node_id),
                providers=providers,
                store=store,
                attempt="fallback",
                reveal_incident=node.node_id == "incident_replan",
            )
            fallback_rows.append(plan)
        rows.extend(fallback_rows)
    budget_gate = _preflight_budget_gate(rows, budget_usd=budget_usd)
    worst_total = float(budget_gate["projected_worst_case_usd"])
    provider_worst = {
        provider: round(sum(float(row["required_work_order_cost_limit_usd"]) for row in rows if row["provider_id"] == provider), 10)
        for provider in PROVIDER_BY_SLOT.values()
    }
    reused_counts = {
        policy_id: len(checkpoints[policy_id]) for policy_id in POLICIES
    }
    planned_local_calls = sum(
        row["provider_id"] == "local-deterministic" for row in rows
    )
    planned_network_calls = len(rows) - planned_local_calls
    return {
        "passed": bool(
            offline["paid_execution_ready"]
            and not missing
            and not secret_missing
            and budget_gate["hard_gate_passed"]
        ),
        "offline_ready": offline["paid_execution_ready"],
        "fresh_replay": fresh_replay,
        "providers_missing": missing,
        "secrets_missing": secret_missing,
        "reused_checkpoint_artifacts": reused_counts,
        "learned_terminal_failures": {
            policy_id: (
                {}
                if fresh_replay
                else dict(LEARNED_TERMINAL_FAILURES.get(policy_id, {}))
            )
            for policy_id in POLICIES
        },
        "virtual_verification_checkpoints": virtual_verified,
        "planned_primary_calls": (
            planned_network_calls - len(fallback_rows)
        ),
        "planned_local_deterministic_calls": planned_local_calls,
        "planned_fallback_calls": len(fallback_rows),
        "planned_maximum_calls": planned_network_calls,
        "worst_case_total_usd": worst_total,
        "largest_single_reservation_usd": budget_gate[
            "largest_single_reservation_usd"
        ],
        "staged_admission_required": budget_gate["staged_admission_required"],
        "soft_warnings": budget_gate["soft_warnings"],
        "budget_gate_policy": {
            "hard": "actual spend plus active atomic reservations must remain within aggregate and provider caps",
            "preflight": "every individual call must fit; the all-fallback projection is advisory",
            "runtime": "admit dependency-ready nodes as workers finish and retry budget deferrals after settlement",
        },
        "provider_worst_case_usd": provider_worst,
        "authorized_budget_usd": budget_usd,
        "authorized_budget_cny": budget_cny,
        "paid_execution_authorized": False,
        "reported_provider_balances_cny": {
            "deepseek_official": DEEPSEEK_REPORTED_BALANCE_CNY,
            "traveltoken_gpt_and_claude_combined": RELAY_REPORTED_BALANCE_CNY,
        },
        "projected_worst_case_headroom_usd": round(
            budget_usd - worst_total, 10
        ),
        "headroom_usd": round(
            budget_usd - float(budget_gate["largest_single_reservation_usd"]),
            10,
        ),
        "models": {provider_id: {"requested_model": provider["selected_model"], "trust": provider.get("trust")} for provider_id, provider in providers.items() if provider_id in PROVIDER_BY_SLOT.values()},
        "rows": rows,
        "network_calls": 0,
        "paid_cost_usd": 0.0,
    }


def run_real(
    *,
    root: Path,
    budget_usd: float,
    budget_cny: float | None = None,
    registry_path: Path | None = None,
    fresh_replay: bool = False,
) -> dict[str, Any]:
    if not fresh_replay:
        raise AuroraRealPilotError(
            "Aurora quality verification requires --fresh-replay"
        )
    if not math.isfinite(budget_usd) or not 0 < budget_usd <= MAX_BUDGET_USD:
        raise AuroraRealPilotError(
            "Aurora reference budget must be positive and within the 4.90 USD ceiling"
        )
    if (
        budget_cny is None
        or not math.isfinite(float(budget_cny))
        or float(budget_cny) <= 0
        or float(budget_cny)
        > DEEPSEEK_REPORTED_BALANCE_CNY + RELAY_REPORTED_BALANCE_CNY
    ):
        raise AuroraRealPilotError(
            "a positive explicitly authorized CNY hard cap within reported balances is required"
        )
    safe_reference_cap = min(
        MAX_BUDGET_USD,
        float(budget_cny) / CNY_HARD_CAP_REFERENCE_MULTIPLIER,
    )
    if budget_usd > safe_reference_cap + 1e-12:
        raise AuroraRealPilotError(
            "reference budget exceeds the conservative bound derived from the CNY hard cap"
        )
    if os.environ.get("COSTWEAVE_EXPERIMENTAL_ADAPTERS") != "1" or os.environ.get("COSTWEAVE_PAID_CALLS") != "1" or os.environ.get(PAID_AUTHORIZATION_ENV) != "1":
        raise AuroraRealPilotError("three explicit paid Aurora switches are required")
    preflight = run_preflight(
        root=root,
        budget_usd=budget_usd,
        budget_cny=budget_cny,
        registry_path=registry_path,
        fresh_replay=fresh_replay,
    )
    if not preflight["passed"]:
        raise AuroraRealPilotError(
            "zero-call Aurora preflight failed a hard prerequisite; "
            "soft budget projections do not block execution"
        )
    provider_list = _load_registry(registry_path or root / DEFAULT_REGISTRY_FILE)
    providers = {str(item["provider_id"]): item for item in provider_list}
    routes = freeze_routes()
    output_dir = root / "work/local-data/real-eval"
    suffix = PILOT_SUFFIX
    frozen_path = output_dir / f"aurora-real-pilot-{suffix}-frozen.json"
    report_path = output_dir / f"aurora-real-pilot-{suffix}-result.json"
    runtime_path = root / f"work/local-data/aurora-real-pilot-{suffix}.sqlite3"
    if any(path.exists() for path in (runtime_path, report_path, frozen_path)):
        raise AuroraRealPilotError(
            f"Aurora {suffix} result already exists; refusing an accidental paid replay"
        )
    checkpoints = (
        {policy_id: {} for policy_id in POLICIES}
        if fresh_replay
        else _load_prior_checkpoints(root)
    )
    virtual_verified = (
        {policy_id: False for policy_id in POLICIES}
        if fresh_replay
        else _load_virtual_verification_checkpoints(root)
    )
    frozen = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "run_id": RUN_ID,
        "checkpoint_schema_version": CHECKPOINT_VERSION,
        "scheduling_policy": "dependency_ready",
        "source_sha256": {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("aurora_real_pilot.py", "aurora_checkpoint.py", "aurora_rescue_eval.py", "experimental_calls.py")
        },
        "created_at": utc_now(),
        "validator_version": VALIDATOR_VERSION,
        "task": frozen_task_record(),
        "routes": routes,
        "checkpoint_counts": {
            policy_id: len(checkpoints[policy_id]) for policy_id in POLICIES
        },
        "preflight_without_secrets": {key: value for key, value in preflight.items() if key not in {"rows"}},
        "paid_calls_started": False,
    }
    frozen_sha256 = _publish_new_json(frozen_path, frozen)
    EventStore(runtime_path)
    store = ExperimentalCallStore(runtime_path)
    # The aggregate ledger is the authoritative hard cap. Giving every provider
    # access to that same ceiling avoids false denials when a fallback shifts the
    # realized provider mix; it does not multiply the aggregate authorization.
    provider_limits = {
        provider: budget_usd for provider in PROVIDER_BY_SLOT.values()
    }
    store.register_experiment(
        ExperimentBudget(
            experiment_id=EXPERIMENT_ID,
            total_cost_limit_usd=budget_usd,
            provider_cost_limits_usd=provider_limits,
            max_logical_calls=MAX_LOGICAL_CALLS,
            max_attempts_per_call=1,
            purpose="authorized_pre_v07_aurora_three_policy_real_parallel_pilot",
            expires_at=(datetime.now(UTC) + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
        )
    )
    order = list(POLICIES)
    random.Random(RANDOM_SEED).shuffle(order)
    results = [
        _execute_policy(
            policy_id=policy_id,
            routes=routes[policy_id],
            providers=providers,
            store=store,
            seed_artifacts=checkpoints[policy_id],
            terminal_failures=(
                {}
                if fresh_replay
                else LEARNED_TERMINAL_FAILURES.get(policy_id, {})
            ),
            virtual_revisions_verified=virtual_verified[policy_id],
        )
        for policy_id in order
    ]
    store.complete_experiment(EXPERIMENT_ID)
    ledger = store.experiment_summary(EXPERIMENT_ID)
    policy_ledger_metrics = _ledger_policy_metrics(runtime_path, results)
    for row in results:
        row["reported_row_cost_usd"] = row["actual_cost_usd"]
        row["cost_leakage"] = policy_ledger_metrics[row["policy_id"]]
        row["actual_cost_usd"] = policy_ledger_metrics[row["policy_id"]][
            "ledger_actual_cost_usd"
        ]
        row["estimated_billed_cost_cny"] = policy_ledger_metrics[
            row["policy_id"]
        ]["estimated_billed_cost_cny"]
        row["effective_billed_cost_cny"] = policy_ledger_metrics[row["policy_id"]]["effective_billed_cost_cny"]
        row["cost_comparison_source"] = "matched_invoice_else_estimate_CNY"
    by_policy = {row["policy_id"]: row for row in results}
    premium_cost = float(by_policy[ALL_PREMIUM]["effective_billed_cost_cny"])
    premium_elapsed = float(by_policy[ALL_PREMIUM]["elapsed_ms"])
    for row in results:
        complete = policy_ledger_metrics[row["policy_id"]]["cost_evidence_complete"] and policy_ledger_metrics[ALL_PREMIUM]["cost_evidence_complete"]
        row["cost_reduction_vs_all_premium"] = round((premium_cost - float(row["effective_billed_cost_cny"])) / premium_cost, 6) if premium_cost and complete else None
        row["latency_reduction_vs_all_premium"] = round((premium_elapsed - float(row["elapsed_ms"])) / premium_elapsed, 6) if premium_elapsed else None
    report = {
        "schema_version": SCHEMA_VERSION,
        "frozen_sha256": frozen_sha256,
        "created_at": utc_now(),
        "experiment_id": EXPERIMENT_ID,
        "run_id": RUN_ID,
        "task_version": AURORA_TASK_VERSION,
        "fresh_replay": fresh_replay,
        "authorized_budget_cny": budget_cny,
        "cny_hard_cap_reference_multiplier": CNY_HARD_CAP_REFERENCE_MULTIPLIER,
        "aggregate_authorized_budget_usd": AGGREGATE_AUTHORIZED_BUDGET_USD,
        "prior_internal_cost_usd": PRIOR_INTERNAL_COST_USD,
        "prior_internal_cost_semantics": "legacy_conservative_reference_not_invoice",
        "prior_reconciled_billed_cost_cny": PRIOR_RECONCILED_BILLED_COST_CNY,
        "authorized_budget_usd": budget_usd,
        "reported_provider_balances_cny": {
            "deepseek_official": DEEPSEEK_REPORTED_BALANCE_CNY,
            "traveltoken_gpt_and_claude_combined": RELAY_REPORTED_BALANCE_CNY,
        },
        "preflight": {key: value for key, value in preflight.items() if key != "rows"},
        "policy_execution_order": order,
        "policy_results": results,
        "ledger_summary": ledger,
        "policy_ledger_metrics": policy_ledger_metrics,
        "per_call_billing": [
            call
            for policy_id in POLICIES
            for call in policy_ledger_metrics[policy_id]["per_call_billing"]
        ],
        "billing_calibration": calibration_receipt(),
        "estimated_total_cost_usd": ledger["actual_cost_usd"],
        "estimated_total_cost_semantics": "conservative_reference_list_price_not_invoice",
        "estimated_total_billed_cost_cny": round(
            sum(
                float(row["estimated_billed_cost_cny"])
                for row in policy_ledger_metrics.values()
            ),
            10,
        ),
        "effective_total_billed_cost_cny": round(sum(float(row["effective_billed_cost_cny"]) for row in policy_ledger_metrics.values()), 10),
        "cumulative_reconciled_and_estimated_billed_cost_cny": round(
            PRIOR_RECONCILED_BILLED_COST_CNY
            + sum(
                float(row["effective_billed_cost_cny"])
                for row in policy_ledger_metrics.values()
            ),
            10,
        ),
        "cumulative_internal_cost_usd": round(
            PRIOR_INTERNAL_COST_USD + float(ledger["actual_cost_usd"]), 10
        ),
        "budget_exceeded": float(ledger["actual_cost_usd"]) > budget_usd + 1e-12,
        "provider_limits_usd": provider_limits,
        "provider_limit_policy": "flexible_per_provider_under_one_aggregate_hard_cap",
        "model_identity": preflight["models"],
        "provider_invoice_reconciled": False,
        "commercial_claims_allowed": False,
        "statistical_superiority_claims_allowed": False,
        "frozen_task_path": str(frozen_path.resolve()),
        "runtime_path": str(runtime_path.resolve()),
    }
    _publish_new_json(report_path, report)
    return {**report, "report_path": str(report_path.resolve())}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the authorized Aurora real parallel pilot")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--analyze-report", type=Path, help="Read terminal blockers from a saved result file offline")
    parser.add_argument("--fresh-replay", action="store_true")
    parser.add_argument("--budget-usd", type=float)
    parser.add_argument("--budget-cny", type=float)
    parser.add_argument("--root", type=Path, default=project_root())
    args = parser.parse_args(argv)
    if args.analyze_report is not None:
        if args.fresh_replay or args.budget_usd is not None or args.budget_cny is not None:
            parser.error("--analyze-report takes no replay or budget options")
        try:
            result = analyze_report(args.analyze_report)
        except (ValueError, OSError, RecursionError) as error:
            print(json.dumps({"status": "invalid_report", "error": str(error)}, ensure_ascii=False))
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    budget_usd = (
        args.budget_usd
        if args.budget_usd is not None
        else min(
            MAX_BUDGET_USD,
            args.budget_cny / CNY_HARD_CAP_REFERENCE_MULTIPLIER,
        )
        if args.budget_cny is not None
        else MAX_BUDGET_USD
    )
    try:
        result = run_preflight(
            root=args.root.resolve(),
            budget_usd=budget_usd,
            budget_cny=args.budget_cny,
            fresh_replay=args.fresh_replay,
        ) if args.preflight else run_real(
            root=args.root.resolve(),
            budget_usd=budget_usd,
            budget_cny=args.budget_cny,
            fresh_replay=args.fresh_replay,
        )
    except AuroraRealPilotError as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.preflight:
        return 0 if result["passed"] else 1
    return 0 if any(row["completed_model_calls"] for row in result["policy_results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
