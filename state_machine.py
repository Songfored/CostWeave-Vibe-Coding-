"""Public v0.4.2 state-machine facade.

The transition table remains in :mod:`state_guard` so there is one source of
truth for legacy and persistent runtimes.
"""

from .state_guard import (  # noqa: F401
    InvalidTransitionError,
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    StateKind,
    StateTransitionError,
    TransitionDecision,
    UnknownStateError,
    is_terminal,
    validate_transition,
)

__all__ = [
    "InvalidTransitionError",
    "RUN_TRANSITIONS",
    "TASK_TRANSITIONS",
    "StateKind",
    "StateTransitionError",
    "TransitionDecision",
    "UnknownStateError",
    "is_terminal",
    "validate_transition",
]
