from __future__ import annotations

import argparse
import json
import mimetypes
import posixpath
import socket
from dataclasses import fields
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .catalog_store import (
    CATALOG_STORE,
    CatalogConflictError,
    CatalogError,
    CatalogStore,
)
from .approvals import ApprovalConflictError, ApprovalError
from .compat_v4 import contract_health, runtime_v4_enabled
from .domain import RunMode, RunRequest, WorkerProfile
from .engine import RunManager
from .event_store import IdempotencyConflictError, RevisionConflictError
from .runtime_maintenance import RuntimeMaintenance
from .planning_contracts import (
    ManagementLimits,
    PLANNING_POLICY_VERSION,
    PLANNING_SCHEMA_VERSION,
    PlanningMode,
)
from .role_registry import RoleRegistry
from .handoff_compiler import (
    HANDOFF_POLICY_REVISION,
    HANDOFF_SCHEMA_VERSION,
)
from .adaptive_recovery import (
    CHECKPOINT_SCHEMA_VERSION,
    RECOVERY_GATE_REVISION,
)
from .evaluation_protocol import (
    BASELINE_IDS,
    EVALUATION_PROTOCOL_VERSION,
    SEED_TASK_SET_VERSION,
    load_seed_tasks,
)
from .outcome_dataset import (
    LABEL_SOURCES,
    OUTCOME_DATASET_SCHEMA_VERSION,
    PURPOSES,
)
from .adapters import AdapterRuntimePolicy
from .experimental_calls import (
    EXPERIMENTAL_CALL_POLICY_REVISION,
    EXPERIMENTAL_CALL_SCHEMA_VERSION,
)
from .shadow_router import SHADOW_POLICY_REVISION, SHADOW_SCHEMA_VERSION
from .predictor_admission import public_predictor_policy
from .predictor_evaluation import (
    public_evaluation_policy,
)
from .portfolio_audit import public_portfolio_audit_policy
from .portfolio_validation import public_portfolio_validation_policy
from .policy_candidate import (
    POLICY_CANDIDATE_SCHEMA_VERSION,
    public_policy_candidate_policy,
)
from .offline_replay import public_offline_replay_policy
from .production_shadow import public_production_shadow_policy
from .policy_registry import public_policy_registry_policy
from .canary_control import public_canary_policy


MANAGER: RunManager | None = None
WEB_ROOT = files("costweave").joinpath("web")


