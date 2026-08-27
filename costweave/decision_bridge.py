"""Authoritative, fail-closed shadow decision bridge for CostWeave v0.6.9.

The bridge deliberately has no execution authority.  It gives the five
advisory intelligence services one frozen view of a Run and records their
recommendations in a single immutable envelope.  The existing router remains
the only source of the production route.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .capability_graph_service import CapabilityGraphService
from .contracts_v4 import utc_now
from .execution_recovery_service import ExecutionRecoveryService
from .intelligence.common import canonical_json
from .joint_search_service import JointSearchService
from .meta_controller_service import RationalMetaControllerService
from .safe_learning_service import SafeLearningService


DECISION_BRIDGE_SCHEMA_VERSION = "v0.6.9"
DECISION_BRIDGE_POLICY_VERSION = "authoritative-decision-bridge-v1"
MODULE_ORDER = (
    "meta_controller",
    "capability_graph",
    "joint_search",
    "execution_recovery",
    "safe_learning",
)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    """Freeze an arbitrary JSON value into deterministic immutable text."""

    return canonical_json(value)


def _unjson(value: str) -> Any:
    return json.loads(value)


def _bounded_probability(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.01, min(0.99, float(value)))
    except (TypeError, ValueError):
        return default


def _risk(value: Any) -> str:
    text = str(value or "medium").lower()
    return {
        "normal": "medium",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "critical": "critical",
    }.get(text, "medium")


def _management_mode(value: Any) -> str:
    return {
        "no_delegation": "no_delegation",
        "direct_delegation": "planner",
        "single_planner": "planner",
        "multi_planner": "multiplanner",
        "expert_override": "expert",
        "ask_user": "expert",
    }.get(str(value or ""), "planner")


@dataclass(frozen=True, slots=True)
class ModuleDecisionReceiptV1:
    module: str
    module_schema_version: str
    status: str
    evidence_level: str
    decision_json: str
    error: str | None = None
    advisory_only: bool = True
    execution_authority: bool = False
    provider_calls: int = 0
    network_calls: int = 0
    external_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "module_schema_version": self.module_schema_version,
            "status": self.status,
            "evidence_level": self.evidence_level,
            "decision": _unjson(self.decision_json),
            "error": self.error,
            "advisory_only": self.advisory_only,
            "execution_authority": self.execution_authority,
            "provider_calls": self.provider_calls,
            "network_calls": self.network_calls,
            "external_calls": self.external_calls,
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeDecisionEnvelopeV1:
    run_id: str
    trace_id: str
    project_id: str
    data_residency: str
    created_at: str
    shared_input_fingerprint: str
    baseline_input_fingerprint: str
    shadow_input_fingerprint: str
    input_snapshot_json: str
    catalog_snapshot_json: str
    price_snapshot_json: str
    capacity_snapshot_json: str
    candidate_topologies_json: str
    candidate_model_combinations_json: str
    distributions_json: str
    budgets_json: str
    baseline_route_json: str
    shadow_route_json: str
    structured_diff_json: str
    module_receipts: tuple[ModuleDecisionReceiptV1, ...]
    failure_closed: bool
    selection_probability: float
    fingerprint: str
    schema_version: str = DECISION_BRIDGE_SCHEMA_VERSION
    policy_version: str = DECISION_BRIDGE_POLICY_VERSION
    evidence_level: str = "E0-shadow"
    authority_status: str = "advisory-only"
    management_depth_limit: int = 2
    formal_route_effects: int = 0
    budget_effects: int = 0
    task_status_effects: int = 0
    dag_mutations: int = 0
    provider_calls: int = 0
    network_calls: int = 0
    external_calls: int = 0

    def _identity(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "project_id": self.project_id,
            "data_residency": self.data_residency,
            "shared_input_fingerprint": self.shared_input_fingerprint,
            "baseline_input_fingerprint": self.baseline_input_fingerprint,
            "shadow_input_fingerprint": self.shadow_input_fingerprint,
            "input_snapshot": _unjson(self.input_snapshot_json),
            "catalog_snapshot": _unjson(self.catalog_snapshot_json),
            "price_snapshot": _unjson(self.price_snapshot_json),
            "capacity_snapshot": _unjson(self.capacity_snapshot_json),
            "candidate_management_topologies": _unjson(
                self.candidate_topologies_json
            ),
            "candidate_model_combinations": _unjson(
                self.candidate_model_combinations_json
            ),
            "distributions": _unjson(self.distributions_json),
            "budgets": _unjson(self.budgets_json),
            "baseline_route": _unjson(self.baseline_route_json),
            "shadow_route": _unjson(self.shadow_route_json),
            "structured_diff": _unjson(self.structured_diff_json),
            "module_receipts": [item.to_dict() for item in self.module_receipts],
            "failure_closed": self.failure_closed,
            "selection_probability": self.selection_probability,
            "evidence_level": self.evidence_level,
            "authority_status": self.authority_status,
            "management_depth_limit": self.management_depth_limit,
            "effects": {
                "formal_route": self.formal_route_effects,
                "budget": self.budget_effects,
                "task_status": self.task_status_effects,
                "dag_mutations": self.dag_mutations,
                "provider_calls": self.provider_calls,
                "network_calls": self.network_calls,
                "external_calls": self.external_calls,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        result = self._identity()
        result["created_at"] = self.created_at
        result["fingerprint"] = self.fingerprint
        return result


class _ServiceAdapter:
    module = ""

    def __init__(self, invoke: Callable[[Mapping[str, Any], str], Mapping[str, Any]]):
        self._invoke = invoke

    def decide(self, payload: Mapping[str, Any], idempotency_key: str) -> ModuleDecisionReceiptV1:
        result = dict(self._invoke(payload, idempotency_key))
        calls = {
            "provider_calls": int(result.get("provider_calls", 0)),
            "network_calls": int(result.get("network_calls", 0)),
            "external_calls": int(result.get("external_calls", 0)),
        }
        if any(calls.values()):
            raise RuntimeError(f"{self.module} attempted a forbidden external call")
        # Transport replay metadata and the non-persisted MetaController
        # committee must not alter the semantic decision fingerprint.
        stable_result = {
            key: value
            for key, value in result.items()
            if key not in {"replayed", "idempotency_key", "committee"}
        }
        return ModuleDecisionReceiptV1(
            module=self.module,
            module_schema_version=str(result.get("schema_version", "unknown")),
            status="ok",
            evidence_level="E0-shadow",
            decision_json=_json(stable_result),
            **calls,
        )


class DecisionOrchestratorV1:
    """Runs all advisory services against one immutable Run snapshot."""

    def __init__(self, control_kernel: Any, *, adapter_overrides: Mapping[str, Any] | None = None):
        self.control_kernel = control_kernel
        project_service = lambda service, method: (
            lambda payload, key: getattr(service, method)(
                project_id=str(payload["_project_id"]),
                idempotency_key=key,
                payload={k: v for k, v in payload.items() if k != "_project_id"},
            )[0]
        )
        services = {
            "meta_controller": _ServiceAdapter(
                project_service(
                    RationalMetaControllerService(control_kernel.meta_controller_store),
                    "decide",
                )
            ),
            "capability_graph": _ServiceAdapter(
                project_service(
                    CapabilityGraphService(control_kernel.capability_graph_store),
                    "decide",
                )
            ),
            "joint_search": _ServiceAdapter(
                project_service(
                    JointSearchService(control_kernel.joint_search_store),
                    "decide",
                )
            ),
            "execution_recovery": _ServiceAdapter(
                project_service(
                    ExecutionRecoveryService(control_kernel.execution_recovery_store),
                    "decide",
                )
            ),
            "safe_learning": _ServiceAdapter(
                project_service(
                    SafeLearningService(control_kernel.safe_learning_store),
                    "evaluate",
                )
            ),
        }
        for name, adapter in services.items():
            adapter.module = name
        services.update(dict(adapter_overrides or {}))
        self.adapters = services

    def build(self, record: Any) -> AuthoritativeDecisionEnvelopeV1:
        if record.plan is None:
            raise ValueError("a routed plan is required before decision bridging")
        now = str(record.started_at or record.created_at or utc_now())
        tasks = [task.to_dict() for task in record.plan.tasks]
        workers = list(record.worker_snapshot)
        contract_snapshot = [
            {
                "id": task["id"],
                "title": task.get("title"),
                "objective": task.get("objective"),
                "task_type": task.get("task_type"),
                "dependencies": list(task.get("dependencies", ())),
                "dependency_types": dict(task.get("dependency_types", {})),
                "required_capabilities": list(task.get("required_capabilities", ())),
                "required_modalities": list(task.get("required_modalities", ())),
                "requires_tools": list(task.get("requires_tools", ())),
                "acceptance_criteria": list(task.get("acceptance_criteria", ())),
                "output_schema": list(task.get("output_schema", ())),
                "include": list(task.get("include", ())),
                "exclude": list(task.get("exclude", ())),
                "difficulty": task.get("difficulty"),
                "risk_level": task.get("risk_level"),
                "uncertainty": task.get("uncertainty"),
                "criticality": task.get("criticality"),
                "selected_worker": task.get("selected_worker"),
                "predicted_success": task.get("predicted_success"),
                "predicted_success_lower_bound": task.get("predicted_success_lower_bound"),
                "estimated_cost": task.get("estimated_cost"),
                "estimated_latency_ms": task.get("estimated_latency_ms"),
            }
            for task in tasks
        ]
        compact_workers = [
            {
                "id": worker.get("id"),
                "name": worker.get("name"),
                "model_id": worker.get("model_id"),
                "provider": worker.get("provider"),
                "tier": worker.get("tier"),
                "specialty": worker.get("specialty"),
                "capabilities": dict(worker.get("capabilities", {})),
                "reasoning": worker.get("reasoning"),
                "speed": worker.get("speed"),
                "reliability": worker.get("reliability"),
                "availability": worker.get("availability"),
                "context_window": worker.get("context_window"),
                "modalities": list(worker.get("modalities", ())),
                "tools": list(worker.get("tools", ())),
                "routable": worker.get("routable", True),
                "local": worker.get("local", False),
                "data_confidence": worker.get("data_confidence"),
                "verified_at": worker.get("verified_at"),
            }
            for worker in workers
        ]
        request = {
            "goal": record.request.goal,
            "project_id": record.request.project_id,
            "data_residency": record.request.data_residency,
            "mode": record.request.mode.value,
            "budget": record.request.budget,
            "quality_floor": record.request.quality_floor,
            "max_concurrency": record.request.max_concurrency,
            "deadline_seconds": record.request.deadline_seconds,
            "recovery_budget_ratio": record.request.recovery_budget_ratio,
        }
        input_snapshot = {
            "request": request,
            "task_contracts": contract_snapshot,
            "acceptance_criteria": {
                task["id"]: list(task.get("acceptance_criteria", ()))
                for task in tasks
            },
            "planning_decision": record.planning_decision,
            "plan_revision": record.plan.version,
        }
        catalog_snapshot = {
            "revision": record.plan.model_snapshot,
            "workers": compact_workers,
        }
        price_snapshot = {
            "revision": record.plan.model_snapshot,
            "currency": "USD",
            "routes": [
                {
                    "worker_id": worker.get("id"),
                    "model_id": worker.get("model_id"),
                    "provider": worker.get("provider"),
                    "cost_per_task": worker.get("cost_per_task", 0),
                    "input_price_per_mtok": worker.get("input_price_per_mtok", 0),
                    "output_price_per_mtok": worker.get("output_price_per_mtok", 0),
                    "verified_at": worker.get("verified_at", ""),
                }
                for worker in workers
            ],
        }
        capacity_snapshot = {
            "max_concurrency": record.request.max_concurrency,
            "available_workers": sum(bool(item.get("routable", True)) for item in workers),
            "local_workers": sum(bool(item.get("local", False)) for item in workers),
            "queue_depth": 0,
            "telemetry_evidence": "E0-unavailable",
        }
        shared = {
            "input": input_snapshot,
            "catalog": catalog_snapshot,
            "price": price_snapshot,
            "capacity": capacity_snapshot,
        }
        shared_fingerprint = _digest(shared)
        objective_hash = hashlib.sha256(record.request.goal.encode("utf-8")).hexdigest()
        task_family = self._task_family(tasks)
        risk = self._overall_risk(tasks)
        baseline_route = self._baseline_route(record)
        payloads = self._payloads(
            record=record,
            now=now,
            tasks=tasks,
            workers=workers,
            objective_hash=objective_hash,
            task_family=task_family,
            risk=risk,
            baseline_route=baseline_route,
        )
        def invoke(name: str) -> ModuleDecisionReceiptV1:
            try:
                receipt = self.adapters[name].decide(
                    payloads[name], f"bridge:{record.id}:{name}:v1"
                )
                if (
                    not receipt.advisory_only
                    or receipt.execution_authority
                    or receipt.provider_calls
                    or receipt.network_calls
                    or receipt.external_calls
                ):
                    raise RuntimeError("adapter violated advisory-only authority")
                return receipt
            except Exception as exc:
                return ModuleDecisionReceiptV1(
                    module=name,
                    module_schema_version="unavailable",
                    status="failed_closed",
                    evidence_level="E0-missing",
                    decision_json=_json({}),
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )

        # The modules consume one immutable snapshot and have no authority to
        # mutate one another.  Run them concurrently so the shadow path adds
        # roughly the slowest module's latency instead of the sum of all five.
        # ``executor.map`` preserves MODULE_ORDER for deterministic receipts.
        with ThreadPoolExecutor(
            max_workers=len(MODULE_ORDER),
            thread_name_prefix="decision-bridge",
        ) as pool:
            receipts = list(pool.map(invoke, MODULE_ORDER))
        failure_closed = any(item.status != "ok" for item in receipts)
        receipt_map = {item.module: item.to_dict() for item in receipts}
        shadow_route = self._shadow_route(
            baseline_route, receipt_map, failure_closed=failure_closed
        )
        topologies = [
            {
                "management_mode": mode,
                "management_depth": 1 if mode == "no_delegation" else 2,
                "feasible": mode == baseline_route["management_mode"],
                "rejection_reasons": [] if mode == baseline_route["management_mode"] else ["not_selected_by_baseline"],
            }
            for mode in ("no_delegation", "planner", "multiplanner", "expert")
        ]
        combinations = [
            {
                "task_id": task["id"],
                "baseline_worker": task.get("selected_worker"),
                "candidates": [
                    item.get("worker_id") or item.get("id")
                    for item in task.get("routing_candidates", [])
                ][:8],
                "rejections": [
                    {
                        "worker_id": item.get("worker_id") or item.get("id"),
                        "reason": item.get("reason"),
                        "reason_codes": list(item.get("reason_codes", ())),
                    }
                    for item in task.get("routing_rejections", ())[:8]
                ],
            }
            for task in tasks
        ]
        distributions = {
            "success": {
                "mean": record.plan.predicted_success,
                "lower": min(
                    [float(task.get("predicted_success_lower_bound", 0)) for task in tasks]
                    or [0]
                ),
                "confidence": record.plan.estimate_confidence,
            },
            "cost_usd": {
                "mean": record.plan.routing_summary.get("estimated_total_cost_usd", 0),
                "p95": sum(float(task.get("estimated_cost", 0)) * 1.5 for task in tasks),
            },
            "latency_ms": {
                "mean": record.plan.routing_summary.get("critical_path_p95_ms", 0),
                "p95": record.plan.routing_summary.get("critical_path_p95_ms", 0),
            },
            "confidence": record.plan.estimate_confidence,
        }
        planning_budget = dict((record.planning_decision or {}).get("planning_budget") or {})
        budgets = {
            "total_usd": record.request.budget,
            "planning": planning_budget,
            "handoff_usd": round(record.request.budget * 0.03, 6),
            "validation_usd": round(record.request.budget * 0.10, 6),
            "recovery_usd": round(record.request.budget * record.request.recovery_budget_ratio, 6),
            "shadow_usd": 0.0,
        }
        structured_diff = {
            "input_diff": [],
            "route_diff": self._route_diff(baseline_route, shadow_route),
            "reason_codes": (
                ["module_failure_fail_closed"]
                if failure_closed
                else ["shadow_recommendation_not_applied"]
            ),
            "applied": False,
        }
        selection_probability = self._selection_probability(receipt_map)
        values = {
            "run_id": str(record.id),
            "trace_id": str(record.trace_id),
            "project_id": record.request.project_id,
            "data_residency": record.request.data_residency,
            "created_at": now,
            "shared_input_fingerprint": shared_fingerprint,
            "baseline_input_fingerprint": shared_fingerprint,
            "shadow_input_fingerprint": shared_fingerprint,
            "input_snapshot_json": _json(input_snapshot),
            "catalog_snapshot_json": _json(catalog_snapshot),
            "price_snapshot_json": _json(price_snapshot),
            "capacity_snapshot_json": _json(capacity_snapshot),
            "candidate_topologies_json": _json(topologies),
            "candidate_model_combinations_json": _json(combinations),
            "distributions_json": _json(distributions),
            "budgets_json": _json(budgets),
            "baseline_route_json": _json(baseline_route),
            "shadow_route_json": _json(shadow_route),
            "structured_diff_json": _json(structured_diff),
            "module_receipts": tuple(receipts),
            "failure_closed": failure_closed,
            "selection_probability": selection_probability,
        }
        provisional = AuthoritativeDecisionEnvelopeV1(
            **values, fingerprint=""
        )
        return AuthoritativeDecisionEnvelopeV1(
            **values, fingerprint=_digest(provisional._identity())
        )

    @staticmethod
    def _task_family(tasks: Sequence[Mapping[str, Any]]) -> str:
        raw = str(tasks[0].get("task_type", "general") if tasks else "general").lower()
        slug = "".join(char if char.isalnum() or char in "_.-" else "-" for char in raw)
        return slug.strip("-")[:64] or "general"

    @staticmethod
    def _overall_risk(tasks: Sequence[Mapping[str, Any]]) -> str:
        weights = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        risks = [_risk(item.get("risk_level")) for item in tasks]
        return max(risks or ["medium"], key=lambda item: weights[item])

    @staticmethod
    def _baseline_route(record: Any) -> dict[str, Any]:
        tasks = [
            {
                "task_id": task.id,
                "worker_id": task.selected_worker,
                "predicted_success": task.predicted_success,
                "predicted_success_lower": task.predicted_success_lower_bound,
                "estimated_cost_usd": task.estimated_cost,
                "estimated_latency_ms": task.estimated_latency_ms,
            }
            for task in record.plan.tasks
        ]
        return {
            "source": "existing_predictive_router",
            "management_mode": _management_mode(
                (record.planning_decision or {}).get("mode")
            ),
            "tasks": tasks,
            "estimated_total_cost_usd": record.plan.routing_summary.get(
                "estimated_total_cost_usd", 0
            ),
            "applied": True,
        }

    @staticmethod
    def _shadow_route(
        baseline: Mapping[str, Any],
        receipts: Mapping[str, Mapping[str, Any]],
        *,
        failure_closed: bool,
    ) -> dict[str, Any]:
        if failure_closed:
            return {
                **dict(baseline),
                "source": "fail_closed_baseline_copy",
                "applied": False,
                "reason_codes": ["module_failure_fail_closed"],
            }
        meta = receipts["meta_controller"]["decision"].get("decision") or {}
        joint = receipts["joint_search"]["decision"].get("decision") or {}
        capability = receipts["capability_graph"]["decision"].get("decision") or {}
        return {
            "source": "v0.6.9_shadow_advisory",
            "management_mode": _management_mode(meta.get("action")),
            "meta_action": meta.get("action"),
            "capability_action": capability.get("action"),
            "joint_action": joint.get("action"),
            "selected_candidate_id": joint.get("selected_candidate_id"),
            "tasks": list(baseline.get("tasks", ())),
            "estimated_total_cost_usd": baseline.get("estimated_total_cost_usd", 0),
            "applied": False,
            "reason_codes": ["shadow_recommendation_not_applied"],
        }

    @staticmethod
    def _route_diff(baseline: Mapping[str, Any], shadow: Mapping[str, Any]) -> list[dict[str, Any]]:
        fields = ("management_mode", "estimated_total_cost_usd")
        result = []
        for field in fields:
            if baseline.get(field) != shadow.get(field):
                result.append(
                    {
                        "field": field,
                        "baseline": baseline.get(field),
                        "shadow": shadow.get(field),
                        "reason_code": "advisory_route_difference",
                    }
                )
        return result

    @staticmethod
    def _selection_probability(receipts: Mapping[str, Mapping[str, Any]]) -> float:
        capability = receipts.get("capability_graph", {}).get("decision", {}).get("decision", {})
        meta = receipts.get("meta_controller", {}).get("decision", {}).get("decision", {})
        for value in (
            capability.get("selection_probability"),
            capability.get("confidence"),
            meta.get("confidence"),
        ):
            if value is not None:
                return round(_bounded_probability(value), 6)
        return 0.5

    def _payloads(
        self,
        *,
        record: Any,
        now: str,
        tasks: Sequence[Mapping[str, Any]],
        workers: Sequence[Mapping[str, Any]],
        objective_hash: str,
        task_family: str,
        risk: str,
        baseline_route: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        project = record.request.project_id
        case_id = str(record.id)[:200]
        deadline_ms = max(
            1,
            int((record.request.deadline_seconds or 30) * 1000),
        )
        candidates = self._route_candidates(workers, tasks)
        strategy = self._strategy_candidate(
            case_id, objective_hash, tasks, record, management_mode=baseline_route["management_mode"]
        )
        task_difficulty = max([int(item.get("difficulty", 1)) for item in tasks] or [1]) / 10
        task_belief = {
            "case_id": case_id,
            "objective_hash": objective_hash,
            "task_family_probabilities": {task_family: 1.0},
            "required_modalities": sorted({m for item in tasks for m in item.get("required_modalities", [])}),
            "required_tools": sorted({t for item in tasks for t in item.get("requires_tools", [])}),
            "risk_level": risk,
            "reversible": risk not in {"critical"},
            "external_side_effects": False,
            "decomposability": self._interval(min(0.95, len(tasks) / 5)),
            "parallelizability": self._interval(min(0.95, record.request.max_concurrency / max(1, len(tasks)))),
            "verification_difficulty": self._interval(task_difficulty),
            "epistemic_uncertainty": min(0.95, max(float(item.get("uncertainty", 0)) for item in tasks)),
            "aleatoric_uncertainty": 0.05,
            "ood_score": min(0.95, max(0.02, task_difficulty * 0.25)),
            "hard_constraints": {
                "budget_usd": record.request.budget,
                "quality_floor": record.request.quality_floor,
                "authority": "shadow-only",
            },
            "evidence_ids": [f"bridge-input:{record.trace_id}"],
            "observed_at": now,
            "schema_version": "v0.6.0",
        }
        system_belief = {
            "case_id": case_id,
            "local_queue_depth": 0,
            "local_available_slots": max(0, record.request.max_concurrency),
            "local_gpu_memory_available_mb": None,
            "budget_remaining_usd": record.request.budget,
            "deadline_remaining_ms": deadline_ms,
            "provider_beliefs": [],
            "telemetry_age_ms": 0,
            "evidence_ids": [f"bridge-capacity:{record.trace_id}"],
            "observed_at": now,
            "schema_version": "v0.6.0",
        }
        joint_nodes = []
        route_sets = []
        all_tools: set[str] = set()
        deliverables: set[str] = set()
        worker_lookup = {str(item.get("id")): item for item in workers}
        for task in tasks:
            output = list(task.get("output_schema") or ["result"])
            deliverable = str(output[0])[:120]
            deliverables.add(deliverable)
            tools = [str(item) for item in task.get("requires_tools", ())]
            all_tools.update(tools)
            joint_nodes.append({
                "node_id": task["id"], "objective_hash": objective_hash,
                "dependencies": list(task.get("dependencies", ())), "deliverables": [deliverable],
                "required_authority_scopes": ["workspace"], "risk_level": _risk(task.get("risk_level")),
                "context_tokens": int(task.get("estimated_input_tokens", 0)), "schema_version": "v0.6.3",
            })
            selected = str(task.get("selected_worker") or "")
            candidate_ids = [selected] if selected else []
            candidate_ids.extend(
                str(item.get("worker_id") or item.get("id") or "")
                for item in task.get("routing_candidates", ())
            )
            candidate_ids.extend(str(item.get("id")) for item in workers[:2])
            options = []
            for worker_id in list(dict.fromkeys(item for item in candidate_ids if item))[:3]:
                worker = worker_lookup.get(worker_id, {})
                success = _bounded_probability(
                    task.get("predicted_success") or worker.get("reliability"), 0.7
                )
                cost = max(0.0, float(task.get("estimated_cost") or worker.get("cost_per_task", 0)))
                latency = max(1.0, float(task.get("estimated_latency_ms") or 100))
                options.append({
                    "route_id": f"{task['id']}:{worker_id}"[:200], "executor_id": worker_id,
                    "reasoning_tier": str(worker.get("tier") or "medium"), "validator_id": "validator:contract",
                    "tool_ids": tools, "authority_scopes": ["workspace"], "predicted_success": self._interval(success),
                    "predicted_cost_usd": self._estimate(cost), "validation_cost_usd": self._estimate(0),
                    "predicted_latency_ms": self._estimate(latency), "validation_latency_ms": self._estimate(0),
                    "failure_correlation_group": str(worker.get("provider") or "unknown"),
                    "handoff_fields": {"objective": str(task.get("objective", ""))[:500]},
                    "information_loss": 0.02, "capacity_units": 1, "cost_known": True,
                    "latency_known": True, "schema_version": "v0.6.3",
                })
            route_sets.append({"node_id": task["id"], "options": options, "schema_version": "v0.6.3"})
        recovery_nodes = [
            {
                "node_id": task["id"], "dependencies": list(task.get("dependencies", ())),
                "dependency_types": {dep: task.get("dependency_types", {}).get(dep, "hard") for dep in task.get("dependencies", ())},
                # v0.6.4 compares RETRY_NODE/FULL_REPLAN against a concrete
                # trigger.  The first node is an explicitly simulated E0
                # preflight suspect; this never touches the authoritative DAG.
                "status": "suspect" if index == 0 else "pending",
                "predicted_success": self._interval(_bounded_probability(task.get("predicted_success"), 0.7)),
                "predicted_cost_usd": self._estimate(max(0.0, float(task.get("estimated_cost", 0)))),
                "predicted_latency_ms": self._estimate(max(0.0, float(task.get("estimated_latency_ms", 0)))),
                "actual_cost_usd": 0, "actual_latency_ms": 0, "attempts": 0,
                "max_attempts": record.request.max_task_attempts, "artifact_refs": [],
                "accepted_artifact_refs": [], "reusable_artifact_refs": [], "failure_class": None,
                "validation_score": None, "validator_family": "contract-validator",
                "alternate_executor_available": len(candidates) > 1, "criticality": float(task.get("criticality", 0.5)),
                "schema_version": "v0.6.4",
            }
            for index, task in enumerate(tasks)
        ]
        observations = [
            {
                "observation_id": f"bridge-{record.id}-{index:02d}", "project_id": project,
                "task_family": task_family, "risk_level": risk, "logged_action": "baseline",
                "logging_action_probabilities": {"baseline": 0.75, "shadow": 0.25},
                "candidate_action_probabilities": {"baseline": 0.6, "shadow": 0.4},
                "predicted_rewards": {"baseline": 0.65, "shadow": 0.68},
                "observed_reward": 0.65 + (index % 3) * 0.01, "evidence_level": "E0-simulated",
                "data_origin": "test_fixture", "independently_validated": False,
                "created_at": now, "schema_version": "v0.6.5",
            }
            for index in range(12)
        ]
        return {
            "meta_controller": {
                "_project_id": project, "confirm": "RUN_META_CONTROLLER", "now": now,
                "task_belief": task_belief, "system_belief": system_belief,
                "candidates": [strategy], "planner_members": [],
                "available_budget": {
                    "money_usd": record.request.budget, "input_tokens": sum(int(item.get("estimated_input_tokens", 0)) for item in tasks),
                    "output_tokens": sum(int(item.get("estimated_output_tokens", 0)) for item in tasks), "wall_clock_ms": deadline_ms,
                    "planner_calls": 4, "judge_calls": 1, "probe_calls": 1,
                },
            },
            "capability_graph": {
                "_project_id": project, "confirm": "RUN_CAPABILITY_ROUTER",
                "context": {
                    "project_id": project, "task_family": task_family, "risk_level": risk,
                    "ood_score": task_belief["ood_score"], "required_tools": task_belief["required_tools"],
                    "required_modalities": task_belief["required_modalities"], "deadline_remaining_ms": deadline_ms,
                    "budget_remaining_usd": record.request.budget,
                    "independent_validator_required": risk in {"high", "critical"},
                },
                "candidates": candidates, "calibration_set_id": "bridge-v069-e0", "now": now,
            },
            "joint_search": {
                "_project_id": project, "confirm": "RUN_JOINT_SEARCH", "searcher": "beam",
                "problem": {
                    "project_id": project, "case_id": case_id, "task_family": task_family,
                    "nodes": joint_nodes, "route_sets": route_sets,
                    "budget": {"money_usd": record.request.budget, "input_tokens": sum(int(item.get("estimated_input_tokens", 0)) for item in tasks),
                               "wall_clock_ms": deadline_ms, "max_evaluations": 64, "beam_width": 4,
                               "max_candidates": 128, "amortization_count": 1, "schema_version": "v0.6.3"},
                    "deadline_ms": deadline_ms, "min_success_lower": record.request.quality_floor,
                    "max_parallelism": record.request.max_concurrency, "max_information_loss": 0.2,
                    "failure_loss_usd": record.request.budget, "latency_weight_usd_per_second": record.request.value_of_time_per_second or 0,
                    "allowed_tools": sorted(all_tools), "allowed_authority_scopes": ["workspace"],
                    "required_deliverables": sorted(deliverables), "management_modes": ["no_delegation", "planner", "multiplanner"],
                    "recovery_policies": ["retry_node", "subgraph_first", "full_replan"],
                    "historical_route_ids": {}, "planner_route_ids": {task["id"]: route_sets[index]["options"][0]["route_id"] for index, task in enumerate(tasks)},
                    "reuse_count": 1, "schema_version": "v0.6.3",
                },
            },
            "execution_recovery": {
                "_project_id": project, "confirm": "RUN_EXECUTION_RECOVERY",
                "snapshot": {
                    "project_id": project, "case_id": case_id, "objective_hash": objective_hash,
                    "plan_revision": record.plan.version, "stage": "milestone", "nodes": recovery_nodes,
                    "budget_remaining_usd": record.request.budget, "deadline_remaining_ms": deadline_ms,
                    "spent_cost_usd": 0, "spent_latency_ms": 0, "validated_artifact_ids": [],
                    "reusable_artifact_ids": [], "polluted_node_ids": [],
                    "failure_node_id": tasks[0]["id"],
                    "failure_class": "e0_pre_execution_uncertainty",
                    "failure_confidence": 0.5, "required_info_missing": [],
                    "trigger_kinds": ["milestone", "failure"], "resource": {
                        "queue_depth": 0, "available_slots": record.request.max_concurrency,
                        "price_multiplier": 1, "provider_available": True, "telemetry_age_ms": 0,
                        "resource_revision": str(record.plan.model_snapshot or "unknown"),
                        "alternate_route_available": len(candidates) > 1,
                        "orthogonal_validator_available": True, "schema_version": "v0.6.4",
                    },
                    "risk_loss_usd": record.request.budget, "max_retries": record.request.max_task_attempts,
                    "max_switches": 1, "max_subgraph_replans": record.request.max_recoveries,
                    "max_full_replans": 1, "observed_at": now,
                    "evidence_ids": [f"bridge-pre-execution:{record.trace_id}"], "schema_version": "v0.6.4",
                },
            },
            "safe_learning": {
                "_project_id": project, "confirm": "RUN_SAFE_LEARNING",
                "policy_id": "decision-bridge-shadow", "policy_version": "1",
                "candidate_policy_id": "decision-bridge-shadow", "candidate_policy_version": "1",
                "champion_policy_id": "existing-predictive-router", "champion_policy_version": str(record.plan.model_snapshot or "1"),
                "champion_estimate": record.plan.predicted_success, "rollback_policy_id": "existing-predictive-router",
                "task_family": task_family, "method": "dr", "stage": "shadow", "observations": observations,
            },
        }

    @staticmethod
    def _interval(mean: float) -> dict[str, float]:
        mean = _bounded_probability(mean)
        return {"mean": mean, "lower": max(0, mean - 0.12), "upper": min(1, mean + 0.08)}

    @staticmethod
    def _estimate(mean: float) -> dict[str, float]:
        mean = max(0.0, float(mean))
        return {"mean": mean, "p95": mean * 1.5}

    def _strategy_candidate(
        self, case_id: str, objective_hash: str, tasks: Sequence[Mapping[str, Any]], record: Any, *, management_mode: str
    ) -> dict[str, Any]:
        if management_mode == "no_delegation" and len(tasks) != 1:
            management_mode = "planner"
        nodes = []
        for task in tasks:
            success = _bounded_probability(task.get("predicted_success"), 0.7)
            nodes.append({
                "node_id": task["id"], "objective_hash": objective_hash,
                "dependencies": list(task.get("dependencies", ())), "executor_id": str(task.get("selected_worker") or "unassigned"),
                "reasoning_tier": "high" if int(task.get("difficulty", 1)) >= 7 else "medium",
                "tool_ids": list(task.get("requires_tools", ())), "parallel_group": str(task.get("parallel_group", "default")),
                "validator_id": "validator:contract", "predicted_success": self._interval(success),
                "predicted_cost_usd": self._estimate(task.get("estimated_cost", 0)),
                "predicted_latency_ms": self._estimate(task.get("estimated_latency_ms", 0)),
            })
        total_cost = max(0.0, float(record.plan.routing_summary.get("estimated_total_cost_usd", 0)))
        total_latency = max(0.0, float(record.plan.routing_summary.get("critical_path_p95_ms", 0)))
        return {
            "case_id": case_id, "management_mode": management_mode, "nodes": nodes,
            "planning_cost_usd": self._estimate(0), "planning_latency_ms": self._estimate(0),
            "predicted_success": self._interval(record.plan.predicted_success),
            "predicted_total_cost_usd": self._estimate(total_cost),
            "predicted_total_latency_ms": self._estimate(total_latency),
            "hard_constraint_checks": {"budget": total_cost <= record.request.budget, "authority": True},
            "recovery_policy": "subgraph_first", "evidence_ids": [f"bridge-baseline:{record.trace_id}"],
            "policy_revision": "existing-router-snapshot", "schema_version": "v0.6.0",
        }

    def _route_candidates(
        self, workers: Sequence[Mapping[str, Any]], tasks: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        candidates = []
        baseline_cost = min([float(item.get("estimated_cost", 0)) for item in tasks] or [0])
        baseline_latency = min([float(item.get("estimated_latency_ms", 100)) for item in tasks] or [100])
        for worker in workers[:32]:
            if not worker.get("routable", True):
                continue
            worker_id = str(worker.get("id") or "unknown")
            candidates.append({
                "route_id": worker_id, "model_revision": str(worker.get("model_id") or worker_id),
                "reasoning_tier": str(worker.get("tier") or "utility"),
                "provider_deployment": f"{worker.get('provider', 'local')}:{worker_id}",
                "validator_id": "validator:contract", "tool_ids": list(worker.get("tools", ())),
                "modality_ids": list(worker.get("modalities", ())),
                "failure_correlation_group": str(worker.get("provider") or "unknown"),
                "base_cost_usd": self._estimate(worker.get("cost_per_task", baseline_cost)),
                "base_latency_ms": self._estimate(max(1.0, baseline_latency * float(worker.get("latency_factor", 1)))),
                "base_cost_known": True, "base_latency_known": True, "schema_version": "v0.6.2",
            })
        if not candidates:
            candidates.append({
                "route_id": "baseline-unavailable", "model_revision": "baseline-unavailable",
                "reasoning_tier": "unknown", "provider_deployment": "local:unavailable",
                "validator_id": "validator:contract", "tool_ids": [], "modality_ids": ["text"],
                "failure_correlation_group": "unknown", "base_cost_usd": self._estimate(baseline_cost),
                "base_latency_ms": self._estimate(max(1, baseline_latency)), "base_cost_known": False,
                "base_latency_known": False, "schema_version": "v0.6.2",
            })
        return candidates


def failure_closed_envelope(record: Any, error: Exception | str) -> AuthoritativeDecisionEnvelopeV1:
    """Produce a complete zero-authority envelope when bridge setup fails."""

    message = str(error)[:500]
    request = {
        "goal": record.request.goal,
        "project_id": record.request.project_id,
        "data_residency": record.request.data_residency,
        "mode": record.request.mode.value,
        "budget": record.request.budget,
        "quality_floor": record.request.quality_floor,
    }
    plan = record.plan.to_dict() if record.plan is not None else None
    shared = {"request": request, "plan": plan}
    shared_fingerprint = _digest(shared)
    baseline = (
        DecisionOrchestratorV1._baseline_route(record)
        if record.plan is not None
        else {"source": "baseline_unavailable", "tasks": [], "applied": True}
    )
    shadow = {
        **baseline,
        "source": "fail_closed_baseline_copy",
        "applied": False,
        "reason_codes": ["decision_bridge_setup_failed"],
    }
    receipts = tuple(
        ModuleDecisionReceiptV1(
            module=name,
            module_schema_version="unavailable",
            status="failed_closed",
            evidence_level="E0-missing",
            decision_json=_json({}),
            error=message,
        )
        for name in MODULE_ORDER
    )
    values = {
        "run_id": str(record.id),
        "trace_id": str(record.trace_id),
        "project_id": record.request.project_id,
        "data_residency": record.request.data_residency,
        "created_at": utc_now(),
        "shared_input_fingerprint": shared_fingerprint,
        "baseline_input_fingerprint": shared_fingerprint,
        "shadow_input_fingerprint": shared_fingerprint,
        "input_snapshot_json": _json(shared),
        "catalog_snapshot_json": _json({"revision": getattr(record.plan, "model_snapshot", ""), "workers": list(record.worker_snapshot)}),
        "price_snapshot_json": _json({"status": "unavailable"}),
        "capacity_snapshot_json": _json({"status": "unavailable"}),
        "candidate_topologies_json": _json([]),
        "candidate_model_combinations_json": _json([]),
        "distributions_json": _json({"status": "unavailable"}),
        "budgets_json": _json({"total_usd": record.request.budget, "shadow_usd": 0}),
        "baseline_route_json": _json(baseline),
        "shadow_route_json": _json(shadow),
        "structured_diff_json": _json({
            "input_diff": [], "route_diff": [],
            "reason_codes": ["decision_bridge_setup_failed"], "applied": False,
        }),
        "module_receipts": receipts,
        "failure_closed": True,
        "selection_probability": 0.0,
    }
    provisional = AuthoritativeDecisionEnvelopeV1(**values, fingerprint="")
    return AuthoritativeDecisionEnvelopeV1(
        **values, fingerprint=_digest(provisional._identity())
    )


__all__ = [
    "AuthoritativeDecisionEnvelopeV1",
    "DECISION_BRIDGE_POLICY_VERSION",
    "DECISION_BRIDGE_SCHEMA_VERSION",
    "DecisionOrchestratorV1",
    "failure_closed_envelope",
    "MODULE_ORDER",
    "ModuleDecisionReceiptV1",
]
