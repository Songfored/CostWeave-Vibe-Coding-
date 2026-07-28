"""Deterministic failure taxonomy used by the recovery policy."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from .contracts_v4 import utc_now


FAILURE_CLASSIFIER_REVISION = "failure-classifier-v1"


class FailureClass(StrEnum):
    TRANSIENT = "transient"
    CAPABILITY = "capability_insufficient"
    CONTRACT = "contract_error"
    ROOT_ASSUMPTION = "root_assumption_error"
    EVIDENCE = "evidence_failure"
    POLICY = "policy_or_safety"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FailureRecord:
    failure_id: str
    task_id: str
    attempt: int
    classification: str
    confidence: float
    signals: tuple[str, ...]
    source: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["signals"] = list(self.signals)
        return data


class FailureClassifier:
    def classify(
        self,
        *,
        task: Any,
        validation: dict[str, Any] | None = None,
        exception: Exception | None = None,
        result: dict[str, Any] | None = None,
    ) -> FailureRecord:
        validation = validation or {}
        result = result or {}
        text = " ".join(
            [
                str(exception or ""),
                str(result.get("fatal_error", "")),
                " ".join(str(item) for item in validation.get("findings", [])),
            ]
        ).lower()
        signals: list[str] = []
        category = FailureClass.UNKNOWN
        confidence = 0.58
        source = "validation"

        if exception is not None:
            source = "adapter"
            name = type(exception).__name__.lower()
            if (
                "transient" in name
                or any(
                    token in text
                    for token in (
                        "timeout",
                        "temporar",
                        "rate limit",
                        "connection reset",
                        "限流",
                        "超时",
                        "暂时",
                    )
                )
            ):
                category = FailureClass.TRANSIENT
                confidence = 0.92
                signals.append("adapter-transient-signal")
            else:
                category = FailureClass.CONTRACT
                confidence = 0.7
                signals.append("adapter-contract-boundary")
        elif any(
            token in text
            for token in (
                "核心假设",
                "root assumption",
                "目标冲突",
                "全局重新规划",
            )
        ):
            category = FailureClass.ROOT_ASSUMPTION
            confidence = 0.94
            signals.append("root-assumption-signal")
        elif task.risk_level == "high" and validation.get(
            "critical_failures"
        ):
            category = FailureClass.POLICY
            confidence = 0.9
            signals.append("critical-high-risk-failure")
        elif validation.get("contract_errors") or any(
            token in text
            for token in ("缺少必要字段", "schema", "合同", "criterion")
        ):
            category = FailureClass.CONTRACT
            confidence = 0.88
            signals.append("contract-shape-failure")
        elif validation.get("critical_failures") or validation.get(
            "evidence_gaps"
        ):
            category = FailureClass.EVIDENCE
            confidence = 0.84
            signals.append("evidence-or-critical-gap")
        elif float(validation.get("score", 0.0)) < 0.45:
            category = FailureClass.CAPABILITY
            confidence = 0.76
            signals.append("low-validation-score")
        else:
            signals.append("unclassified-failure")

        raw_id = (
            f"{task.id}:{task.attempt}:{category.value}:"
            f"{'|'.join(signals)}"
        )
        identifier = "failure_" + hashlib.sha256(
            raw_id.encode("utf-8")
        ).hexdigest()[:32]
        return FailureRecord(
            failure_id=identifier,
            task_id=str(task.id),
            attempt=int(task.attempt),
            classification=category.value,
            confidence=confidence,
            signals=tuple(signals),
            source=source,
            created_at=utc_now(),
        )
