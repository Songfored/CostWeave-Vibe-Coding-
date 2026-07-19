"""Deterministic run and task transition rules for CostWeave v0.4.1."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from .domain import RunStatus, TaskStatus


class StateKind(StrEnum):
    RUN = "run"
    TASK = "task"


class StateTransitionError(ValueError):
    """Base error for rejected state transitions."""


class UnknownStateError(StateTransitionError):
    """Raised when either side of a transition is not a known state."""


class InvalidTransitionError(StateTransitionError):
    """Raised when a known transition is not permitted."""


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    state_kind: StateKind
    current: str
    target: str
    changed: bool


RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = MappingProxyType(
    {
        RunStatus.CREATED: frozenset({RunStatus.PLANNING, RunStatus.FAILED}),
        RunStatus.PLANNING: frozenset({RunStatus.EXECUTING, RunStatus.FAILED}),
        RunStatus.EXECUTING: frozenset(
            {RunStatus.REPLANNING, RunStatus.COMPLETED, RunStatus.FAILED}
        ),
        RunStatus.REPLANNING: frozenset({RunStatus.EXECUTING, RunStatus.FAILED}),
        RunStatus.COMPLETED: frozenset(),
        RunStatus.FAILED: frozenset(),
    }
)

TASK_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = MappingProxyType(
    {
        TaskStatus.PENDING: frozenset(
            {TaskStatus.RUNNING, TaskStatus.SUSPECT, TaskStatus.INVALIDATED}
        ),
        TaskStatus.RUNNING: frozenset(
            {TaskStatus.VALIDATED, TaskStatus.SUSPECT, TaskStatus.REJECTED}
        ),
        TaskStatus.SUSPECT: frozenset(
            {
                TaskStatus.PENDING,
                TaskStatus.RUNNING,
                TaskStatus.VALIDATED,
                TaskStatus.REJECTED,
                TaskStatus.INVALIDATED,
            }
        ),
        TaskStatus.REJECTED: frozenset(
            {TaskStatus.PENDING, TaskStatus.INVALIDATED}
        ),
        TaskStatus.VALIDATED: frozenset(),
        TaskStatus.INVALIDATED: frozenset(),
    }
)


def validate_transition(
    current: RunStatus | TaskStatus | str,
    target: RunStatus | TaskStatus | str,
) -> TransitionDecision:
    """Return a decision for a legal transition; raise for an illegal one.

    Assigning the same state is accepted as an idempotent no-op. This permits a
    repeated command to be harmless without permitting terminal-state rollback.
    """

    state_kind, current_state, target_state = _coerce_pair(current, target)
    if current_state == target_state:
        return TransitionDecision(
            state_kind=state_kind,
            current=current_state.value,
            target=target_state.value,
            changed=False,
        )

    transitions = RUN_TRANSITIONS if state_kind == StateKind.RUN else TASK_TRANSITIONS
    if target_state not in transitions[current_state]:
        raise InvalidTransitionError(
            f"illegal {state_kind.value} transition: "
            f"{current_state.value} -> {target_state.value}"
        )
    return TransitionDecision(
        state_kind=state_kind,
        current=current_state.value,
        target=target_state.value,
        changed=True,
    )


def is_terminal(state: RunStatus | TaskStatus | str) -> bool:
    state_kind, coerced = _coerce_one(state)
    transitions = RUN_TRANSITIONS if state_kind == StateKind.RUN else TASK_TRANSITIONS
    return not transitions[coerced]


def _coerce_pair(
    current: RunStatus | TaskStatus | str,
    target: RunStatus | TaskStatus | str,
) -> tuple[
    StateKind,
    RunStatus | TaskStatus,
    RunStatus | TaskStatus,
]:
    current_kind, current_state = _coerce_one(current)
    try:
        target_state = (
            RunStatus(target) if current_kind == StateKind.RUN else TaskStatus(target)
        )
    except (TypeError, ValueError) as exc:
        raise UnknownStateError(
            f"unknown {current_kind.value} target state: {target!r}"
        ) from exc
    return current_kind, current_state, target_state


def _coerce_one(
    state: RunStatus | TaskStatus | str,
) -> tuple[StateKind, RunStatus | TaskStatus]:
    if isinstance(state, RunStatus):
        return StateKind.RUN, state
    if isinstance(state, TaskStatus):
        return StateKind.TASK, state
    try:
        return StateKind.RUN, RunStatus(state)
    except (TypeError, ValueError):
        pass
    try:
        return StateKind.TASK, TaskStatus(state)
    except (TypeError, ValueError) as exc:
        raise UnknownStateError(f"unknown state: {state!r}") from exc
