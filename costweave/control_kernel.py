"""Persistent command/event/projection owner for CostWeave v0.4.2."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from .commands import CommandKind
from .contracts_v4 import CommandEnvelope, EventEnvelope, canonical_identifier, new_command_id
from .domain import Event, RunRequest, RunStatus, TaskStatus
from .event_store import EventStore, ProjectionUpdate
from .projections import canonical_task_id, record_from_snapshot, record_to_projection
from .state_guard import is_terminal, validate_transition


RUNTIME_DB_ENV = "COSTWEAVE_RUNTIME_DB"


class ControlKernel:
    """The only persistent state writer when COSTWEAVE_RUNTIME_V4 is enabled."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.store = EventStore(path or self.default_path())
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
        return {**self.store.health(), "persistent": True}

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