class RequestError(ValueError):
    def __init__(self, message: str, status: HTTPStatus, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class CostWeaveServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 64

    def server_close(self) -> None:
        manager = getattr(self, "manager", None)
        if manager is not None:
            manager.shutdown(wait_timeout=10)
        super().server_close()


class CostWeaveHandler(BaseHTTPRequestHandler):
    server_version = "CostWeave/0.5.7.4"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10.0)
        self._legacy_api = False

    @property
    def manager(self) -> RunManager:
        manager = getattr(self.server, "manager", MANAGER)
        if manager is None:
            raise RuntimeError("run manager is not configured")
        return manager

    @property
    def catalog_store(self) -> CatalogStore:
        return getattr(self.server, "catalog_store", CATALOG_STORE)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = self._normalise_api_path(parsed.path)
        if path == "/api/health":
            runtime_health = self.manager.health()
            adapter_policy = AdapterRuntimePolicy.from_environment()
            contracts = contract_health()
            contracts["features"]["runtime_v4"] = runtime_v4_enabled()
            contracts["contract_trace_persistent"] = bool(
                runtime_health.get("persistent")
            )
            self._json({
                "status": "ok",
                "runtime": "offline-decision-simulation",
                "version": __version__,
                "catalog_revision": self.catalog_store.revision,
                "planning_contract": {
                    "schema_version": PLANNING_SCHEMA_VERSION,
                    "policy_version": PLANNING_POLICY_VERSION,
                    "paid_calls_enabled": False,
                },
                "handoff_contract": {
                    "schema_version": HANDOFF_SCHEMA_VERSION,
                    "compiler_revision": HANDOFF_POLICY_REVISION,
                    "checkpoint_schema": CHECKPOINT_SCHEMA_VERSION,
                    "recovery_policy": RECOVERY_GATE_REVISION,
                },
                "evaluation_protocol": {
                    "schema_version": EVALUATION_PROTOCOL_VERSION,
                    "task_set_version": SEED_TASK_SET_VERSION,
                    "baseline_count": len(BASELINE_IDS),
                    "real_provider_calls_allowed": False,
                    "commercial_claims_allowed": False,
                },
                "outcome_dataset_governance": {
                    "schema_version": OUTCOME_DATASET_SCHEMA_VERSION,
                    "training_enabled": False,
                    "policy_promotion_enabled": False,
                },
                "experimental_adapters": {
                    "schema_version": EXPERIMENTAL_CALL_SCHEMA_VERSION,
                    "policy_revision": EXPERIMENTAL_CALL_POLICY_REVISION,
                    **adapter_policy.public(),
                    "hard_budget_required": True,
                    "secrets_persisted": False,
                    "commercial_claims_allowed": False,
                },
                "shadow_router": {
                    "schema_version": SHADOW_SCHEMA_VERSION,
                    "policy_revision": SHADOW_POLICY_REVISION,
                    "advisory_only": True,
                    "intervention_count": 0,
                },
                "predictor_admission": public_predictor_policy(),
                "predictor_evaluation": public_evaluation_policy(),
                "portfolio_audit": public_portfolio_audit_policy(),
                "portfolio_validation": public_portfolio_validation_policy(),
                "policy_candidates": public_policy_candidate_policy(),
                "offline_replay": public_offline_replay_policy(),
                "production_shadow": public_production_shadow_policy(),
                "policy_registry": public_policy_registry_policy(),
                "bounded_canary": public_canary_policy(),
                **contracts,
                "control_kernel": runtime_health,
            })
            return
        if path == "/api/v1/canary/policy":
            if self.manager.control_kernel is not None:
                counts = self.manager.control_kernel.canary_store.counts()
                state = self.manager.control_kernel.canary_store.current_state()
            else:
                counts = {
                    "canary_deployments": 0,
                    "canary_pointer_events": 0,
                    "canary_assignments": 0,
                    "canary_observation_windows": 0,
                    "canary_stage_decisions": 0,
                    "canary_candidate_assignments": 0,
                    "canary_control_assignments": 0,
                    "canary_stable_only_assignments": 0,
                    "canary_high_risk_candidate_assignments": 0,
                    "canary_runtime_policy_changes": 0,
                    "canary_runtime_rollbacks": 0,
                    "canary_stage_advances": 0,
                    "canary_provider_calls": 0,
                    "canary_execution_side_effects": 0,
                }
                state = None
            self._json({
                **public_canary_policy(),
                "current_runtime_state": state,
                "runtime_counts": counts,
            })
            return
        if path == "/api/v1/policies/registry":
            counts = (
                self.manager.control_kernel.policy_registry_store.counts()
                if self.manager.control_kernel is not None
                else {
                    "policy_registry_policies": 0,
                    "policy_registry_evidence": 0,
                    "policy_registry_gate_reports": 0,
                    "policy_registry_transitions": 0,
                    "policy_registry_cards": 0,
                    "policy_registry_rollbacks": 0,
                    "policy_registry_pointer_events": 0,
                    "policy_registry_eligible_gates": 0,
                    "policy_registry_runtime_route_changes": 0,
                    "policy_registry_runtime_activations": 0,
                    "policy_registry_runtime_rollbacks": 0,
                    "policy_registry_execution_side_effects": 0,
                }
            )
            pointer = (
                self.manager.control_kernel.policy_registry_store.current_registry_pointer()
                if self.manager.control_kernel is not None
                else None
            )
            self._json({
                **public_policy_registry_policy(),
                "current_registry_pointer": pointer,
                "runtime_counts": counts,
            })
            return
        if path == "/api/v1/shadow/policy":
            counts = (
                self.manager.control_kernel.production_shadow_store.counts()
                if self.manager.control_kernel is not None
                else {
                    "production_shadow_inputs": 0,
                    "production_shadow_decisions": 0,
                    "production_shadow_failures": 0,
                    "production_shadow_outcomes": 0,
                    "production_shadow_comparisons": 0,
                    "production_shadow_audit_reports": 0,
                    "production_shadow_external_calls": 0,
                    "production_shadow_execution_side_effects": 0,
                    "production_shadow_route_interventions": 0,
                    "production_shadow_production_promotions": 0,
                }
            )
            self._json({
                **public_production_shadow_policy(),
                "runtime_counts": counts,
            })
            return
        if path == "/api/v1/replay/policy":
            counts = (
                self.manager.control_kernel.offline_replay_store.counts()
                if self.manager.control_kernel is not None
                else {
                    "replay_version_snapshots": 0,
                    "offline_replay_cases": 0,
                    "observed_route_outcomes": 0,
                    "offline_replay_results": 0,
                    "approximate_oracle_reports": 0,
                    "replay_benchmark_reports": 0,
                    "offline_replay_external_calls": 0,
                    "offline_replay_route_interventions": 0,
                    "offline_replay_global_optimality_claims": 0,
                    "offline_replay_production_promotions": 0,
                }
            )
            self._json({
                **public_offline_replay_policy(),
                "runtime_counts": counts,
            })
            return
        if path == "/api/v1/portfolio/policy":
            audit_counts = (
                self.manager.control_kernel.portfolio_audit_store.counts()
                if self.manager.control_kernel is not None
                else {
                    "portfolio_audits": 0,
                    "feasible_portfolio_audits": 0,
                    "directional_portfolio_audits": 0,
                    "portfolio_scenario_comparisons": 0,
                    "portfolio_route_interventions": 0,
                }
            )
            validation_counts = (
                self.manager.control_kernel.portfolio_validation_store.counts()
                if self.manager.control_kernel is not None
                else {
                    "portfolio_oracle_results": 0,
                    "portfolio_oracle_proofs": 0,
                    "portfolio_oracle_skips": 0,
                    "portfolio_differential_reports": 0,
                    "portfolio_suboptimal_candidates": 0,
                    "portfolio_sensitivity_reports": 0,
                    "portfolio_validation_route_interventions": 0,
                }
            )
            self._json({
                **public_portfolio_audit_policy(),
                "exact_validation": public_portfolio_validation_policy(),
                "runtime_counts": {**audit_counts, **validation_counts},
            })
            return
        if path == "/api/v1/predictors/policy":
            counts = (
                self.manager.control_kernel.predictor_admission_counts()
                if self.manager.control_kernel is not None
                else {
                    "predictor_admission_reports": 0,
                    "calibration_candidates": 0,
                    "frozen_prediction_receipts": 0,
                    "authoritative_learned_predictions": 0,
                }
            )
            self._json({
                "schema_version": POLICY_CANDIDATE_SCHEMA_VERSION,
                "admission": public_predictor_policy(),
                "offline_evaluation": public_evaluation_policy(),
                "safe_policy_candidates": public_policy_candidate_policy(),
                "current_state": "frozen_no_real_evidence",
                "routing_authority": "legacy-resource-optimizer",
                "policy_promotion_enabled": False,
                "runtime_counts": counts,
            })
            return
        if path == "/api/v1/experiments/policy":
            policy = AdapterRuntimePolicy.from_environment()
            self._json({
                "schema_version": EXPERIMENTAL_CALL_SCHEMA_VERSION,
                "external_call_policy_revision": (
                    EXPERIMENTAL_CALL_POLICY_REVISION
                ),
                "shadow_policy_revision": SHADOW_POLICY_REVISION,
                **policy.public(),
                "default_external_calls": 0,
                "provider_neutral": True,
                "supported_adapter_families": [
                    "openai-compatible-http",
                    "local-loopback-http",
                ],
                "text_only": True,
                "tools_enabled": False,
                "batch_enabled": False,
                "hard_budget_required": True,
                "provider_budget_required": True,
                "retry_budgeted_before_call": True,
                "automatic_model_switching": False,
                "secrets_source": "environment-only",
                "secrets_persisted": False,
                "shadow": {
                    "advisory_only": True,
                    "may_change_actual_route": False,
                    "intervention_count": 0,
                },
                "evidence_gate": {
                    "real_directional_tasks_required": "30-50",
                    "fixture_results_count_as_real": False,
                    "commercial_claims_allowed": False,
                },
            })
            return
        if path == "/api/v1/outcomes/governance":
            self._json({
                "schema_version": OUTCOME_DATASET_SCHEMA_VERSION,
                "label_sources": list(LABEL_SOURCES),
                "purposes": list(PURPOSES),
                "raw_sensitive_text_retained": False,
                "project_partition_isolation": True,
                "append_only_label_evidence": True,
                "conflicting_labels_preserved": True,
                "learning_copy_ttl_and_deletion": True,
                "training_gate": "hard-fail-when-frozen",
                "training_enabled": False,
                "policy_promotion_enabled": False,
                "real_provider_calls_allowed": False,
                "commercial_claims_allowed": False,
                "run_command": (
                    "python -m costweave.outcome_dataset "
                    "<experiment.json> --output-dir <directory> "
                    "--privacy-salt <secret>"
                ),
            })
            return
        if path == "/api/v1/evaluation/protocol":
            seed_tasks = load_seed_tasks()
            self._json({
                "schema_version": EVALUATION_PROTOCOL_VERSION,
                "task_set_version": SEED_TASK_SET_VERSION,
                "task_count": len(seed_tasks),
                "partitions": {
                    partition: sum(
                        item.partition == partition
                        for item in seed_tasks
                    )
                    for partition in ("train", "calibration", "test")
                },
                "baselines": list(BASELINE_IDS),
                "execution_mode": "deterministic_fixture_simulation",
                "evidence_level": "E0-simulated",
                "real_provider_calls_allowed": False,
                "commercial_claims_allowed": False,
                "run_command": (
                    "python -m costweave.baseline_runner "
                    "--output-dir work/v0561-evaluation"
                ),
            })
            return
        if path == "/api/v1/planning/policy":
            self._json({
                "schema_version": PLANNING_SCHEMA_VERSION,
                "policy_version": PLANNING_POLICY_VERSION,
                "modes": [mode.value for mode in PlanningMode],
                "roles": [
                    role.to_dict() for role in RoleRegistry().list()
                ],
                "limits": ManagementLimits().to_dict(),
                "adapter_policy": {
                    "provider_neutral": True,
                    "default_adapter": "mock",
                    "paid_calls_enabled": False,
                    "real_provider_requires_explicit_feature_flag": True,
                },
                "classifier": {
                    "interface": "LightweightTaskClassifier",
                    "default": "local-heuristic-difficulty",
                    "provider_calls": 0,
                },
                "decision_objective": "minimum-expected-total-loss",
                "scope": "bounded-identity-blind-multiplanner",
                "deliberation": {
                    "parallel_proposals": True,
                    "identity_blind_scoring": True,
                    "majority_vote": False,
                    "max_adjudication_rounds": 1,
                    "compiler_target": "artifact-dag",
                },
            })
            return
        if path == "/api/catalog":
            payload = self.catalog_store.payload()
            self._json({**payload, "workers": payload["models"]})
            return
        if path == "/api/catalog/schema":
            self._json({
                "schema_version": 1,
                "fields": [item.name for item in fields(WorkerProfile)],
                "import_formats": ["json", "csv"],
                "import_modes": ["merge", "replace"],
            })
            return
        if path == "/api/catalog/export":
            format_name = parse_qs(parsed.query).get("format", ["json"])[0]
            if format_name == "json":
                self._text(
                    self.catalog_store.export_json(),
                    "application/json; charset=utf-8",
                    "costweave-model-catalog.json",
                )
            elif format_name == "csv":
                self._text(
                    "\ufeff" + self.catalog_store.export_csv(),
                    "text/csv; charset=utf-8",
                    "costweave-model-catalog.csv",
                )
            else:
                self._json(
                    {"error": "unsupported_format", "message": "format 必须是 json 或 csv"},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if path == "/api/v1/system/diagnostics":
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json(
                    {"error": "persistent_runtime_disabled"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            maintenance = RuntimeMaintenance(kernel.store.path)
            self._json({
                "runtime": maintenance.diagnostics(),
                "backups": maintenance.list_backups(),
                "security": {
                    "listener": "loopback-only",
                    "request_timeout_seconds": 10,
                    "artifact_access": "metadata-only",
                },
            })
            return
        if path in {"/api/runs", "/api/v1/runs"}:
            self._json({"runs": self.manager.list()})
            return
        if path == "/api/v1/resources/ledger":
            run_id = parse_qs(parsed.query).get("run_id", [None])[0]
            if run_id is not None and self.manager.get(run_id) is None:
                self._json(
                    {"error": "run_not_found"}, HTTPStatus.NOT_FOUND
                )
                return
            entries = self.manager.ledger(run_id=run_id)
            self._json({
                "run_id": run_id,
                "entries": entries,
                "total": round(
                    sum(float(item["amount"]) for item in entries), 8
                ),
            })
            return
        if path == "/api/v1/resources":
            self._json({"resources": self.manager.resources()})
            return
        if path == "/api/v1/approvals":
            status = parse_qs(parsed.query).get("status", [None])[0]
            if status not in {None, "pending", "approved", "rejected"}:
                self._json(
                    {"error": "invalid_approval_status"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._json(
                {"approvals": self.manager.approvals(status=status)}
            )
            return
        if path == "/api/v1/evaluations":
            self._json({"evaluations": self.manager.evaluations()})
            return
        if path.startswith("/api/v1/media/projects/"):
            suffix = path.removeprefix(
                "/api/v1/media/projects/"
            ).strip("/")
            if suffix.endswith("/shots"):
                run_id = suffix.removesuffix("/shots").strip("/")
                if self.manager.get(run_id) is None:
                    self._json(
                        {"error": "run_not_found"},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                project = self.manager.media_project(run_id)
                if project is None:
                    self._json(
                        {"error": "media_project_not_found"},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                self._json({
                    "run_id": run_id,
                    "project_id": project["project_id"],
                    "shots": self.manager.media_shots(run_id),
                })
                return
            run_id = suffix
            if self.manager.get(run_id) is None:
                self._json(
                    {"error": "run_not_found"}, HTTPStatus.NOT_FOUND
                )
                return
            project = self.manager.media_project(run_id)
            if project is None:
                self._json(
                    {"error": "media_project_not_found"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._json(project)
            return
        if path.startswith("/api/v1/tasks/"):
            task_id = unquote(path.removeprefix("/api/v1/tasks/").strip("/"))
            task = self.manager.task(task_id)
            if task is None:
                self._json({"error": "task_not_found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(task)
            return
        if path.startswith("/api/v1/artifacts/"):
            artifact_id = unquote(
                path.removeprefix("/api/v1/artifacts/").strip("/")
            )
            try:
                artifact = self.manager.artifact(artifact_id)
            except Exception as exc:
                self._json(
                    {
                        "error": "artifact_integrity_failed",
                        "message": str(exc),
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if artifact is None:
                self._json(
                    {"error": "artifact_not_found"}, HTTPStatus.NOT_FOUND
                )
                return
            self._json(artifact)
            return
        if path.startswith("/api/v1/runs/"):
            suffix = path.removeprefix("/api/v1/runs/").strip("/")
            if suffix.endswith("/handoffs"):
                run_id = suffix.removesuffix(
                    "/handoffs"
                ).strip("/")
                if self.manager.get(run_id) is None:
                    self._json(
                        {"error": "run_not_found"},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                handoffs = self.manager.handoffs(run_id)
                self._json({
                    "run_id": run_id,
                    "schema_version": HANDOFF_SCHEMA_VERSION,
                    "handoffs": handoffs,
                    "admitted": sum(
                        bool(
                            item.get("preflight", {}).get(
                                "admitted"
                            )
                        )
                        for item in handoffs
                    ),
                })
                return
            if suffix.endswith("/recovery"):
                run_id = suffix.removesuffix(
                    "/recovery"
                ).strip("/")
                recovery = self.manager.recovery_control(run_id)
                if recovery is None:
                    self._json(
                        {"error": "run_not_found"},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                self._json(recovery)
                return
            if suffix.endswith("/planning"):
                run_id = suffix.removesuffix("/planning").strip("/")
                planning = self.manager.planning(run_id)
                if planning is None:
                    self._json(
                        {"error": "run_not_found"},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                self._json(planning)
                return
            if suffix.endswith("/economics"):
                run_id = suffix.removesuffix(
                    "/economics"
                ).strip("/")
                record = self.manager.get(run_id)
                if record is None:
                    self._json(
                        {"error": "run_not_found"},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                routing = (
                    record.plan.routing_summary
                    if record.plan is not None
                    else {}
                )
                self._json({
                    "run_id": run_id,
                    "strategy": routing.get("strategy"),
                    "estimated_total_cost_usd": routing.get(
                        "estimated_total_cost_usd"
                    ),
                    "direct_model_cost_usd": routing.get(
                        "direct_model_cost_usd"
                    ),
                    "non_model_cost_usd": routing.get(
                        "non_model_cost_usd"
                    ),
                    "critical_path_p95_ms": routing.get(
                        "critical_path_p95_ms"
                    ),
                    "budget_rebalances": routing.get(
                        "budget_rebalances", 0
                    ),
                    "cross_provider": routing.get(
                        "cross_provider", {}
                    ),
                })
                return
            if suffix.endswith("/forecast"):
                run_id = suffix.removesuffix(
                    "/forecast"
                ).strip("/")
                if self.manager.get(run_id) is None:
                    self._json(
                        {"error": "run_not_found"},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                forecast = self.manager.forecast(run_id)
                if forecast is None:
                    self._json(
                        {
                            "error": "forecast_unavailable",
                            "run_id": run_id,
                        },
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                self._json(forecast)
                return
            if suffix.endswith("/validations"):
                run_id = suffix.removesuffix(
                    "/validations"
                ).strip("/")
                if self.manager.get(run_id) is None:
                    self._json(
                        {"error": "run_not_found"},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                items = self.manager.validations(run_id)
                self._json({
                    "run_id": run_id,
                    "validations": items,
                    "passed": sum(
                        item["passed"] for item in items
                    ),
                    "failed": sum(
                        not item["passed"] for item in items
                    ),
                })
                return
            if suffix.endswith("/failures"):
                run_id = suffix.removesuffix(
                    "/failures"
                ).strip("/")
                if self.manager.get(run_id) is None:
                    self._json(
                        {"error": "run_not_found"},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                self._json({
                    "run_id": run_id,
                    "failures": self.manager.failures(run_id),
                })
                return
            if suffix.endswith("/placements"):
                run_id = suffix.removesuffix("/placements").strip("/")
                if self.manager.get(run_id) is None:
                    self._json(
                        {"error": "run_not_found"}, HTTPStatus.NOT_FOUND
                    )
                    return
                placements = self.manager.placements(run_id)
                self._json({
                    "run_id": run_id,
                    "placements": placements,
                    "selected": sum(
                        item["status"] == "selected"
                        for item in placements
                    ),
                    "backups": sum(
                        item["status"] == "backup"
                        for item in placements
                    ),
                })
                return
            if suffix.endswith("/graph"):
                run_id = suffix.removesuffix("/graph").strip("/")
                graph = self.manager.graph(run_id)
                if graph is None:
                    self._json(
                        {"error": "run_not_found"}, HTTPStatus.NOT_FOUND
                    )
                    return
                self._json(graph)
                return
            if suffix.endswith("/artifacts"):
                run_id = suffix.removesuffix("/artifacts").strip("/")
                if self.manager.get(run_id) is None:
                    self._json(
                        {"error": "run_not_found"}, HTTPStatus.NOT_FOUND
                    )
                    return
                self._json(
                    {
                        "run_id": run_id,
                        "artifacts": self.manager.artifacts(run_id),
                    }
                )
                return
            if suffix.endswith("/events"):
                run_id = suffix.removesuffix("/events").strip("/")
                record = self.manager.get(run_id)
                if not record:
                    self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    query = parse_qs(parsed.query)
                    after = int(query.get("after_revision", ["0"])[0])
                    limit = int(query.get("limit", ["200"])[0])
                    if after < 0 or not 1 <= limit <= 1000:
                        raise ValueError
                except (TypeError, ValueError):
                    self._json(
                        {"error": "invalid_pagination"}, HTTPStatus.BAD_REQUEST
                    )
                    return
                events = self.manager.events(
                    run_id, after_revision=after, limit=limit
                )
                self._json({
                    "run_id": run_id,
                    "events": events,
                    "after_revision": after,
                    "next_revision": events[-1].get("revision", after) if events else after,
                })
                return
            record = self.manager.get(suffix)
            if not record:
                self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(record.snapshot())
            return
        if path.startswith("/api/runs/"):
            run_id = path.removeprefix("/api/runs/").strip("/")
            record = self.manager.get(run_id)
            if not record:
                self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(record.snapshot())
            return
        if path.startswith("/api/"):
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = self._normalise_api_path(urlparse(self.path).path)
        if path == "/api/v1/system/backups":
            self._create_runtime_backup()
            return
        if path == "/api/v1/runs":
            self._create_run(v1=True)
            return
        if path.startswith("/api/v1/runs/"):
            suffix = path.removeprefix("/api/v1/runs/").strip("/")
            if suffix.endswith("/cancel"):
                self._cancel_run(suffix.removesuffix("/cancel").strip("/"))
                return
            if suffix.endswith("/retry"):
                self._retry_run(suffix.removesuffix("/retry").strip("/"))
                return
        if path.startswith("/api/v1/media/projects/") and path.endswith(
            "/approve"
        ):
            run_id = path.removeprefix(
                "/api/v1/media/projects/"
            ).removesuffix("/approve").strip("/")
            self._approve_media_project(run_id)
            return
        if path.startswith("/api/v1/media/shots/") and path.endswith(
            "/approve"
        ):
            shot_id = unquote(
                path.removeprefix(
                    "/api/v1/media/shots/"
                ).removesuffix("/approve").strip("/")
            )
            self._approve_media_shot(shot_id)
            return
        if path.startswith("/api/v1/approvals/") and path.endswith(
            "/resolve"
        ):
            approval_id = path.removeprefix(
                "/api/v1/approvals/"
            ).removesuffix("/resolve").strip("/")
            self._resolve_approval(approval_id)
            return
        if path == "/api/runs":
            self._create_run(v1=False)
            return
        if path == "/api/catalog/models":
            self._catalog_create()
            return
        if path == "/api/catalog/import":
            self._catalog_import()
            return
        if path == "/api/catalog/reset":
            self._catalog_reset()
            return
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802
        path = self._normalise_api_path(urlparse(self.path).path)
        prefix = "/api/catalog/models/"
        if not path.startswith(prefix):
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        model_id = unquote(path.removeprefix(prefix).strip("/"))
        if not model_id:
            self._json({"error": "invalid_model_id"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            payload = self._read_write_json()
            expected = self._expected_revision(payload)
            changes = payload.get("model", payload)
            if changes is payload:
                changes = {
                    key: value for key, value in payload.items()
                    if key != "expected_revision"
                }
            model = self.catalog_store.update(
                model_id,
                changes,
                expected_revision=expected,
            )
            self._json({
                "model": model.to_dict(),
                "metadata": self.catalog_store.payload()["metadata"],
            })
        except Exception as exc:
            self._catalog_error(exc)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parsed = parsed._replace(path=self._normalise_api_path(parsed.path))
        prefix = "/api/catalog/models/"
        if not parsed.path.startswith(prefix):
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            self._validate_write_headers(require_json=False)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
            return
        model_id = unquote(parsed.path.removeprefix(prefix).strip("/"))
        try:
            raw_revision = parse_qs(parsed.query).get("expected_revision", [None])[0]
            expected = int(raw_revision) if raw_revision is not None else None
            self.catalog_store.delete(model_id, expected_revision=expected)
            self._json({
                "deleted": model_id,
                "metadata": self.catalog_store.payload()["metadata"],
            })
        except Exception as exc:
            self._catalog_error(exc)

    def _create_run(self, *, v1: bool = False) -> None:
        try:
            payload = self._read_write_json()
            goal = str(payload.get("goal", "")).strip()
            if len(goal) < 6:
                raise ValueError("任务目标至少需要6个字符")
            try:
                mode = RunMode(payload.get("mode", "balanced"))
            except ValueError as exc:
                raise ValueError(
                    "mode必须是 economy、balanced、turbo 或 quality"
                ) from exc
            simulate_replan = payload.get("simulate_replan", False)
            if not isinstance(simulate_replan, bool):
                raise ValueError("simulate_replan必须是布尔值")
            request = RunRequest(
                goal=goal,
                mode=mode,
                budget=float(payload.get("budget", 1.0)),
                quality_floor=float(payload.get("quality_floor", .78)),
                max_concurrency=int(payload.get("max_concurrency", 4)),
                simulate_replan=simulate_replan,
                deadline_seconds=(
                    float(payload["deadline_seconds"])
                    if payload.get("deadline_seconds") is not None
                    else None
                ),
                recovery_budget_ratio=float(
                    payload.get("recovery_budget_ratio", 0.2)
                ),
                value_of_time_per_second=(
                    float(payload["value_of_time_per_second"])
                    if payload.get("value_of_time_per_second") is not None
                    else None
                ),
                max_recoveries=int(
                    payload.get("max_recoveries", 2)
                ),
                max_task_attempts=int(
                    payload.get("max_task_attempts", 2)
                ),
                recovery_time_limit_seconds=float(
                    payload.get(
                        "recovery_time_limit_seconds", 30.0
                    )
                ),
            )
            key = self.headers.get("Idempotency-Key")
            if v1 and not key:
                raise RequestError(
                    "v1 创建运行必须提供 Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            record = self.manager.create(request, idempotency_key=key)
            self._json(record.snapshot(), HTTPStatus.ACCEPTED)
        except (RevisionConflictError, IdempotencyConflictError) as exc:
            self._runtime_conflict(exc)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "invalid_request", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _cancel_run(self, run_id: str) -> None:
        try:
            payload = self._read_write_json()
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "取消运行必须提供 Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            expected = payload.get("expected_revision")
            if isinstance(expected, bool) or expected is None:
                raise ValueError("expected_revision 必须是整数")
            record = self.manager.cancel(
                run_id,
                idempotency_key=key,
                expected_revision=int(expected),
            )
            self._json(record.snapshot(), HTTPStatus.ACCEPTED)
        except KeyError:
            self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
        except (RevisionConflictError, IdempotencyConflictError) as exc:
            self._runtime_conflict(exc)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "invalid_request", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _retry_run(self, run_id: str) -> None:
        try:
            self._read_write_json()
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "重试运行必须提供 Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            record = self.manager.retry(run_id, idempotency_key=key)
            self._json(record.snapshot(), HTTPStatus.ACCEPTED)
        except KeyError:
            self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
        except (RevisionConflictError, IdempotencyConflictError) as exc:
            self._runtime_conflict(exc)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "invalid_request", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _runtime_conflict(self, exc: Exception) -> None:
        code = (
            "runtime_revision_conflict"
            if isinstance(exc, RevisionConflictError)
            else "idempotency_conflict"
        )
        payload = {"error": code, "message": str(exc)}
        if isinstance(exc, RevisionConflictError):
            payload.update({"expected_revision": exc.expected, "current_revision": exc.actual})
        self._json(payload, HTTPStatus.CONFLICT)

    def _approve_media_project(self, run_id: str) -> None:
        try:
            self._read_write_json()
            if not self.headers.get("Idempotency-Key"):
                raise RequestError(
                    "批准媒体项目必须提供 Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            self._json(
                self.manager.approve_media_project(
                    run_id,
                    idempotency_key=self.headers.get("Idempotency-Key"),
                ),
                HTTPStatus.ACCEPTED,
            )
        except KeyError:
            self._json(
                {"error": "media_project_not_found"},
                HTTPStatus.NOT_FOUND,
            )
        except RequestError as exc:
            self._json(
                {"error": exc.code, "message": str(exc)}, exc.status
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "invalid_request", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _approve_media_shot(self, shot_id: str) -> None:
        try:
            self._read_write_json()
            if not self.headers.get("Idempotency-Key"):
                raise RequestError(
                    "批准镜头必须提供 Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            self._json(
                self.manager.approve_media_shot(
                    shot_id,
                    idempotency_key=self.headers.get("Idempotency-Key"),
                ),
                HTTPStatus.ACCEPTED,
            )
        except KeyError:
            self._json(
                {"error": "media_shot_not_found"},
                HTTPStatus.NOT_FOUND,
            )
        except RequestError as exc:
            self._json(
                {"error": exc.code, "message": str(exc)}, exc.status
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "invalid_request", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _resolve_approval(self, approval_id: str) -> None:
        try:
            payload = self._read_write_json()
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "审批决议必须提供 Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            expected = payload.get("expected_revision")
            if isinstance(expected, bool) or expected is None:
                raise ValueError("expected_revision 必须是整数")
            approval = self.manager.resolve_approval(
                approval_id,
                decision=str(payload.get("decision", "")),
                note=str(payload.get("note", ""))[:2000],
                expected_revision=int(expected),
                idempotency_key=key,
            )
            self._json(approval)
        except KeyError:
            self._json(
                {"error": "approval_not_found"}, HTTPStatus.NOT_FOUND
            )
        except ApprovalConflictError as exc:
            self._json(
                {"error": "approval_revision_conflict", "message": str(exc)},
                HTTPStatus.CONFLICT,
            )
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (ApprovalError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "invalid_request", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _catalog_create(self) -> None:
        try:
            payload = self._read_write_json()
            expected = self._expected_revision(payload)
            model_payload = payload.get("model")
            if not isinstance(model_payload, dict):
                raise CatalogError("model 必须是对象")
            model = self.catalog_store.create(
                model_payload,
                expected_revision=expected,
            )
            self._json({
                "model": model.to_dict(),
                "metadata": self.catalog_store.payload()["metadata"],
            }, HTTPStatus.CREATED)
        except Exception as exc:
            self._catalog_error(exc)

    def _catalog_import(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=2_000_000)
            expected = self._expected_revision(payload)
            result = self.catalog_store.import_data(
                payload.get("data"),
                format=str(payload.get("format", "json")).lower(),
                mode=str(payload.get("mode", "merge")).lower(),
                expected_revision=expected,
            )
            self._json({
                "result": result,
                "metadata": self.catalog_store.payload()["metadata"],
            })
        except Exception as exc:
            self._catalog_error(exc)

    def _catalog_reset(self) -> None:
        try:
            payload = self._read_write_json()
            expected = self._expected_revision(payload)
            if payload.get("confirm") != "RESET":
                raise CatalogError("恢复默认需要 confirm=RESET")
            self.catalog_store.reset(expected_revision=expected)
            self._json(self.catalog_store.payload())
        except Exception as exc:
            self._catalog_error(exc)

    @staticmethod
    def _expected_revision(payload: dict) -> int | None:
        raw = payload.get("expected_revision")
        if raw is None:
            return None
        if isinstance(raw, bool):
            raise CatalogError("expected_revision 必须是整数")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise CatalogError("expected_revision 必须是整数") from exc

    def _catalog_error(self, exc: Exception) -> None:
        if isinstance(exc, CatalogConflictError):
            status = HTTPStatus.CONFLICT
            code = "catalog_revision_conflict"
        elif isinstance(exc, CatalogError):
            status = HTTPStatus.UNPROCESSABLE_ENTITY
            code = "catalog_validation_failed"
        elif isinstance(exc, RequestError):
            status = exc.status
            code = exc.code
        elif isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)):
            status = HTTPStatus.BAD_REQUEST
            code = "invalid_request"
        else:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            code = "catalog_write_failed"
        self._json({"error": code, "message": str(exc)}, status)

    def _read_write_json(self, *, max_bytes: int = 256_000) -> dict:
        self._validate_write_headers(require_json=True)
        payload = self._read_json(max_bytes=max_bytes)
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是JSON对象")
        return payload

    def _validate_write_headers(self, *, require_json: bool) -> None:
        if require_json:
            content_type = (
                self.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if content_type != "application/json":
                raise RequestError(
                    "Content-Type 必须是 application/json",
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "unsupported_media_type",
                )
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
                "127.0.0.1", "localhost", "::1",
            }:
                raise RequestError(
                    "只允许本地页面发起写请求",
                    HTTPStatus.FORBIDDEN,
                    "cross_origin_forbidden",
                )

        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise RequestError(
                "cross-site write requests are forbidden",
                HTTPStatus.FORBIDDEN,
                "cross_origin_forbidden",
            )
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        if host and host not in {"127.0.0.1", "localhost", "::1"}:
            raise RequestError(
                "invalid local Host header",
                HTTPStatus.FORBIDDEN,
                "host_forbidden",
            )

    def _read_json(self, *, max_bytes: int = 64_000) -> object:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块请求体")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            length = 0
        else:
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValueError("Content-Length无效") from exc
        if length < 0:
            raise ValueError("Content-Length不能为负数")
        if length > max_bytes:
            raise ValueError("请求体过大")
        try:
            raw = self.rfile.read(length)
        except (TimeoutError, socket.timeout) as exc:
            raise RequestError(
                "request body timed out",
                HTTPStatus.REQUEST_TIMEOUT,
                "request_timeout",
            ) from exc
        if len(raw) != length:
            raise ValueError("request body ended before Content-Length")
        payload = json.loads(raw or b"{}")
        self._validate_json_shape(payload)
        return payload

    @staticmethod
    def _validate_json_shape(value: object) -> None:
        nodes = 0
        stack: list[tuple[object, int]] = [(value, 1)]
        while stack:
            current, depth = stack.pop()
            nodes += 1
            if nodes > 10_000:
                raise ValueError("JSON contains too many values")
            if depth > 32:
                raise ValueError("JSON nesting is too deep")
            if isinstance(current, dict):
                stack.extend((item, depth + 1) for item in current.values())
            elif isinstance(current, list):
                stack.extend((item, depth + 1) for item in current)

    def _normalise_api_path(self, path: str) -> str:
        self._legacy_api = (
            path.startswith("/api/")
            and not path.startswith("/api/v1/")
            and path != "/api/health"
        )
        if path == "/api/v1/catalog":
            return "/api/catalog"
        if path.startswith("/api/v1/catalog/"):
            return "/api/catalog/" + path.removeprefix("/api/v1/catalog/")
        return path

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        if self._legacy_api:
            self.send_header("Deprecation", "true")
            self.send_header("Sunset", "Wed, 31 Dec 2026 23:59:59 GMT")
            self.send_header("Link", '</api/v1>; rel="successor-version"')

    def _json(
        self,
        payload: object,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, content_type: str, filename: str) -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{filename}"',
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _create_runtime_backup(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=8_000)
            if payload.get("confirm") != "BACKUP":
                raise ValueError("confirm must equal BACKUP")
            if not self.headers.get("Idempotency-Key"):
                raise RequestError(
                    "backup requires Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            kernel = self.manager.control_kernel
            if kernel is None:
                raise RequestError(
                    "persistent runtime is disabled",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "persistent_runtime_disabled",
                )
            result = RuntimeMaintenance(kernel.store.path).create_backup()
            self._json(result, HTTPStatus.CREATED)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "backup_failed", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _static(self, request_path: str) -> None:
        if "\\" in request_path or ".." in request_path.split("/"):
            self._json({"error": "invalid_path"}, HTTPStatus.BAD_REQUEST)
            return
        clean = posixpath.normpath(request_path).lstrip("/") or "index.html"
        if clean.startswith("..") or "/../" in clean:
            self._json({"error": "invalid_path"}, HTTPStatus.BAD_REQUEST)
            return
        target = WEB_ROOT.joinpath(clean)
        if not target.is_file():
            target = WEB_ROOT.joinpath("index.html")
        data = target.read_bytes()
        mime, _ = mimetypes.guess_type(str(target))
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            f"{mime or 'application/octet-stream'}; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Cache-Control",
            "no-cache" if clean == "index.html" else "public, max-age=300",
        )
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def build_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    manager: RunManager | None = None,
    catalog_store: CatalogStore | None = None,
    runtime_db_path: str | None = None,
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "离线原型仅允许绑定本机回环地址；公开监听需要先实现身份认证"
        )
    store = catalog_store or CATALOG_STORE
    server = CostWeaveServer((host, port), CostWeaveHandler)
    server.catalog_store = store  # type: ignore[attr-defined]
    if runtime_db_path is None and store is not CATALOG_STORE:
        runtime_db_path = str(store.path.with_name("runtime-v4.sqlite3"))
    server.manager = manager or RunManager(  # type: ignore[attr-defined]
        store,
        runtime_db_path=runtime_db_path,
    )
    return server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the CostWeave v0.5.7.4 bounded canary prototype"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    server = build_server(args.host, args.port)
    print(f"CostWeave running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
