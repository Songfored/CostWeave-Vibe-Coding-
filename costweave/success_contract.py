"""Versioned, criterion-level definitions of task success."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

from .domain import TaskContract


SUCCESS_CONTRACT_REVISION = "success-contract-v1"


@dataclass(frozen=True, slots=True)
class CriterionContract:
    criterion_id: str
    text: str
    critical: bool
    evidence_required: bool = True
    independent_evidence_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SuccessContract:
    revision: str
    task_id: str
    criteria: tuple[CriterionContract, ...]
    required_result_fields: tuple[str, ...]
    minimum_score: float

    @classmethod
    def from_task(cls, task: TaskContract) -> "SuccessContract":
        critical_text = set(task.critical_criteria)
        if not critical_text and task.acceptance_criteria:
            critical_text.add(task.acceptance_criteria[0])
        criteria = []
        for index, text in enumerate(task.acceptance_criteria):
            digest = hashlib.sha256(
                f"{task.id}:{index}:{text}".encode("utf-8")
            ).hexdigest()[:16]
            critical = (
                text in critical_text
                or task.risk_level == "high"
                or task.task_type in {"risk", "safety"}
            )
            criteria.append(
                CriterionContract(
                    criterion_id=f"criterion_{digest}",
                    text=text,
                    critical=critical,
                    independent_evidence_required=(
                        critical and task.risk_level == "high"
                    ),
                )
            )
        return cls(
            revision=SUCCESS_CONTRACT_REVISION,
            task_id=task.id,
            criteria=tuple(criteria),
            required_result_fields=tuple(
                dict.fromkeys(
                    ["summary", "evidence", "confidence", *task.output_schema]
                )
            ),
            minimum_score=round(
                min(
                    0.95,
                    0.62
                    + task.criticality * 0.14
                    + (0.08 if task.risk_level == "high" else 0),
                ),
                3,
            ),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SuccessContract":
        return cls(
            revision=str(value["revision"]),
            task_id=str(value["task_id"]),
            criteria=tuple(
                CriterionContract(**item)
                for item in value.get("criteria", [])
            ),
            required_result_fields=tuple(
                value.get("required_result_fields", [])
            ),
            minimum_score=float(value.get("minimum_score", 0.7)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "task_id": self.task_id,
            "criteria": [item.to_dict() for item in self.criteria],
            "required_result_fields": list(self.required_result_fields),
            "minimum_score": self.minimum_score,
        }


def is_self_attestation(reference: object) -> bool:
    normalized = str(reference).strip().lower()
    return normalized.startswith(
        ("self:", "model_claim:", "executor_claim:")
    )
