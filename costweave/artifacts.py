"""Immutable, content-addressed artifacts for CostWeave v0.4.3."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping

from .contracts_v4 import canonical_identifier, utc_now, validate_identifier


ARTIFACT_SCHEMA_VERSION = "v4.3"


class ArtifactError(ValueError):
    """Base error for malformed or unsafe artifacts."""


class ArtifactIntegrityError(ArtifactError):
    """Raised when stored content does not match its immutable hash."""


class StaleArtifactError(ArtifactError):
    """Raised when an invalidated artifact is used as an input."""


class ArtifactStatus(StrEnum):
    VALIDATED = "validated"
    REJECTED = "rejected"
    STALE = "stale"


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ArtifactError("artifact content must contain finite JSON values") from exc


def content_hash(content: Any) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    run_id: str
    producer_task_id: str
    artifact_type: str
    schema_ref: str
    content: Mapping[str, Any]
    content_hash: str
    plan_revision: int
    status: ArtifactStatus
    producer_model: str | None
    input_artifact_refs: tuple[str, ...]
    attempt: int
    created_at: str
    sensitivity: str = "internal"
    schema_version: str = ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_identifier(self.artifact_id, "artifact")
        validate_identifier(self.run_id, "run")
        validate_identifier(self.producer_task_id, "task")
        for artifact_id in self.input_artifact_refs:
            validate_identifier(artifact_id, "artifact")
        if self.plan_revision < 1:
            raise ArtifactError("plan_revision must be positive")
        if self.attempt < 1:
            raise ArtifactError("attempt must be positive")
        if not self.artifact_type.strip() or not self.schema_ref.strip():
            raise ArtifactError("artifact_type and schema_ref are required")
        if self.sensitivity not in {"public", "internal", "sensitive"}:
            raise ArtifactError("unsupported artifact sensitivity")
        if self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ArtifactError("unsupported artifact schema version")
        frozen_content = copy.deepcopy(dict(self.content))
        canonical_json(frozen_content)
        object.__setattr__(self, "content", frozen_content)
        self.verify_integrity()

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        producer_task_id: str,
        artifact_type: str,
        schema_ref: str,
        content: Mapping[str, Any],
        plan_revision: int,
        status: ArtifactStatus,
        producer_model: str | None,
        input_artifact_refs: list[str] | tuple[str, ...],
        attempt: int,
        sensitivity: str = "internal",
    ) -> "Artifact":
        digest = content_hash(content)
        artifact_id = canonical_identifier(
            "artifact",
            f"{run_id}:{producer_task_id}:{attempt}:"
            f"{artifact_type}:{schema_ref}:{digest}",
        )
        return cls(
            artifact_id=artifact_id,
            run_id=run_id,
            producer_task_id=producer_task_id,
            artifact_type=artifact_type,
            schema_ref=schema_ref,
            content=content,
            content_hash=digest,
            plan_revision=int(plan_revision),
            status=status,
            producer_model=producer_model,
            input_artifact_refs=tuple(input_artifact_refs),
            attempt=int(attempt),
            created_at=utc_now(),
            sensitivity=sensitivity,
        )

    def verify_integrity(self) -> None:
        actual = content_hash(self.content)
        if actual != self.content_hash:
            raise ArtifactIntegrityError(
                f"artifact {self.artifact_id} content hash mismatch"
            )

    def with_status(self, status: ArtifactStatus) -> "Artifact":
        return replace(self, status=status)

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "producer_task_id": self.producer_task_id,
            "artifact_type": self.artifact_type,
            "schema_ref": self.schema_ref,
            "content_hash": self.content_hash,
            "plan_revision": self.plan_revision,
            "status": self.status.value,
            "producer_model": self.producer_model,
            "input_artifact_refs": list(self.input_artifact_refs),
            "attempt": self.attempt,
            "created_at": self.created_at,
            "sensitivity": self.sensitivity,
            "content_included": bool(include_content),
            "access_policy": (
                "internal-runtime"
                if include_content
                else "metadata-only"
            ),
        }
        if include_content:
            payload["content"] = copy.deepcopy(dict(self.content))
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Artifact":
        return cls(
            artifact_id=str(data["artifact_id"]),
            run_id=str(data["run_id"]),
            producer_task_id=str(data["producer_task_id"]),
            artifact_type=str(data["artifact_type"]),
            schema_ref=str(data["schema_ref"]),
            content=dict(data["content"]),
            content_hash=str(data["content_hash"]),
            plan_revision=int(data["plan_revision"]),
            status=ArtifactStatus(data["status"]),
            producer_model=(
                str(data["producer_model"])
                if data.get("producer_model") is not None
                else None
            ),
            input_artifact_refs=tuple(data.get("input_artifact_refs") or ()),
            attempt=int(data["attempt"]),
            created_at=str(data["created_at"]),
            sensitivity=str(data.get("sensitivity", "internal")),
            schema_version=str(data.get("schema_version", ARTIFACT_SCHEMA_VERSION)),
        )
