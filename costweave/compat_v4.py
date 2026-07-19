"""Compatibility layer between the v0.3 runtime and v0.4.1 contracts."""

from __future__ import annotations

import copy
import os
import threading
from collections import deque
from typing import Any, Mapping

from .contracts_v4 import (
    CONTRACT_SCHEMA_VERSION,
    EventEnvelope,
    canonical_identifier,
)
from .domain import RunStatus, TaskStatus
from .state_guard import StateTransitionError, validate_transition


FEATURE_FLAG = "COSTWEAVE_CONTRACTS_V4"
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})
_TRACE_LIMIT = 2048
_TRACE: deque[EventEnvelope] = deque(maxlen=_TRACE_LIMIT)
_TRACE_LOCK = threading.RLock()


def contracts_v4_enabled() -> bool:
    raw = os.environ.get(FEATURE_FLAG, "1")
    return raw.strip().lower() not in _FALSE_VALUES


def contract_health() -> dict[str, Any]:
    return {
        "contract_schema": CONTRACT_SCHEMA_VERSION,
        "features": {"contracts_v4": contracts_v4_enabled()},
        "contract_trace_persistent": False,
    }


def set_run_status(record: Any, target: RunStatus, *, source: str) -> None:
    _set_status(
        owner=record,
        record=record,
        target=target,
        source=source,
        task_id=None,
    )


def set_task_status(
    record: Any,
    task: Any,
    target: TaskStatus,
    *,
    source: str,
) -> None:
    _set_status(
        owner=task,
        record=record,
        target=target,
        source=source,
        task_id=str(task.id),
    )


def trace_snapshot(run_id: str | None = None) -> tuple[dict[str, Any], ...]:
    canonical_run_id = (
        canonical_identifier("run", run_id) if run_id is not None else None
    )
    with _TRACE_LOCK:
        entries = tuple(_TRACE)
    return tuple(
        entry.to_dict()
        for entry in entries
        if canonical_run_id is None or entry.run_id == canonical_run_id
    )


def clear_trace() -> None:
    """Clear the ephemeral trace; intended for tests and local diagnostics."""

    with _TRACE_LOCK:
        _TRACE.clear()


def v3_snapshot_to_v4(
    snapshot: Mapping[str, Any],
    *,
    include_trace: bool = True,
) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise TypeError("v0.3 snapshot must be an object")
    payload = copy.deepcopy(dict(snapshot))
    if "id" not in payload or "status" not in payload:
        raise ValueError("v0.3 snapshot requires id and status")
    RunStatus(payload["status"])
    legacy_id = str(payload["id"])
    run_id = canonical_identifier("run", legacy_id)
    contract = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "run_id": run_id,
        "legacy_run_id": legacy_id,
        "trace_persistent": False,
    }
    if include_trace:
        contract["trace"] = list(trace_snapshot(legacy_id))
    payload["contract"] = contract
    return payload


def v4_snapshot_to_v3(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise TypeError("v4 snapshot must be an object")
    payload = copy.deepcopy(dict(snapshot))
    contract = payload.pop("contract", None)
    if not isinstance(contract, Mapping):
        raise ValueError("v4 snapshot requires contract metadata")
    if contract.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise ValueError(
            f"contract schema must be {CONTRACT_SCHEMA_VERSION}"
        )
    return payload


def _set_status(
    *,
    owner: Any,
    record: Any,
    target: RunStatus | TaskStatus,
    source: str,
    task_id: str | None,
) -> None:
    if not contracts_v4_enabled():
        owner.status = target
        return

    current = owner.status
    try:
        decision = validate_transition(current, target)
    except StateTransitionError as exc:
        _append_trace(
            record=record,
            source=source,
            task_id=task_id,
            current=str(current),
            target=str(target),
            accepted=False,
            changed=False,
            reason=str(exc),
        )
        raise

    owner.status = target
    _append_trace(
        record=record,
        source=source,
        task_id=task_id,
        current=decision.current,
        target=decision.target,
        accepted=True,
        changed=decision.changed,
        reason=None,
    )


def _append_trace(
    *,
    record: Any,
    source: str,
    task_id: str | None,
    current: str,
    target: str,
    accepted: bool,
    changed: bool,
    reason: str | None,
) -> None:
    legacy_run_id = str(record.id)
    payload = {
        "current": current,
        "target": target,
        "accepted": accepted,
        "changed": changed,
        "reason": reason,
        "legacy_run_id": legacy_run_id,
    }
    event = EventEnvelope.create(
        run_id=canonical_identifier("run", legacy_run_id),
        task_id=(
            canonical_identifier("task", task_id)
            if task_id is not None
            else None
        ),
        kind=(
            "state.transition.accepted"
            if accepted
            else "state.transition.rejected"
        ),
        source=source,
        payload=payload,
    )
    with _TRACE_LOCK:
        _TRACE.append(event)
