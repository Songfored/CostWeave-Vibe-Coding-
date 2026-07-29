"""Persistent command/event/projection owner for CostWeave v0.5.5."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from .commands import CommandKind
from .artifact_store import ArtifactStore
from .artifacts import Artifact, ArtifactStatus
from .approvals import ApprovalStore
from .contracts_v4 import CommandEnvelope, EventEnvelope, canonical_identifier, new_command_id
from .domain import Event, RunRequest, RunStatus, TaskStatus, WorkerProfile
from .event_store import EventStore, ProjectionUpdate
from .graph_compiler import GraphCompiler
from .messages import MessageEnvelope
from .handoff_compiler import (
    HandoffCompiler,
    HandoffPreflightError,
)
from .placements import PlacementStore
from .recovery import EvidenceRecoveryStore
from .outcome_store import OutcomeStore
from .calibration import HierarchicalCalibrator
from .media_runtime import MediaRuntime
from .projections import canonical_task_id, record_from_snapshot, record_to_projection
from .reservations import ReservationManager
from .state_guard import is_terminal, validate_transition


RUNTIME_DB_ENV = "COSTWEAVE_RUNTIME_DB"


class ControlKernel:
    """The only persistent state writer when COSTWEAVE_RUNTIME_V4 is enabled."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.store = EventStore(path or self.default_path())
        self.artifact_store = ArtifactStore(self.store.path)
        self.reservations = ReservationManager(self.store.path)
        self.approval_store = ApprovalStore(self.store.path)
        self.placement_store = PlacementStore(self.store.path)
        self.evidence_store = EvidenceRecoveryStore(self.store.path)
        self.outcome_store = OutcomeStore(self.store.path)
        self.calibrator = HierarchicalCalibrator()
        self.media_runtime = MediaRuntime(self.store.path)
        self.recovered_orphan_leases = self.reservations.release_orphans()
        self.graph_compiler = GraphCompiler()
        self.handoff_compiler = HandoffCompiler()
        self._lock = threading.RLock()

    @staticmethod
    def default_path() -> Path:
        configured = os.environ.get(RUNTIME_DB_ENV)
        if configured:
            return Path(configured)
        return Path.home() / ".costweave" / "runtime-v4.sqlite3"

    def bind(self, record: Any) -> Any:
        record.control_kernel = self
        return record

    def create_run(
        self,
        request: RunRequest,
        *,
        idempotency_key: str,
        retry_of: str | None = None,
    ) -> tuple[Any, bool]:
        run_id = canonical_identifier("run", f"create:{idempotency_key}")
        command = CommandEnvelope.create(
            run_id,
            CommandKind.CREATE_RUN,
            {
                "goal": request.goal,
                "mode": request.mode.value,
                "budget": request.budget,
                "quality_floor": request.quality_floor,
                "max_concurrency": request.max_concurrency,
                "simulate_replan": request.simulate_replan,
                "deadline_seconds": request.deadline_seconds,
                "recovery_budget_ratio": (
                    request.recovery_budget_ratio
                ),
                "value_of_time_per_second": (
                    request.value_of_time_per_second
                ),
                "max_recoveries": request.max_recoveries,
                "max_task_attempts": request.max_task_attempts,
                "recovery_time_limit_seconds": (
                    request.recovery_time_limit_seconds
                ),
                "retry_of": retry_of,
            },
        )
        replay = self.store.replay_command(command, idempotency_key=idempotency_key)
        if replay is not None:
            snapshot = self.store.get_projection(run_id)
            if snapshot is None:
                raise RuntimeError("idempotent create result has no run projection")
            return record_from_snapshot(snapshot, control_kernel=self), True

        from .engine import RunRecord

        record = self.bind(RunRecord(id=run_id, request=request, retry_of=retry_of))
        record.runtime_revision = 1
        projection = record_to_projection(record, revision=1)
        event = EventEnvelope.create(
            run_id,
            "run.created",
            "control_kernel.create_run",
            {"projection": projection},
        )
        result = self.store.commit(
            command,
            [event],
            idempotency_key=idempotency_key,
            expected_revision=0,
            projection_updates=[ProjectionUpdate("run", run_id, projection)],
            result={"run_id": run_id},
        )
        record.runtime_revision = result.revision
        return record, result.replayed

    def set_run_status(self, record: Any, target: RunStatus, *, source: str) -> None:
        with record.lock:
            current = record.status
            decision = validate_transition(current, target)
            if not decision.changed:
                return
            record.status = target
            try:
                self._persist(
                    record,
                    kind=CommandKind.TRANSITION_RUN,
                    event_kind="run.state.changed",
                    source=source,
                    payload={"current": decision.current, "target": decision.target},
                    idempotency_key=(
                        f"internal:{record.id}:run:{source}:{record.runtime_revision + 1}"
                    ),
                )
            except Exception:
                record.status = current
                raise

    def set_task_status(
        self,
        record: Any,
        task: Any,
        target: TaskStatus,
        *,
        source: str,
    ) -> None:
        with record.lock:
            current = task.status
            decision = validate_transition(current, target)
            if not decision.changed:
                return
            task.status = target
            try:
                self._persist(
                    record,
                    kind=CommandKind.TRANSITION_TASK,
                    event_kind="task.state.changed",
                    source=source,
                    task_id=str(task.id),
                    payload={"current": decision.current, "target": decision.target},
                    idempotency_key=(
                        f"internal:{record.id}:task:{task.id}:{source}:{record.runtime_revision + 1}"
                    ),
                )
            except Exception:
                task.status = current
                raise

    def record_runtime_event(self, record: Any, event: Event, *, source: str) -> None:
        self._persist(
            record,
            kind=CommandKind.RECORD_EVENT,
            event_kind=f"runtime.{event.kind}",
            source=source,
            task_id=event.task_id,
            payload={
                "message": event.message,
                "detail": event.detail,
                "legacy_event_at": event.at,
            },
            idempotency_key=f"internal:{record.id}:event:{record.runtime_revision + 1}:{str(new_command_id())[-8:]}",
        )

    def checkpoint(self, record: Any, *, source: str) -> None:
        self._persist(
            record,
            kind=CommandKind.CHECKPOINT,
            event_kind="runtime.checkpointed",
            source=source,
            payload={},
            idempotency_key=f"internal:{record.id}:checkpoint:{record.runtime_revision + 1}",
        )

    def cancel_run(
        self,
        record: Any,
        *,
        idempotency_key: str,
        expected_revision: int,
    ) -> Any:
        with record.lock:
            command = CommandEnvelope.create(
                str(record.id),
                CommandKind.CANCEL_RUN,
                {"requested": True},
            )
            replay = self.store.replay_command(
                command, idempotency_key=idempotency_key
            )
            if replay is not None:
                return record
            if int(expected_revision) != record.runtime_revision:
                from .event_store import RevisionConflictError

                raise RevisionConflictError(expected_revision, record.runtime_revision)
            if is_terminal(record.status):
                raise ValueError(f"terminal run cannot be cancelled: {record.status.value}")
            old_status = record.status
            record.cancel_requested = True
            validate_transition(record.status, RunStatus.CANCELLING)
            record.status = RunStatus.CANCELLING
            next_revision = record.runtime_revision + 1
            projection = record_to_projection(record, revision=next_revision)
            event = EventEnvelope.create(
                str(record.id),
                "run.cancel.requested",
                "control_kernel.cancel_run",
                {"projection": projection},
            )
            try:
                committed = self.store.commit(
                    command,
                    [event],
                    idempotency_key=idempotency_key,
                    expected_revision=expected_revision,
                    projection_updates=[ProjectionUpdate("run", str(record.id), projection)],
                    result={"run_id": str(record.id), "status": "cancelling"},
                )
                record.runtime_revision = committed.revision
            except Exception:
                record.status = old_status
                record.cancel_requested = False
                raise
            self.set_run_status(
                record,
                RunStatus.CANCELLED,
                source="control_kernel.cancel_finalized",
            )
            return record

    def load_records(self) -> list[Any]:
        return [
            record_from_snapshot(snapshot, control_kernel=self)
            for snapshot in self.store.list_projections(limit=1000)
        ]

    def events(
        self,
        run_id: str,
        *,
        after_revision: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self.store.list_events(
            run_id, after_revision=after_revision, limit=limit
        )

    def health(self) -> dict[str, Any]:
        from .compat_v4 import (
            artifact_dag_enabled,
            evidence_recovery_enabled,
            handoff_intelligence_enabled,
            outcome_forecast_enabled,
            media_workflow_enabled,
            parallel_runtime_enabled,
            placement_v4_enabled,
        )

        return {
            **self.store.health(),
            **self.artifact_store.counts(),
            **self.reservations.counts(),
            **self.placement_store.counts(),
            **self.evidence_store.counts(),
            **self.outcome_store.counts(),
            **self.media_runtime.counts(),
            "artifact_dag": artifact_dag_enabled(),
            "parallel_runtime": parallel_runtime_enabled(),
            "placement_v4": placement_v4_enabled(),
            "evidence_recovery": evidence_recovery_enabled(),
            "outcome_forecast": outcome_forecast_enabled(),
            "media_workflow": media_workflow_enabled(),
            "handoff_intelligence": handoff_intelligence_enabled(),
            "recovered_orphan_leases": self.recovered_orphan_leases,
            "persistent": True,
        }

    def compile_graph(self, record: Any) -> dict[str, Any]:
        if record.plan is None:
            raise ValueError("cannot compile a run without a plan")
        graph = self.graph_compiler.compile(str(record.id), record.plan)
        self.artifact_store.sync_graph(graph)
        return graph.to_dict()

    def prepare_handoff(self, record: Any, task: Any) -> list[dict[str, Any]]:
        if record.plan is None:
            raise ValueError("cannot hand off a task without a plan")
        inputs = self.artifact_store.task_artifacts(
            str(record.id),
            task.dependencies,
            validated_only=True,
        )
        from .compat_v4 import handoff_intelligence_enabled

        if not handoff_intelligence_enabled():
            found_producers = {
                artifact.producer_task_id for artifact in inputs
            }
            required = {
                canonical_identifier(
                    "task", f"{record.id}:{dependency}"
                )
                for dependency in task.dependencies
                if task.dependency_types.get(dependency, "hard")
                != "soft"
            }
            missing = required - found_producers
            if missing:
                raise ValueError(
                    "handoff is missing validated producer artifacts: "
                    f"{sorted(missing)}"
                )
        task.input_artifact_refs = [artifact.artifact_id for artifact in inputs]
        compiled = None
        if handoff_intelligence_enabled():
            worker_data = next(
                (
                    item
                    for item in record.worker_snapshot
                    if str(item.get("id")) == str(task.selected_worker)
                ),
                None,
            )
            worker = (
                WorkerProfile(**worker_data)
                if worker_data is not None
                else WorkerProfile(
                    id=str(task.selected_worker or "compat-receiver"),
                    name="Compatibility receiver",
                    specialty=task.task_type,
                    capabilities={
                        item: 1.0
                        for item in task.required_capabilities
                    },
                    cost_per_task=max(
                        0.0, float(task.estimated_cost)
                    ),
                    latency_factor=1.0,
                    reliability=max(
                        0.5, float(task.predicted_success or 0.8)
                    ),
                    context_window=max(
                        32_000,
                        int(task.estimated_input_tokens),
                    ),
                    max_output_tokens=max(
                        8_000,
                        int(task.estimated_output_tokens),
                    ),
                    modalities=list(task.required_modalities),
                    tools=list(task.requires_tools),
                )
            )
            approval = (
                self.approval_store.get(task.approval_id)
                if task.approval_id
                else None
            )
            assumptions = (
                record.plan.analysis.get("assumptions", [])
                if isinstance(record.plan.analysis, dict)
                else []
            )
            try:
                compiled = self.handoff_compiler.compile(
                    run_id=str(record.id),
                    goal=record.request.goal,
                    plan=record.plan,
                    task=task,
                    artifacts=inputs,
                    worker=worker,
                    approval=approval,
                    assumptions=assumptions,
                    remaining_budget=max(
                        0.0,
                        record.request.budget - record.spent,
                    ),
                )
            except HandoffPreflightError as exc:
                receipt = exc.receipt.to_dict()
                task.handoff_receipt = receipt
                entry = {
                    "message_id": None,
                    "task_id": str(task.id),
                    "attempt": int(task.attempt),
                    "plan_revision": int(record.plan.version),
                    "admitted": False,
                    "preflight": receipt,
                }
                if not any(
                    item.get("task_id") == task.id
                    and item.get("attempt") == task.attempt
                    for item in record.handoff_history
                ):
                    record.handoff_history.append(entry)
                self._persist(
                    record,
                    kind=CommandKind.RECORD_HANDOFF,
                    event_kind="message.handoff.rejected",
                    source="control_kernel.prepare_handoff",
                    task_id=str(task.id),
                    payload=entry,
                    idempotency_key=(
                        f"internal:{record.id}:handoff-rejected:"
                        f"{task.id}:{task.attempt}"
                    ),
                )
                raise
        message = MessageEnvelope.create(
            run_id=str(record.id),
            recipient_task_id=canonical_task_id(str(record.id), str(task.id)),
            plan_revision=record.plan.version,
            attempt=task.attempt,
            goal=record.request.goal,
            objective=task.objective,
            input_artifact_refs=task.input_artifact_refs,
            output_artifact_specs=task.output_artifact_specs,
            acceptance_criteria=task.acceptance_criteria,
            include=task.include,
            exclude=task.exclude,
            context_manifest=(
                [
                    dict(item)
                    for item in compiled.context_manifest
                ]
                if compiled is not None
                else None
            ),
            work_order=(
                compiled.work_order.to_dict()
                if compiled is not None
                else None
            ),
            preflight=(
                compiled.preflight.to_dict()
                if compiled is not None
                else None
            ),
            contract_digest=(
                compiled.contract_digest
                if compiled is not None
                else None
            ),
            compiled_prompt=(
                compiled.compiled_prompt
                if compiled is not None
                else task.handoff_prompt
            ),
            compiler_revision=(
                compiled.compiler_revision
                if compiled is not None
                else "legacy-envelope"
            ),
        )
        stored, replayed = self.artifact_store.put_message(message)
        task.handoff_message_id = stored.message_id
        task.handoff_prompt = stored.compiled_prompt or task.handoff_prompt
        task.handoff_receipt = dict(stored.preflight)
        history_entry = {
            "message_id": stored.message_id,
            "task_id": str(task.id),
            "attempt": int(task.attempt),
            "plan_revision": int(stored.plan_revision),
            "admitted": bool(stored.preflight.get("admitted", True)),
            "contract_digest": stored.contract_digest,
            "compiler_revision": stored.compiler_revision,
            "preflight": dict(stored.preflight),
        }
        if not any(
            item.get("message_id") == stored.message_id
            for item in record.handoff_history
        ):
            record.handoff_history.append(history_entry)
        if not replayed:
            self._persist(
                record,
                kind=CommandKind.RECORD_HANDOFF,
                event_kind="message.handoff.recorded",
                source="control_kernel.prepare_handoff",
                task_id=str(task.id),
                payload={
                    "message_id": stored.message_id,
                    "input_artifact_refs": list(stored.input_artifact_refs),
                    "plan_revision": stored.plan_revision,
                    "admitted": history_entry["admitted"],
                    "contract_digest": stored.contract_digest,
                    "compiler_revision": stored.compiler_revision,
                    "preflight": dict(stored.preflight),
                },
                idempotency_key=(
                    f"internal:{record.id}:handoff:{stored.message_id}"
                ),
            )
        return [dict(artifact.content) for artifact in inputs]

    def publish_task_artifacts(
        self,
        record: Any,
        task: Any,
        result: dict[str, Any],
        *,
        validated: bool,
    ) -> list[Artifact]:
        if record.plan is None:
            raise ValueError("cannot publish an artifact without a plan")
        status = (
            ArtifactStatus.VALIDATED if validated else ArtifactStatus.REJECTED
        )
        published: list[Artifact] = []
        for spec in task.output_artifact_specs:
            artifact = Artifact.create(
                run_id=str(record.id),
                producer_task_id=canonical_task_id(
                    str(record.id), str(task.id)
                ),
                artifact_type=str(spec["artifact_type"]),
                schema_ref=str(spec["schema_ref"]),
                content=result,
                plan_revision=record.plan.version,
                status=status,
                producer_model=task.selected_worker,
                input_artifact_refs=task.input_artifact_refs,
                attempt=task.attempt,
                sensitivity=str(spec.get("sensitivity", "internal")),
            )
            artifact, _ = self.artifact_store.put(artifact)
            if artifact.artifact_id not in task.artifact_ids:
                task.artifact_ids.append(artifact.artifact_id)
            published.append(artifact)
        self._persist(
            record,
            kind=CommandKind.PUBLISH_ARTIFACT,
            event_kind="artifact.published",
            source="control_kernel.publish_task_artifacts",
            task_id=str(task.id),
            payload={
                "artifact_ids": [artifact.artifact_id for artifact in published],
                "status": status.value,
                "plan_revision": record.plan.version,
            },
            idempotency_key=(
                f"internal:{record.id}:artifacts:{task.id}:"
                f"{task.attempt}:{status.value}"
            ),
        )
        return published

    def invalidate_tasks(
        self,
        record: Any,
        task_ids: set[str] | list[str],
        *,
        reason: str,
    ) -> list[str]:
        invalidated = self.artifact_store.mark_stale_for_tasks(
            str(record.id), task_ids
        )
        if invalidated:
            self._persist(
                record,
                kind=CommandKind.INVALIDATE_ARTIFACTS,
                event_kind="artifact.invalidated",
                source="control_kernel.invalidate_tasks",
                payload={
                    "artifact_ids": invalidated,
                    "affected_tasks": sorted(task_ids),
                    "reason": reason,
                },
                idempotency_key=(
                    f"internal:{record.id}:invalidate:"
                    f"{record.runtime_revision + 1}"
                ),
            )
        return invalidated

    def artifacts(self, run_id: str, *, include_content: bool = False) -> list[dict[str, Any]]:
        return self.artifact_store.list_run(
            run_id, include_content=include_content
        )

    def handoffs(self, run_id: str) -> list[dict[str, Any]]:
        return self.artifact_store.list_messages(run_id)

    def record_completion_checkpoint(
        self,
        record: Any,
        checkpoint: Any,
    ) -> None:
        payload = checkpoint.to_dict()
        if not any(
            item.get("checkpoint_id") == payload["checkpoint_id"]
            for item in record.completion_checkpoints
        ):
            record.completion_checkpoints.append(payload)
        self._persist(
            record,
            kind=CommandKind.CHECKPOINT,
            event_kind="runtime.completion_checkpointed",
            source="control_kernel.record_completion_checkpoint",
            payload=payload,
            idempotency_key=(
                f"internal:{record.id}:pcomplete:"
                f"{payload['checkpoint_id']}"
            ),
        )

    def record_pollution_report(
        self,
        record: Any,
        report: Any,
    ) -> None:
        payload = report.to_dict()
        record.pollution_reports.append(payload)
        self._persist(
            record,
            kind=CommandKind.CHECKPOINT,
            event_kind="runtime.pollution_analyzed",
            source="control_kernel.record_pollution_report",
            payload=payload,
            idempotency_key=(
                f"internal:{record.id}:pollution:"
                f"{len(record.pollution_reports)}:"
                f"{payload['failed_task_id']}"
            ),
        )

    def artifact(self, artifact_id: str) -> dict[str, Any] | None:
        artifact = self.artifact_store.get(artifact_id)
        return (
            artifact.to_dict(include_content=False)
            if artifact is not None
            else None
        )

    def task(self, task_id: str) -> dict[str, Any] | None:
        return self.store.get_task(task_id)

    def graph(self, record: Any) -> dict[str, Any]:
        if record.plan is None:
            return {
                "run_id": str(record.id),
                "plan_revision": None,
                "tasks": [],
                "artifact_links": [],
            }
        return self.artifact_store.graph(str(record.id), record.plan)

    def resources(self) -> list[dict[str, Any]]:
        return self.reservations.resources()

    def approvals(self, *, status: str | None = None) -> list[dict[str, Any]]:
        return self.approval_store.list(status=status)

    def scheduling_summary(self, run_id: str) -> dict[str, Any]:
        approvals = self.approval_store.list(run_id=run_id)
        return {
            **self.reservations.summary(run_id),
            **self.placement_store.summary(run_id),
            **self.evidence_store.summary(run_id),
            "pending_approvals": sum(
                item["status"] == "pending" for item in approvals
            ),
            "resolved_approvals": sum(
                item["status"] in {"approved", "rejected"}
                for item in approvals
            ),
        }

    def record_forecast(self, forecast: Any) -> None:
        self.outcome_store.record_forecast(forecast)

    def calibrated_probability(self, task: Any) -> dict[str, Any]:
        candidates = self.outcome_store.capabilities.candidates(
            model_id=task.selected_worker or "unassigned",
            task_type=task.task_type,
            risk_level=task.risk_level,
        )
        return self.calibrator.calibrate(
            task.predicted_success,
            candidates,
        )

    def forecast(self, run_id: str) -> dict[str, Any] | None:
        return self.outcome_store.forecast(run_id)

    def record_outcome(self, record: Any) -> dict[str, Any]:
        return self.outcome_store.record_outcome(record).to_dict()

    def outcomes(self) -> list[dict[str, Any]]:
        return self.outcome_store.outcomes()

    def evaluations(self) -> list[dict[str, Any]]:
        return self.outcome_store.evaluations()

    def ensure_media_project(self, record: Any) -> dict[str, Any] | None:
        return self.media_runtime.ensure_project(
            run_id=str(record.id),
            goal=record.request.goal,
        )

    def media_project(self, run_id: str) -> dict[str, Any] | None:
        return self.media_runtime.project(run_id)

    def media_shots(self, run_id: str) -> list[dict[str, Any]]:
        return self.media_runtime.shots(run_id)

    def approve_media_shot(
        self, shot_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self.media_runtime.approve_shot(
            shot_id, idempotency_key=idempotency_key
        )

    def approve_media_project(
        self, run_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self.media_runtime.approve_project(
            run_id, idempotency_key=idempotency_key
        )

    def sync_placements(self, record: Any) -> int:
        if record.plan is None:
            return 0
        return self.placement_store.sync_run(
            str(record.id), record.plan.tasks
        )

    def record_actual_cost(self, record: Any, task: Any) -> None:
        self.placement_store.record_actual(
            run_id=str(record.id),
            task=task,
            actual_total=task.estimated_cost,
        )

    def placements(self, run_id: str) -> list[dict[str, Any]]:
        return self.placement_store.list_run(run_id)

    def ledger(
        self, *, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        return self.placement_store.ledger(run_id=run_id)

    def record_validation(self, record: Any, task: Any) -> str:
        return self.evidence_store.record_validation(
            run_id=str(record.id),
            task=task,
            validation=task.validation,
        )

    def record_failure(
        self,
        record: Any,
        task: Any,
        failure: Any,
        decision: Any,
        affected_task_ids: list[str],
        patch: Any = None,
        recovery_receipt: dict[str, Any] | None = None,
    ) -> None:
        self.evidence_store.record_failure(
            run_id=str(record.id),
            task=task,
            failure=failure,
            decision=decision,
            affected_task_ids=affected_task_ids,
            patch=patch,
            recovery_receipt=recovery_receipt,
        )

    def validations(self, run_id: str) -> list[dict[str, Any]]:
        return self.evidence_store.validations(run_id)

    def failures(self, run_id: str) -> list[dict[str, Any]]:
        return self.evidence_store.failures(run_id)

    def resolve_approval(
        self,
        record: Any,
        approval_id: str,
        *,
        decision: str,
        note: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        approval, replayed = self.approval_store.resolve(
            approval_id,
            decision=decision,
            note=note,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        if not replayed:
            self._persist(
                record,
                kind=CommandKind.RECORD_EVENT,
                event_kind="approval.resolved",
                source="control_kernel.resolve_approval",
                payload={
                    "approval_id": approval_id,
                    "decision": decision,
                    "approval_revision": approval["revision"],
                },
                idempotency_key=(
                    f"internal:{record.id}:approval:{approval_id}:"
                    f"{approval['revision']}"
                ),
            )
        return approval

    def _persist(
        self,
        record: Any,
        *,
        kind: CommandKind,
        event_kind: str,
        source: str,
        payload: dict[str, Any],
        idempotency_key: str,
        task_id: str | None = None,
    ) -> None:
        with self._lock, record.lock:
            previous_revision = int(record.runtime_revision)
            next_revision = previous_revision + 1
            record.runtime_revision = next_revision
            projection = record_to_projection(record, revision=next_revision)
            command = CommandEnvelope.create(
                str(record.id),
                kind,
                {**payload, "source": source},
                task_id=(
                    canonical_task_id(str(record.id), task_id)
                    if task_id is not None
                    else None
                ),
            )
            event = EventEnvelope.create(
                str(record.id),
                event_kind,
                source,
                {**payload, "projection": projection},
                task_id=command.task_id,
            )
            try:
                committed = self.store.commit(
                    command,
                    [event],
                    idempotency_key=idempotency_key,
                    expected_revision=previous_revision,
                    projection_updates=[
                        ProjectionUpdate("run", str(record.id), projection)
                    ],
                    result={"run_id": str(record.id), "event_kind": event_kind},
                )
                record.runtime_revision = committed.revision
            except Exception:
                record.runtime_revision = previous_revision
                raise
