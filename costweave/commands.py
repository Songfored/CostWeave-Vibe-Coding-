"""Command helpers for the CostWeave v0.4.3 control kernel."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Mapping

from .contracts_v4 import CommandEnvelope


class CommandKind(StrEnum):
    CREATE_RUN = "run.create"
    CANCEL_RUN = "run.cancel"
    RETRY_RUN = "run.retry"
    TRANSITION_RUN = "run.transition"
    TRANSITION_TASK = "task.transition"
    RECORD_EVENT = "runtime.record-event"
    CHECKPOINT = "runtime.checkpoint"
    RECOVER_RUN = "run.recover"
    PUBLISH_ARTIFACT = "artifact.publish"
    INVALIDATE_ARTIFACTS = "artifact.invalidate"
    RECORD_HANDOFF = "message.record"


_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,191}$")


class IdempotencyKeyError(ValueError):
    """Raised when an idempotency key is unsafe or ambiguous."""


def validate_idempotency_key(value: str) -> str:
    if not isinstance(value, str) or _KEY_PATTERN.fullmatch(value) is None:
        raise IdempotencyKeyError(
            "idempotency key must contain 8-192 letters, digits, dots, underscores, colons or hyphens"
        )
    return value


def command_fingerprint(command: CommandEnvelope) -> str:
    """Hash semantic command content while ignoring generated IDs and timestamps."""

    semantic: Mapping[str, Any] = {
        "schema_version": command.schema_version,
        "run_id": command.run_id,
        "task_id": command.task_id,
        "kind": command.kind,
        "payload": command.to_dict()["payload"],
    }
    raw = json.dumps(
        semantic,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
