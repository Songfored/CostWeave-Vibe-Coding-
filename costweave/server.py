from __future__ import annotations

import argparse
import json
import math
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
from .gateway_openai import public_openai_gateway_policy
from .gateway_litellm import public_litellm_gateway_policy
from .gateway_local import public_local_endpoint_policy
from .experimental_calls import (
    EXPERIMENTAL_CALL_POLICY_REVISION,
    EXPERIMENTAL_CALL_SCHEMA_VERSION,
    ExternalIdempotencyError,
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
from .evidence_console import (
    EvidenceConsoleError,
    build_evidence_console,
    build_evidence_export,
    public_evidence_console_policy,
)
from .evidence_console_store import EvidenceConsoleStoreError
from .security_boundary import (
    DEFAULT_DATA_RESIDENCY,
    DEFAULT_PROJECT_ID,
    normalize_data_residency,
    normalize_project_id,
    public_security_policy,
    redact_structure,
    redact_text,
)
from .security_store import ProjectRevokedError, SecurityStoreError
from .resilience import public_resilience_policy
from .resilience_store import ResilienceStoreError
from .release_readiness import build_release_status, public_release_policy
from .release_store import ReleaseStoreError
from .tournament_service import OfflineTournamentService, TournamentServiceError
from .decision_intelligence_store import DecisionIntelligenceStoreError
from .meta_controller_service import (
    MetaControllerServiceError,
    RationalMetaControllerService,
)
from .meta_controller_store import MetaControllerStoreError
from .capability_graph_service import (
    CapabilityGraphService,
    CapabilityGraphServiceError,
)
from .capability_graph_store import CapabilityGraphStoreError
from .joint_search_service import JointSearchService, JointSearchServiceError
from .joint_search_store import JointSearchStoreError
from .execution_recovery_service import (
    ExecutionRecoveryService,
    ExecutionRecoveryServiceError,
)
from .execution_recovery_store import ExecutionRecoveryStoreError
from .safe_learning_service import SafeLearningService, SafeLearningServiceError
from .safe_learning_store import SafeLearningStoreError
from .observability import public_observability_policy
from .runtime_backend import (
    LocalRuntimeBackend,
    RuntimeBackendContractError,
    RuntimeBackendUnavailableError,
    RuntimeSubmitRequestV1,
    public_runtime_backend_policy,
)
from .small_budget_shadow import (
    ShadowExperimentConfigV1,
    ShadowProbeRequestV1,
    SmallBudgetShadowError,
    SmallBudgetShadowNotFoundError,
    SmallBudgetShadowRuntime,
    public_small_budget_shadow_policy,
)


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
    server_version = "CostWeave/0.6.6"

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

    @property
    def runtime_backend(self) -> LocalRuntimeBackend:
        return LocalRuntimeBackend(self.manager)

    @property
    def small_budget_shadow(self) -> SmallBudgetShadowRuntime:
        runtime = getattr(self.server, "small_budget_shadow", None)
        if runtime is None:
            raise RuntimeError("small-budget shadow runtime is unavailable")
        return runtime

    def _small_budget_shadow_state(self) -> dict[str, Any]:
        runtime = getattr(self.server, "small_budget_shadow", None)
        if runtime is None:
            return {
                "runtime_available": False,
                "adapter_policy": AdapterRuntimePolicy.from_environment().public(),
                "runtime_counts": {
                    "configured_sessions": 0,
                    "armed_sessions": 0,
                    "external_calls": 0,
                    "external_attempts": 0,
                    "route_interventions": 0,
                    "production_promotions": 0,
                },
            }
        return {
            "runtime_available": True,
            "adapter_policy": runtime.adapter_policy.public(),
            "runtime_counts": runtime.counts(),
        }

    def _project_id(self) -> str:
        return normalize_project_id(
            self.headers.get("X-CostWeave-Project", DEFAULT_PROJECT_ID)
        )

    def _data_residency(self) -> str:
        return normalize_data_residency(
            self.headers.get(
                "X-CostWeave-Data-Residency", DEFAULT_DATA_RESIDENCY
            )
        )

    def _get_run(self, run_id: str):
        return self.manager.get(run_id, project_id=self._project_id())

    def _shot_visible(self, shot_id: str) -> bool:
        for run in self.manager.list(project_id=self._project_id()):
            if any(
                str(item.get("shot_id")) == shot_id
                for item in self.manager.media_shots(str(run.get("id")))
            ):
                return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._project_id()
            self._data_residency()
        except ValueError as exc:
            self._json(
                {"error": "invalid_project_scope", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )
            return
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
                # Keep the released v0.5.8.4 compatibility label while
                # Expose the active v0.6.6 development line explicitly.
                "version": "0.5.8.4",
                "development_version": __version__,
                "version_line": {
                    "stable_baseline": "v0.5.8.4",
                    "previous_lab": "v0.6.0",
                    "active_development": "v0.6.6",
                    "active_work_package": "V066-WP07",
                },
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
                "resilience": public_resilience_policy(),
                "stable_release": public_release_policy(),
                    "openai_gateway": public_openai_gateway_policy(),
                    "litellm_gateway": public_litellm_gateway_policy(),
                    "local_endpoint_gateway": public_local_endpoint_policy(),
                "unified_evidence_console": public_evidence_console_policy(),
                "security_boundary": public_security_policy(
                    runtime_counts=dict(runtime_health.get("security") or {})
                ),
                **contracts,
                "control_kernel": runtime_health,
                "meta_controller": {
                    "schema_version": "v0.6.1",
                    "policy_version": "rational-meta-controller-v1",
                    "advisory_only": True,
                    "provider_calls": 0,
                    "network_calls": 0,
                },
                "capability_router": {
                    "schema_version": "v0.6.2",
                    "policy_version": "capability-evidence-graph-router-v1",
                    "advisory_only": True,
                    "provider_calls": 0,
                    "network_calls": 0,
                },
                "joint_search": {
                    "schema_version": "v0.6.3",
                    "policy_version": "joint-plan-route-schedule-search-v1",
                    "advisory_only": True,
                    "production_route_intervention": False,
                    "provider_calls": 0,
                    "network_calls": 0,
                },
                "execution_recovery": {
                    "schema_version": "v0.6.4",
                    "policy_version": "execution-time-belief-economic-recovery-v1",
                    "advisory_only": True,
                    "production_route_intervention": False,
                    "provider_calls": 0,
                    "network_calls": 0,
                },
                "safe_learning": {
                    "schema_version": "v0.6.5",
                    "policy_version": "trace-credit-assignment-safe-learning-v1",
                    "advisory_only": True,
                    "training_enabled": False,
                    "route_intervention": False,
                    "provider_calls": 0,
                    "network_calls": 0,
                },
                "observability": public_observability_policy(),
                "runtime_backend": public_runtime_backend_policy(),
                "small_budget_shadow": {
                    **public_small_budget_shadow_policy(),
                    **self._small_budget_shadow_state(),
                },
            })
            return
        if path == "/api/v1/runtime/shadow/status":
            self._json({
                "policy": public_small_budget_shadow_policy(),
                **self._small_budget_shadow_state(),
            })
            return
        if path.startswith("/api/v1/runtime/shadow/experiments/"):
            experiment_id = unquote(
                path.removeprefix(
                    "/api/v1/runtime/shadow/experiments/"
                ).strip("/")
            )
            if experiment_id and "/" not in experiment_id:
                try:
                    self._json(
                        self.small_budget_shadow.summary(
                            experiment_id,
                            project_id=self._project_id(),
                        )
                    )
                except SmallBudgetShadowNotFoundError:
                    self._json(
                        {"error": "shadow_experiment_not_found"},
                        HTTPStatus.NOT_FOUND,
                    )
                except RuntimeError as exc:
                    self._json(
                        {
                            "error": "shadow_runtime_unavailable",
                            "message": str(exc),
                        },
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                except (SmallBudgetShadowError, ValueError, TypeError) as exc:
                    self._json(
                        {
                            "error": "shadow_experiment_invalid",
                            "message": str(exc),
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
                return
        if path == "/api/v1/runtime/status":
            self._json({
                "backend": self.runtime_backend.capabilities.to_dict(),
                "policy": public_runtime_backend_policy(),
            })
            return
        if path.startswith("/api/v1/runtime/"):
            suffix = path.removeprefix("/api/v1/runtime/").strip("/")
            if suffix.endswith("/events"):
                run_id = unquote(suffix.removesuffix("/events").strip("/"))
                try:
                    query = parse_qs(parsed.query)
                    after = int(query.get("after_revision", ["0"])[0])
                    limit = int(query.get("limit", ["200"])[0])
                    page = self.runtime_backend.events(
                        run_id,
                        project_id=self._project_id(),
                        after_revision=after,
                        limit=limit,
                    )
                    self._json(page.to_dict())
                except KeyError:
                    self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
                except RuntimeBackendUnavailableError as exc:
                    self._json({"error": "persistent_runtime_disabled", "message": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                except (RuntimeBackendContractError, TypeError, ValueError) as exc:
                    self._json({"error": "runtime_events_invalid", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if suffix and "/" not in suffix:
                try:
                    record = self._get_run(unquote(suffix))
                    if record is None:
                        raise KeyError(suffix)
                    self._json(record.snapshot())
                except KeyError:
                    self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
                except RuntimeBackendUnavailableError as exc:
                    self._json({"error": "persistent_runtime_disabled", "message": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
        if path == "/api/v1/meta-controller/status":
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json({"error": "persistent_runtime_disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                self._json(RationalMetaControllerService(kernel.meta_controller_store).status(project_id=self._project_id()))
            except (MetaControllerServiceError, ValueError, RuntimeError) as exc:
                self._json({"error": "meta_controller_status_unavailable", "message": str(exc)}, HTTPStatus.CONFLICT)
            return
        if path.startswith("/api/v1/meta-controller/"):
            decision_id = unquote(path.removeprefix("/api/v1/meta-controller/").strip("/"))
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json({"error": "persistent_runtime_disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                if not decision_id or "/" in decision_id:
                    raise ValueError("invalid meta decision id")
                result = RationalMetaControllerService(kernel.meta_controller_store).get(project_id=self._project_id(), decision_id=decision_id)
                if result is None:
                    self._json({"error": "meta_decision_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(result)
            except (MetaControllerServiceError, ValueError, RuntimeError) as exc:
                self._json({"error": "meta_decision_lookup_failed", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/v1/capability-router/status":
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json({"error": "persistent_runtime_disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                self._json(CapabilityGraphService(kernel.capability_graph_store).status(project_id=self._project_id()))
            except (CapabilityGraphServiceError, ValueError, RuntimeError) as exc:
                self._json({"error": "capability_router_status_unavailable", "message": str(exc)}, HTTPStatus.CONFLICT)
            return
        if path.startswith("/api/v1/capability-router/"):
            decision_id = unquote(path.removeprefix("/api/v1/capability-router/").strip("/"))
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json({"error": "persistent_runtime_disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                if not decision_id or "/" in decision_id or decision_id in {"status", "decide", "evidence"}:
                    raise ValueError("invalid capability route decision id")
                result = CapabilityGraphService(kernel.capability_graph_store).get(project_id=self._project_id(), decision_id=decision_id)
                if result is None:
                    self._json({"error": "capability_decision_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(result)
            except (CapabilityGraphServiceError, ValueError, RuntimeError) as exc:
                self._json({"error": "capability_decision_lookup_failed", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/v1/joint-search/status":
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json({"error": "persistent_runtime_disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                self._json(JointSearchService(kernel.joint_search_store).status(project_id=self._project_id()))
            except (JointSearchServiceError, ValueError, RuntimeError) as exc:
                self._json({"error": "joint_search_status_unavailable", "message": str(exc)}, HTTPStatus.CONFLICT)
            return
        if path.startswith("/api/v1/joint-search/"):
            decision_id = unquote(path.removeprefix("/api/v1/joint-search/").strip("/"))
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json({"error": "persistent_runtime_disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                if not decision_id or "/" in decision_id or decision_id in {"status", "decide"}:
                    raise ValueError("invalid joint search decision id")
                result = JointSearchService(kernel.joint_search_store).get(project_id=self._project_id(), decision_id=decision_id)
                if result is None:
                    self._json({"error": "joint_search_decision_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(result)
            except (JointSearchServiceError, ValueError, RuntimeError) as exc:
                self._json({"error": "joint_search_decision_lookup_failed", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/v1/execution-recovery/status":
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json({"error": "persistent_runtime_disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                self._json(ExecutionRecoveryService(kernel.execution_recovery_store).status(project_id=self._project_id()))
            except (ExecutionRecoveryServiceError, ValueError, RuntimeError) as exc:
                self._json({"error": "execution_recovery_status_unavailable", "message": str(exc)}, HTTPStatus.CONFLICT)
            return
        if path.startswith("/api/v1/execution-recovery/"):
            decision_id = unquote(path.removeprefix("/api/v1/execution-recovery/").strip("/"))
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json({"error": "persistent_runtime_disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                if not decision_id or "/" in decision_id or decision_id in {"status", "decide"}:
                    raise ValueError("invalid execution recovery decision id")
                result = ExecutionRecoveryService(kernel.execution_recovery_store).get(project_id=self._project_id(), decision_id=decision_id)
                if result is None:
                    self._json({"error": "execution_recovery_decision_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(result)
            except (ExecutionRecoveryServiceError, ValueError, RuntimeError) as exc:
                self._json({"error": "execution_recovery_decision_lookup_failed", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/v1/safe-learning/status":
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json({"error": "persistent_runtime_disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                self._json(SafeLearningService(kernel.safe_learning_store).status(project_id=self._project_id()))
            except (SafeLearningServiceError, ValueError, RuntimeError) as exc:
                self._json({"error": "safe_learning_status_unavailable", "message": str(exc)}, HTTPStatus.CONFLICT)
            return
        if path.startswith("/api/v1/safe-learning/"):
            record_id = unquote(path.removeprefix("/api/v1/safe-learning/").strip("/"))
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json({"error": "persistent_runtime_disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                if not record_id or "/" in record_id or record_id in {"status", "credit", "replay", "evaluate"}:
                    raise ValueError("invalid safe-learning record id")
                result = SafeLearningService(kernel.safe_learning_store).get(project_id=self._project_id(), record_id=record_id)
                if result is None:
                    self._json({"error": "safe_learning_record_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(result)
            except (SafeLearningServiceError, ValueError, RuntimeError) as exc:
                self._json({"error": "safe_learning_record_lookup_failed", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/v1/tournaments/status":
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json(
                    {"error": "persistent_runtime_disabled"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            try:
                self._json(OfflineTournamentService(kernel.decision_intelligence_store).status())
            except (TournamentServiceError, ValueError, RuntimeError) as exc:
                self._json(
                    {"error": "tournament_status_unavailable", "message": str(exc)},
                    HTTPStatus.CONFLICT,
                )
            return
        if path.startswith("/api/v1/tournaments/"):
            tournament_id = unquote(path.removeprefix("/api/v1/tournaments/").strip("/"))
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json(
                    {"error": "persistent_runtime_disabled"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            try:
                if not tournament_id or "/" in tournament_id:
                    raise ValueError("invalid tournament id")
                result = OfflineTournamentService(kernel.decision_intelligence_store).get(
                    project_id=self._project_id(), tournament_id=tournament_id
                )
                if result is None:
                    self._json({"error": "tournament_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(result)
            except (TournamentServiceError, ValueError, RuntimeError) as exc:
                self._json(
                    {"error": "tournament_lookup_failed", "message": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if path == "/api/v1/evidence/console":
            run_id = parse_qs(parsed.query).get("run_id", [None])[0]
            try:
                self._json(
                    build_evidence_console(
                        self.manager,
                        run_id=run_id,
                        project_id=self._project_id(),
                    )
                )
            except KeyError:
                self._json(
                    {"error": "run_not_found", "run_id": run_id},
                    HTTPStatus.NOT_FOUND,
                )
            except EvidenceConsoleError as exc:
                self._json(
                    {"error": "evidence_console_failed", "message": str(exc)},
                    HTTPStatus.CONFLICT,
                )
            return
        if path == "/api/v1/evidence/exports":
            try:
                limit = int(parse_qs(parsed.query).get("limit", ["100"])[0])
                self._json({
                    "exports": self.manager.evidence_exports(
                        limit=limit,
                        project_id=self._project_id(),
                    )
                })
            except (TypeError, ValueError, EvidenceConsoleStoreError) as exc:
                self._json(
                    {"error": "invalid_export_query", "message": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if path == "/api/v1/security/policy":
            kernel = self.manager.control_kernel
            counts = kernel.security_store.counts() if kernel is not None else {}
            self._json({
                **public_security_policy(runtime_counts=counts),
                "project": (
                    kernel.project_status(self._project_id())
                    if kernel is not None
                    else {
                        "project_id": self._project_id(),
                        "status": "active",
                    }
                ),
            })
            return
        if path == "/api/v1/resilience/status":
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json(
                    {"error": "persistent_runtime_disabled"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            try:
                project_id = self._project_id()
                kernel.security_store.ensure_project_active(project_id)
                maintenance = RuntimeMaintenance(kernel.store.path)
                self._json({
                    **public_resilience_policy(),
                    "project_id": project_id,
                    "counts": kernel.resilience_store.counts(),
                    "integrity": kernel.resilience_store.integrity_audit(),
                    "runtime": maintenance.diagnostics(),
                    "campaigns": kernel.resilience_store.list_campaigns(
                        project_id=project_id, limit=25
                    ),
                })
            except (ProjectRevokedError, ResilienceStoreError, RuntimeError) as exc:
                self._json(
                    {"error": "resilience_status_unavailable", "message": str(exc)},
                    HTTPStatus.CONFLICT,
                )
            return
        if path == "/api/v1/release/status":
            kernel = self.manager.control_kernel
            if kernel is None:
                self._json(
                    {"error": "persistent_runtime_disabled"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            try:
                project_id = self._project_id()
                kernel.security_store.ensure_project_active(project_id)
                self._json({
                    **build_release_status(
                        kernel,
                        adapter_policy=(
                            AdapterRuntimePolicy.from_environment().public()
                        ),
                        litellm_policy=public_litellm_gateway_policy(),
                        local_policy=public_local_endpoint_policy(),
                    ),
                    "project_id": project_id,
                    "attestations": kernel.release_store.list_attestations(
                        project_id=project_id, limit=25
                    ),
                })
            except (ProjectRevokedError, ReleaseStoreError, RuntimeError) as exc:
                self._json(
                    {"error": "release_status_unavailable", "message": str(exc)},
                    HTTPStatus.CONFLICT,
                )
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
            self._json({"runs": self.manager.list(project_id=self._project_id())})
            return
        if path == "/api/v1/resources/ledger":
            run_id = parse_qs(parsed.query).get("run_id", [None])[0]
            if run_id is not None and self._get_run(run_id) is None:
                self._json(
                    {"error": "run_not_found"}, HTTPStatus.NOT_FOUND
                )
                return
            entries = self.manager.ledger(
                run_id=run_id, project_id=self._project_id()
            )
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
                {
                    "approvals": self.manager.approvals(
                        status=status,
                        project_id=self._project_id(),
                    )
                }
            )
            return
        if path == "/api/v1/evaluations":
            evaluations = [
                item for item in self.manager.evaluations()
                if not item.get("run_id")
                or self._get_run(str(item.get("run_id"))) is not None
            ]
            self._json({"evaluations": evaluations})
            return
        if path.startswith("/api/v1/media/projects/"):
            suffix = path.removeprefix(
                "/api/v1/media/projects/"
            ).strip("/")
            if suffix.endswith("/shots"):
                run_id = suffix.removesuffix("/shots").strip("/")
                if self._get_run(run_id) is None:
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
            if self._get_run(run_id) is None:
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
            if self._get_run(str(task.get("run_id") or "")) is None:
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
            if self._get_run(str(artifact.get("run_id") or "")) is None:
                self._json(
                    {"error": "artifact_not_found"}, HTTPStatus.NOT_FOUND
                )
                return
            self._json(artifact)
            return
        if path.startswith("/api/v1/runs/"):
            suffix = path.removeprefix("/api/v1/runs/").strip("/")
            scoped_run_id = suffix.split("/", 1)[0]
            if self._get_run(scoped_run_id) is None:
                self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
                return
            if suffix.endswith("/handoffs"):
                run_id = suffix.removesuffix(
                    "/handoffs"
                ).strip("/")
                if self._get_run(run_id) is None:
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
                record = self._get_run(run_id)
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
                if self._get_run(run_id) is None:
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
                if self._get_run(run_id) is None:
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
                if self._get_run(run_id) is None:
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
                if self._get_run(run_id) is None:
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
                if self._get_run(run_id) is None:
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
            if suffix.endswith("/trace"):
                run_id = suffix.removesuffix("/trace").strip("/")
                trace = self.manager.trace(run_id)
                if trace is None:
                    self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
                    return
                self._json(trace)
                return
            if suffix.endswith("/events"):
                run_id = suffix.removesuffix("/events").strip("/")
                record = self._get_run(run_id)
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
            record = self._get_run(suffix)
            if not record:
                self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(record.snapshot())
            return
        if path.startswith("/api/runs/"):
            run_id = path.removeprefix("/api/runs/").strip("/")
            record = self._get_run(run_id)
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
        try:
            self._project_id()
            self._data_residency()
        except ValueError as exc:
            self._json(
                {"error": "invalid_project_scope", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )
            return
        path = self._normalise_api_path(urlparse(self.path).path)
        if path.startswith("/api/v1/projects/") and path.endswith("/revoke"):
            self._revoke_project(
                unquote(
                    path.removeprefix("/api/v1/projects/")
                    .removesuffix("/revoke")
                    .strip("/")
                )
            )
            return
        if path == "/api/v1/evidence/exports":
            self._create_evidence_export()
            return
        if path == "/api/v1/tournaments/offline":
            self._run_offline_tournament()
            return
        if path == "/api/v1/meta-controller/decide":
            self._run_meta_controller()
            return
        if path == "/api/v1/capability-router/evidence":
            self._ingest_capability_evidence()
            return
        if path == "/api/v1/capability-router/decide":
            self._run_capability_router()
            return
        if path == "/api/v1/joint-search/decide":
            self._run_joint_search()
            return
        if path == "/api/v1/execution-recovery/decide":
            self._run_execution_recovery()
            return
        if path == "/api/v1/safe-learning/credit":
            self._run_safe_learning_credit()
            return
        if path == "/api/v1/safe-learning/replay":
            self._run_safe_learning_replay()
            return
        if path == "/api/v1/safe-learning/evaluate":
            self._run_safe_learning_evaluate()
            return
        if path == "/api/v1/capability-router/drift":
            self._run_capability_drift()
            return
        if path == "/api/v1/system/backups":
            self._create_runtime_backup()
            return
        if path == "/api/v1/resilience/campaigns":
            self._create_resilience_campaign()
            return
        if path == "/api/v1/release/attestations":
            self._create_release_attestation()
            return
        if path == "/api/v1/runtime/shadow/experiments":
            self._configure_small_budget_shadow()
            return
        if (
            path.startswith("/api/v1/runtime/shadow/experiments/")
            and path.endswith("/probes")
        ):
            experiment_id = unquote(
                path.removeprefix(
                    "/api/v1/runtime/shadow/experiments/"
                ).removesuffix("/probes").strip("/")
            )
            if experiment_id and "/" not in experiment_id:
                self._run_small_budget_shadow_probe(experiment_id)
                return
        if path == "/api/v1/runtime/submit":
            self._runtime_submit()
            return
        if path.startswith("/api/v1/runtime/"):
            suffix = path.removeprefix("/api/v1/runtime/").strip("/")
            parts = [unquote(item) for item in suffix.split("/") if item]
            if len(parts) == 2 and parts[1] in {"cancel", "checkpoint", "resume"}:
                self._runtime_action(parts[0], parts[1])
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

    def _revoke_project(self, project_id: str) -> None:
        try:
            payload = self._read_write_json(max_bytes=8_000)
            project = normalize_project_id(project_id)
            if project != self._project_id():
                raise RequestError(
                    "project header and revocation target disagree",
                    HTTPStatus.FORBIDDEN,
                    "project_scope_forbidden",
                )
            if payload.get("confirm") != f"REVOKE {project}":
                raise ValueError(
                    f"confirm must equal REVOKE {project}"
                )
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "project revocation requires Idempotency-Key",
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
            tombstone, replayed = kernel.revoke_project(
                project,
                idempotency_key=key,
                reason=str(payload.get("reason") or "user_requested"),
            )
            self._json(
                {
                    "project": tombstone,
                    "replayed": replayed,
                    "warning": (
                        "Access is revoked immediately; physical row erasure "
                        "is not implemented in the current release."
                    ),
                },
                HTTPStatus.ACCEPTED,
            )
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (SecurityStoreError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "project_revocation_failed", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _configure_small_budget_shadow(self) -> None:
        try:
            payload = dict(self._read_write_json(max_bytes=64_000))
            project = self._project_id()
            body_project = normalize_project_id(
                payload.get("project_id", project)
            )
            if body_project != project:
                raise RequestError(
                    "shadow project does not match X-CostWeave-Project",
                    HTTPStatus.FORBIDDEN,
                    "project_scope_forbidden",
                )
            residency = self._data_residency()
            body_residency = normalize_data_residency(
                payload.get("data_residency", residency)
            )
            if body_residency != residency:
                raise RequestError(
                    "shadow data residency does not match the request header",
                    HTTPStatus.FORBIDDEN,
                    "data_residency_forbidden",
                )
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "shadow configuration requires Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            if payload.get("idempotency_key") not in {None, key}:
                raise RequestError(
                    "body and header idempotency keys must match",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_mismatch",
                )
            payload["project_id"] = project
            payload["data_residency"] = residency
            payload["idempotency_key"] = key
            session = self.small_budget_shadow.configure(
                ShadowExperimentConfigV1.from_dict(payload)
            )
            self._json(
                session.to_dict(),
                HTTPStatus.OK if session.replayed else HTTPStatus.CREATED,
            )
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except ExternalIdempotencyError as exc:
            self._json(
                {"error": "shadow_idempotency_conflict", "message": str(exc)},
                HTTPStatus.CONFLICT,
            )
        except RuntimeError as exc:
            self._json(
                {"error": "shadow_runtime_unavailable", "message": str(exc)},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except (SmallBudgetShadowError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "shadow_configuration_invalid", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _run_small_budget_shadow_probe(self, experiment_id: str) -> None:
        try:
            payload = dict(self._read_write_json(max_bytes=128_000))
            project = self._project_id()
            body_project = normalize_project_id(
                payload.get("project_id", project)
            )
            if body_project != project:
                raise RequestError(
                    "shadow probe project does not match X-CostWeave-Project",
                    HTTPStatus.FORBIDDEN,
                    "project_scope_forbidden",
                )
            if payload.get("experiment_id") not in {None, experiment_id}:
                raise RequestError(
                    "path and body shadow experiment ids must match",
                    HTTPStatus.BAD_REQUEST,
                    "shadow_experiment_mismatch",
                )
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "shadow probe requires Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            if payload.get("idempotency_key") not in {None, key}:
                raise RequestError(
                    "body and header idempotency keys must match",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_mismatch",
                )
            payload["experiment_id"] = experiment_id
            payload["project_id"] = project
            payload["idempotency_key"] = key
            receipt = self.small_budget_shadow.probe(
                ShadowProbeRequestV1.from_dict(payload)
            )
            self._json(receipt.to_dict(), HTTPStatus.OK)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except SmallBudgetShadowNotFoundError:
            self._json(
                {"error": "shadow_experiment_not_found"},
                HTTPStatus.NOT_FOUND,
            )
        except ExternalIdempotencyError as exc:
            self._json(
                {"error": "shadow_idempotency_conflict", "message": str(exc)},
                HTTPStatus.CONFLICT,
            )
        except RuntimeError as exc:
            self._json(
                {"error": "shadow_runtime_unavailable", "message": str(exc)},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except (SmallBudgetShadowError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "shadow_probe_invalid", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _runtime_submit(self) -> None:
        try:
            payload = dict(self._read_write_json())
            project = self._project_id()
            body_project = normalize_project_id(payload.get("project_id", project))
            if body_project != project:
                raise RequestError(
                    "runtime submit project does not match X-CostWeave-Project",
                    HTTPStatus.FORBIDDEN,
                    "project_scope_forbidden",
                )
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "runtime submit requires Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            if payload.get("idempotency_key") not in {None, key}:
                raise RequestError(
                    "body and header idempotency keys must match",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_mismatch",
                )
            payload["project_id"] = project
            payload["data_residency"] = self._data_residency()
            payload["idempotency_key"] = key
            request = RuntimeSubmitRequestV1.from_dict(payload)
            operation = self.runtime_backend.submit(request)
            self._json(operation.to_dict(), HTTPStatus.ACCEPTED)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (RevisionConflictError, IdempotencyConflictError) as exc:
            self._runtime_conflict(exc)
        except RuntimeBackendUnavailableError as exc:
            self._json(
                {"error": "persistent_runtime_disabled", "message": str(exc)},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except (RuntimeBackendContractError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json({"error": "runtime_submit_invalid", "message": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _runtime_action(self, run_id: str, action: str) -> None:
        try:
            if self._get_run(run_id) is None:
                raise KeyError(run_id)
            payload = self._read_write_json()
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    f"runtime {action} requires Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            expected = payload.get("expected_revision")
            if isinstance(expected, bool) or not isinstance(expected, int):
                raise RequestError(
                    "expected_revision must be an integer",
                    HTTPStatus.BAD_REQUEST,
                    "invalid_expected_revision",
                )
            kwargs = {
                "project_id": self._project_id(),
                "idempotency_key": key,
                "expected_revision": expected,
            }
            if action == "cancel":
                operation = self.runtime_backend.cancel(run_id, **kwargs)
            elif action == "checkpoint":
                operation = self.runtime_backend.checkpoint(
                    run_id,
                    source=str(payload.get("source") or "client"),
                    **kwargs,
                )
            else:
                operation = self.runtime_backend.resume(
                    run_id,
                    source=str(payload.get("source") or "client"),
                    **kwargs,
                )
            self._json(operation.to_dict(), HTTPStatus.ACCEPTED)
        except KeyError:
            self._json({"error": "run_not_found"}, HTTPStatus.NOT_FOUND)
        except (RevisionConflictError, IdempotencyConflictError) as exc:
            self._runtime_conflict(exc)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except RuntimeBackendUnavailableError as exc:
            self._json(
                {"error": "persistent_runtime_disabled", "message": str(exc)},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except (RuntimeBackendContractError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json({"error": f"runtime_{action}_invalid", "message": str(exc)}, HTTPStatus.BAD_REQUEST)

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
                project_id=self._project_id(),
                data_residency=self._data_residency(),
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
        except (ProjectRevokedError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "invalid_request", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _cancel_run(self, run_id: str) -> None:
        try:
            if self._get_run(run_id) is None:
                raise KeyError(run_id)
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
            if self._get_run(run_id) is None:
                raise KeyError(run_id)
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
            if self._get_run(run_id) is None:
                raise KeyError(run_id)
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
            if not self._shot_visible(shot_id):
                raise KeyError(shot_id)
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
            if not any(
                str(item.get("approval_id")) == approval_id
                for item in self.manager.approvals(
                    project_id=self._project_id()
                )
            ):
                raise KeyError(approval_id)
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

    def _run_offline_tournament(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=16_000)
            allowed = {"confirm", "seed", "bootstrap_samples"}
            unknown = set(payload) - allowed
            if unknown:
                raise TournamentServiceError(f"unknown tournament fields: {sorted(unknown)!r}")
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "offline tournament requires Idempotency-Key",
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
            result, replayed = OfflineTournamentService(
                kernel.decision_intelligence_store
            ).run(
                project_id=self._project_id(),
                idempotency_key=key,
                confirm=payload.get("confirm"),
                bootstrap_samples=payload.get("bootstrap_samples", 2000),
                seed=payload.get("seed", 600),
            )
            self._json(result, HTTPStatus.OK if replayed else HTTPStatus.CREATED)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except DecisionIntelligenceStoreError as exc:
            self._json(
                {"error": "tournament_conflict", "message": str(exc)},
                HTTPStatus.CONFLICT,
            )
        except (TournamentServiceError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
            self._json(
                {"error": "tournament_request_invalid", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _run_meta_controller(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=256_000)
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "MetaController requires Idempotency-Key",
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
            result, replayed = RationalMetaControllerService(kernel.meta_controller_store).decide(
                project_id=self._project_id(),
                idempotency_key=key,
                payload=payload,
            )
            self._json(result, HTTPStatus.OK if replayed else HTTPStatus.CREATED)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except MetaControllerStoreError as exc:
            self._json({"error": "meta_controller_conflict", "message": str(exc)}, HTTPStatus.CONFLICT)
        except (MetaControllerServiceError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
            self._json({"error": "meta_controller_request_invalid", "message": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _ingest_capability_evidence(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=256_000)
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError("capability evidence requires Idempotency-Key", HTTPStatus.BAD_REQUEST, "idempotency_key_required")
            kernel = self.manager.control_kernel
            if kernel is None:
                raise RequestError("persistent runtime is disabled", HTTPStatus.SERVICE_UNAVAILABLE, "persistent_runtime_disabled")
            result, replayed = CapabilityGraphService(kernel.capability_graph_store).ingest(project_id=self._project_id(), idempotency_key=key, payload=payload)
            self._json(result, HTTPStatus.OK if replayed else HTTPStatus.CREATED)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except CapabilityGraphStoreError as exc:
            self._json({"error": "capability_graph_conflict", "message": str(exc)}, HTTPStatus.CONFLICT)
        except (CapabilityGraphServiceError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
            self._json({"error": "capability_evidence_request_invalid", "message": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _run_capability_router(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=256_000)
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError("capability router requires Idempotency-Key", HTTPStatus.BAD_REQUEST, "idempotency_key_required")
            kernel = self.manager.control_kernel
            if kernel is None:
                raise RequestError("persistent runtime is disabled", HTTPStatus.SERVICE_UNAVAILABLE, "persistent_runtime_disabled")
            result, replayed = CapabilityGraphService(kernel.capability_graph_store).decide(project_id=self._project_id(), idempotency_key=key, payload=payload)
            self._json(result, HTTPStatus.OK if replayed else HTTPStatus.CREATED)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except CapabilityGraphStoreError as exc:
            self._json({"error": "capability_graph_conflict", "message": str(exc)}, HTTPStatus.CONFLICT)
        except (CapabilityGraphServiceError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
            self._json({"error": "capability_router_request_invalid", "message": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _run_capability_drift(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=64_000)
            if payload.get("confirm") != "RUN_CAPABILITY_ROUTER":
                raise CapabilityGraphServiceError("confirm must equal RUN_CAPABILITY_ROUTER")
            if not isinstance(payload.get("baseline"), dict) or not isinstance(payload.get("current"), dict):
                raise CapabilityGraphServiceError("baseline and current must be JSON objects")
            kernel = self.manager.control_kernel
            if kernel is None:
                raise RequestError("persistent runtime is disabled", HTTPStatus.SERVICE_UNAVAILABLE, "persistent_runtime_disabled")
            result = CapabilityGraphService(kernel.capability_graph_store).drift(baseline=payload["baseline"], current=payload["current"], now=payload.get("now"))
            self._json(result)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (CapabilityGraphServiceError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
            self._json({"error": "capability_drift_request_invalid", "message": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _run_joint_search(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=512_000)
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError("joint search requires Idempotency-Key", HTTPStatus.BAD_REQUEST, "idempotency_key_required")
            kernel = self.manager.control_kernel
            if kernel is None:
                raise RequestError("persistent runtime is disabled", HTTPStatus.SERVICE_UNAVAILABLE, "persistent_runtime_disabled")
            result, replayed = JointSearchService(kernel.joint_search_store).decide(project_id=self._project_id(), idempotency_key=key, payload=payload)
            self._json(result, HTTPStatus.OK if replayed else HTTPStatus.CREATED)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except JointSearchStoreError as exc:
            self._json({"error": "joint_search_conflict", "message": str(exc)}, HTTPStatus.CONFLICT)
        except (JointSearchServiceError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
            self._json({"error": "joint_search_request_invalid", "message": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _run_execution_recovery(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=512_000)
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError("execution recovery requires Idempotency-Key", HTTPStatus.BAD_REQUEST, "idempotency_key_required")
            kernel = self.manager.control_kernel
            if kernel is None:
                raise RequestError("persistent runtime is disabled", HTTPStatus.SERVICE_UNAVAILABLE, "persistent_runtime_disabled")
            result, replayed = ExecutionRecoveryService(kernel.execution_recovery_store).decide(project_id=self._project_id(), idempotency_key=key, payload=payload)
            self._json(result, HTTPStatus.OK if replayed else HTTPStatus.CREATED)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except ExecutionRecoveryStoreError as exc:
            self._json({"error": "execution_recovery_conflict", "message": str(exc)}, HTTPStatus.CONFLICT)
        except (ExecutionRecoveryServiceError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
            self._json({"error": "execution_recovery_request_invalid", "message": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _run_safe_learning_credit(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=512_000)
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError("safe learning credit requires Idempotency-Key", HTTPStatus.BAD_REQUEST, "idempotency_key_required")
            kernel = self.manager.control_kernel
            if kernel is None:
                raise RequestError("persistent runtime is disabled", HTTPStatus.SERVICE_UNAVAILABLE, "persistent_runtime_disabled")
            result, replayed = SafeLearningService(kernel.safe_learning_store).credit(project_id=self._project_id(), idempotency_key=key, payload=payload)
            self._json(result, HTTPStatus.OK if replayed else HTTPStatus.CREATED)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except SafeLearningStoreError as exc:
            self._json({"error": "safe_learning_conflict", "message": str(exc)}, HTTPStatus.CONFLICT)
        except (SafeLearningServiceError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
            self._json({"error": "safe_learning_request_invalid", "message": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _run_safe_learning_replay(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=512_000)
            kernel = self.manager.control_kernel
            if kernel is None:
                raise RequestError("persistent runtime is disabled", HTTPStatus.SERVICE_UNAVAILABLE, "persistent_runtime_disabled")
            self._json(SafeLearningService(kernel.safe_learning_store).replay(project_id=self._project_id(), payload=payload), HTTPStatus.CREATED)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except SafeLearningStoreError as exc:
            self._json({"error": "safe_learning_conflict", "message": str(exc)}, HTTPStatus.CONFLICT)
        except (SafeLearningServiceError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
            self._json({"error": "safe_learning_request_invalid", "message": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _run_safe_learning_evaluate(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=2_000_000)
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError("safe learning evaluation requires Idempotency-Key", HTTPStatus.BAD_REQUEST, "idempotency_key_required")
            kernel = self.manager.control_kernel
            if kernel is None:
                raise RequestError("persistent runtime is disabled", HTTPStatus.SERVICE_UNAVAILABLE, "persistent_runtime_disabled")
            result, replayed = SafeLearningService(kernel.safe_learning_store).evaluate(project_id=self._project_id(), idempotency_key=key, payload=payload)
            self._json(result, HTTPStatus.OK if replayed else HTTPStatus.CREATED)
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except SafeLearningStoreError as exc:
            self._json({"error": "safe_learning_conflict", "message": str(exc)}, HTTPStatus.CONFLICT)
        except (SafeLearningServiceError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
            self._json({"error": "safe_learning_request_invalid", "message": str(exc)}, HTTPStatus.BAD_REQUEST)

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
        try:
            self._validate_write_headers(require_json=True)
        except RequestError:
            # On Windows, closing a socket with an unread request body can
            # replace the intended JSON 4xx response with WSAECONNABORTED at
            # the client.  Drain only a bounded, already-sent body before the
            # handler writes the rejection; partial/oversized bodies remain
            # fail-closed and force the connection to close.
            self._drain_rejected_body(max_bytes=max_bytes)
            raise
        payload = self._read_json(max_bytes=max_bytes)
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是JSON对象")
        return payload

    def _drain_rejected_body(self, *, max_bytes: int) -> None:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or 0)
        except (TypeError, ValueError):
            self.close_connection = True
            return
        if length <= 0:
            return
        if length > max(0, int(max_bytes)):
            self.close_connection = True
            return
        connection = getattr(self, "connection", None)
        previous_timeout = connection.gettimeout() if connection is not None else None
        try:
            if connection is not None:
                connection.settimeout(0.1)
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(remaining, 65_536))
                if not chunk:
                    self.close_connection = True
                    break
                remaining -= len(chunk)
        except (OSError, TimeoutError, socket.timeout):
            self.close_connection = True
        finally:
            if connection is not None:
                connection.settimeout(previous_timeout)

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
            elif isinstance(current, float) and not math.isfinite(current):
                raise ValueError("JSON numbers must be finite")

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
        body = json.dumps(
            redact_structure(payload),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, content_type: str, filename: str) -> None:
        body = redact_text(text).encode("utf-8")
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

    def _create_resilience_campaign(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=32_000)
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "resilience campaign requires Idempotency-Key",
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                )
            campaign_kind = str(payload.get("campaign_kind") or "").strip()
            spec = payload.get("spec")
            if not isinstance(spec, dict):
                raise ValueError("spec must be an object")
            forbidden = {
                "real_provider_calls", "paid_calls", "external_side_effects"
            }
            if any(bool(spec.get(name)) for name in forbidden):
                raise ValueError(
                    "resilience campaigns are offline/replay only"
                )
            kernel = self.manager.control_kernel
            if kernel is None:
                raise RequestError(
                    "persistent runtime is disabled",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "persistent_runtime_disabled",
                )
            project_id = self._project_id()
            kernel.security_store.ensure_project_active(project_id)
            campaign, replayed = kernel.resilience_store.create_campaign(
                project_id=project_id,
                campaign_kind=campaign_kind,
                spec={**spec, "real_provider_calls": False},
                evidence_level=str(
                    payload.get("evidence_level") or "E0-simulated"
                ),
            )
            self._json(
                {"campaign": campaign, "replayed": replayed},
                HTTPStatus.OK if replayed else HTTPStatus.CREATED,
            )
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (
            ProjectRevokedError,
            ResilienceStoreError,
            RuntimeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            self._json(
                {"error": "resilience_campaign_rejected", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _create_release_attestation(self) -> None:
        try:
            payload = self._read_write_json(max_bytes=8_000)
            if payload.get("confirm") != "ATTEST_STABLE_ENGINEERING":
                raise ValueError(
                    "confirm must equal ATTEST_STABLE_ENGINEERING"
                )
            key = self.headers.get("Idempotency-Key")
            if not key:
                raise RequestError(
                    "release attestation requires Idempotency-Key",
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
            project_id = self._project_id()
            kernel.security_store.ensure_project_active(project_id)
            existing = kernel.release_store.get_by_key(
                project_id=project_id, idempotency_key=key
            )
            if existing is not None:
                self._json(
                    {"attestation": existing, "replayed": True},
                    HTTPStatus.OK,
                )
                return
            manifest = build_release_status(
                kernel,
                adapter_policy=AdapterRuntimePolicy.from_environment().public(),
                litellm_policy=public_litellm_gateway_policy(),
                local_policy=public_local_endpoint_policy(),
            )
            if not manifest["engineering_ready"]:
                raise ValueError(
                    "engineering release gates are not all satisfied"
                )
            attestation, replayed = kernel.release_store.create_attestation(
                project_id=project_id,
                manifest=manifest,
                idempotency_key=key,
            )
            self._json(
                {"attestation": attestation, "replayed": replayed},
                HTTPStatus.OK if replayed else HTTPStatus.CREATED,
            )
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (
            ProjectRevokedError,
            ReleaseStoreError,
            RuntimeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            self._json(
                {"error": "release_attestation_rejected", "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def _create_evidence_export(self) -> None:
        run_id: str | None = None
        try:
            payload = self._read_write_json(max_bytes=8_000)
            raw_run_id = payload.get("run_id")
            run_id = None if raw_run_id in {None, ""} else str(raw_run_id)
            if run_id is not None and (len(run_id) > 160 or not run_id.strip()):
                raise ValueError("run_id is invalid")
            package = build_evidence_export(
                self.manager,
                run_id=run_id,
                project_id=self._project_id(),
                catalog_snapshot=self.catalog_store.payload(),
            )
            receipt, replayed = self.manager.record_evidence_export(
                package, project_id=self._project_id()
            )
            stamp = str(package["generated_at"]).replace(":", "-")
            scope = run_id or "workspace"
            self._json(
                {
                    "filename": f"costweave-evidence-{scope}-{stamp}.json",
                    "receipt": receipt,
                    "replayed": replayed,
                    "package": package,
                },
                HTTPStatus.CREATED,
            )
        except KeyError:
            self._json(
                {"error": "run_not_found", "run_id": run_id},
                HTTPStatus.NOT_FOUND,
            )
        except RequestError as exc:
            self._json({"error": exc.code, "message": str(exc)}, exc.status)
        except (
            EvidenceConsoleError,
            EvidenceConsoleStoreError,
            RuntimeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            self._json(
                {"error": "evidence_export_failed", "message": str(exc)},
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
        print(
            redact_text(
                f"[{self.log_date_time_string()}] {fmt % args}"
            )
        )


def build_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    manager: RunManager | None = None,
    catalog_store: CatalogStore | None = None,
    runtime_db_path: str | None = None,
    small_budget_shadow: SmallBudgetShadowRuntime | None = None,
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
    run_manager = manager or RunManager(
        store,
        runtime_db_path=runtime_db_path,
    )
    server.manager = run_manager  # type: ignore[attr-defined]
    if small_budget_shadow is None:
        kernel = run_manager.control_kernel
        if kernel is not None:
            small_budget_shadow = SmallBudgetShadowRuntime(
                kernel.experimental_call_store
            )
    server.small_budget_shadow = small_budget_shadow  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the CostWeave v0.5.8.4 stable engineering control plane"
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
