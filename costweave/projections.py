"""Pure serialization and hydration for persistent runtime projections."""

from __future__ import annotations

import copy
from dataclasses import fields
from typing import Any, Mapping

from .contracts_v4 import canonical_identifier
from .domain import (
    Event,
    Plan,
    RunMode,
    RunRequest,
    RunStatus,
    TaskContract,
    TaskStatus,
)


PROJECTION_SCHEMA_VERSION = "v4.2"


class ProjectionError(ValueError):
    pass


def canonical_task_id(run_id: str, legacy_task_id: str) -> str:
    canonical_run = canonical_identifier("run", run_id)
    return canonical_identifier("task", f"{canonical_run}:{legacy_task_id}")


def canonical_attempt_id(run_id: str, legacy_task_id: str, ordinal: int) -> str:
    if int(ordinal) < 1:
        raise ProjectionError("attempt ordinal must be positive")
    canonical_run = canonical_identifier("run", run_id)
    return canonical_identifier(
        "attempt", f"{canonical_run}:{legacy_task_id}:{int(ordinal)}"
    )


def record_to_projection(record: Any, *, revision: int | None = None) -> dict[str, Any]:
    """Serialize stable state without dynamic durations or compatibility trace."""

    with record.lock:
        projection = {
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
            "id": str(record.id),
            "request": {
                "goal": record.request.goal,
                "mode": record.request.mode.value,
                "budget": record.request.budget,
                "quality_floor": record.request.quality_floor,
                "max_concurrency": record.request.max_concurrency,
                "simulate_replan": record.request.simulate_replan,
            },
            "status": record.status.value,
            "plan": record.plan.to_dict() if record.plan else None,
            "events": [event.to_dict() for event in record.events],
            "metrics": {
                "success_probability": record.current_success_probability,
                "spent": record.spent,
                "replans": record.replans,
                "peak_parallelism": record.peak_parallelism,
            },
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "error": record.error,
            "runtime": {
                "schema_version": PROJECTION_SCHEMA_VERSION,
                "revision": record.runtime_revision if revision is None else int(revision),
                "persistent": True,
                "recovered": bool(record.recovered),
                "cancel_requested": bool(record.cancel_requested),
                "retry_of": record.retry_of,
            },
            "_runtime_internal": {
                "worker_snapshot": copy.deepcopy(record.worker_snapshot),
            },
        }
    return projection


def _known_values(cls: type, data: Mapping[str, Any]) -> dict[str, Any]:
    names = {item.name for item in fields(cls)}
    return {key: copy.deepcopy(value) for key, value in data.items() if key in names}


def _task_from_dict(data: Mapping[str, Any]) -> TaskContract:
    values = _known_values(TaskContract, data)
    values["status"] = TaskStatus(values.get("status", TaskStatus.PENDING))
    return TaskContract(**values)


def _plan_from_dict(data: Mapping[str, Any] | None) -> Plan | None:
    if data is None:
        return None
    values = _known_values(Plan, data)
    values["tasks"] = [_task_from_dict(item) for item in data.get("tasks", [])]
    return Plan(**values)


def hydrate_run_data(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise ProjectionError("run projection must be an object")
    schema = snapshot.get("projection_schema_version")
    if schema not in {None, PROJECTION_SCHEMA_VERSION}:
        raise ProjectionError(f"unsupported projection schema: {schema}")
    request_data = dict(snapshot.get("request") or {})
    request = RunRequest(
        goal=str(request_data.get("goal", "")),
        mode=RunMode(request_data.get("mode", RunMode.BALANCED)),
        budget=float(request_data.get("budget", 1.0)),
        quality_floor=float(request_data.get("quality_floor", 0.78)),
        max_concurrency=int(request_data.get("max_concurrency", 4)),
        simulate_replan=bool(request_data.get("simulate_replan", False)),
    ).normalized()
    runtime = dict(snapshot.get("runtime") or {})
    metrics = dict(snapshot.get("metrics") or {})
    internal = dict(snapshot.get("_runtime_internal") or {})
    return {
        "id": str(snapshot["id"]),
        "request": request,
        "status": RunStatus(snapshot.get("status", RunStatus.CREATED)),
        "plan": _plan_from_dict(snapshot.get("plan")),
        "events": [Event(**_known_values(Event, item)) for item in snapshot.get("events", [])],
        "created_at": snapshot.get("created_at"),
        "started_at": snapshot.get("started_at"),
        "finished_at": snapshot.get("finished_at"),
        "current_success_probability": float(metrics.get("success_probability", 0.0)),
        "spent": float(metrics.get("spent", 0.0)),
        "replans": int(metrics.get("replans", 0)),
        "peak_parallelism": int(metrics.get("peak_parallelism", 0)),
        "error": snapshot.get("error"),
        "runtime_revision": int(runtime.get("revision", 0)),
        "recovered": bool(runtime.get("recovered", False)),
        "cancel_requested": bool(runtime.get("cancel_requested", False)),
        "retry_of": runtime.get("retry_of"),
        "worker_snapshot": copy.deepcopy(internal.get("worker_snapshot", [])),
    }


def record_from_snapshot(snapshot: Mapping[str, Any], *, control_kernel: Any = None) -> Any:
    from .engine import RunRecord

    values = hydrate_run_data(snapshot)
    if values["created_at"] is None:
        values.pop("created_at")
    record = RunRecord(**values)
    record.control_kernel = control_kernel
    return record
