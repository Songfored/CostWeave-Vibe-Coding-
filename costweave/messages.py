"""Versioned, minimal handoff messages between task executors."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .contracts_v4 import canonical_identifier, utc_now, validate_identifier


MESSAGE_SCHEMA_VERSION = "v4.3"


class MessageError(ValueError):
    pass


class StalePlanRevisionError(MessageError):
    pass


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    message_id: str
    run_id: str
    recipient_task_id: str
    plan_revision: int
    goal: str
    objective: str
    input_artifact_refs: tuple[str, ...]
    output_artifact_specs: tuple[Mapping[str, Any], ...]
    acceptance_criteria: tuple[str, ...]
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    issued_at: str
    attempt: int
    sender: str = "control-kernel"
    kind: str = "task.handoff"
    schema_version: str = MESSAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_identifier(self.message_id, "message")
        validate_identifier(self.run_id, "run")
        validate_identifier(self.recipient_task_id, "task")
        for artifact_id in self.input_artifact_refs:
            validate_identifier(artifact_id, "artifact")
        if self.plan_revision < 1 or self.attempt < 1:
            raise MessageError("plan_revision and attempt must be positive")
        if not self.goal.strip() or not self.objective.strip():
            raise MessageError("goal and objective are required")
        if not self.acceptance_criteria or not self.output_artifact_specs:
            raise MessageError(
                "handoff requires acceptance criteria and output artifact specs"
            )

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        recipient_task_id: str,
        plan_revision: int,
        attempt: int,
        goal: str,
        objective: str,
        input_artifact_refs: list[str],
        output_artifact_specs: list[Mapping[str, Any]],
        acceptance_criteria: list[str],
        include: list[str],
        exclude: list[str],
    ) -> "MessageEnvelope":
        message_id = canonical_identifier(
            "message",
            f"{run_id}:{recipient_task_id}:{plan_revision}:{attempt}",
        )
        return cls(
            message_id=message_id,
            run_id=run_id,
            recipient_task_id=recipient_task_id,
            plan_revision=int(plan_revision),
            attempt=int(attempt),
            goal=goal,
            objective=objective,
            input_artifact_refs=tuple(input_artifact_refs),
            output_artifact_specs=tuple(dict(item) for item in output_artifact_specs),
            acceptance_criteria=tuple(acceptance_criteria),
            include=tuple(include),
            exclude=tuple(exclude),
            issued_at=utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "message_id": self.message_id,
            "run_id": self.run_id,
            "recipient_task_id": self.recipient_task_id,
            "plan_revision": self.plan_revision,
            "attempt": self.attempt,
            "kind": self.kind,
            "sender": self.sender,
            "goal": self.goal,
            "objective": self.objective,
            "input_artifact_refs": list(self.input_artifact_refs),
            "output_artifact_specs": [
                dict(item) for item in self.output_artifact_specs
            ],
            "acceptance_criteria": list(self.acceptance_criteria),
            "include": list(self.include),
            "exclude": list(self.exclude),
            "issued_at": self.issued_at,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def fingerprint(self) -> str:
        semantic = self.to_dict()
        semantic.pop("issued_at", None)
        return hashlib.sha256(
            json.dumps(
                semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MessageEnvelope":
        return cls(
            message_id=str(data["message_id"]),
            run_id=str(data["run_id"]),
            recipient_task_id=str(data["recipient_task_id"]),
            plan_revision=int(data["plan_revision"]),
            attempt=int(data["attempt"]),
            goal=str(data["goal"]),
            objective=str(data["objective"]),
            input_artifact_refs=tuple(data.get("input_artifact_refs") or ()),
            output_artifact_specs=tuple(data.get("output_artifact_specs") or ()),
            acceptance_criteria=tuple(data.get("acceptance_criteria") or ()),
            include=tuple(data.get("include") or ()),
            exclude=tuple(data.get("exclude") or ()),
            issued_at=str(data["issued_at"]),
            sender=str(data.get("sender", "control-kernel")),
            kind=str(data.get("kind", "task.handoff")),
            schema_version=str(data.get("schema_version", MESSAGE_SCHEMA_VERSION)),
        )
