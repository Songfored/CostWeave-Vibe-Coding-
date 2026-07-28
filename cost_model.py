"""Auditable forecast and actual cost components for v0.4.5."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping


COST_CATEGORIES = (
    "provider_inference",
    "local_compute",
    "energy",
    "scarce_capacity",
    "queue_opportunity",
    "handoff",
    "validation_merge",
    "expected_rework",
)


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    provider_inference: float = 0.0
    local_compute: float = 0.0
    energy: float = 0.0
    scarce_capacity: float = 0.0
    queue_opportunity: float = 0.0
    handoff: float = 0.0
    validation_merge: float = 0.0
    expected_rework: float = 0.0

    def __post_init__(self) -> None:
        for name, amount in asdict(self).items():
            if float(amount) < 0:
                raise ValueError(f"cost component {name} cannot be negative")

    @property
    def total(self) -> float:
        return sum(float(value) for value in asdict(self).values())

    def to_dict(self) -> dict[str, float]:
        result = {
            key: round(float(value), 8)
            for key, value in asdict(self).items()
        }
        result["total"] = round(self.total, 8)
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, float]) -> "CostBreakdown":
        return cls(
            **{
                category: max(0.0, float(value.get(category, 0.0)))
                for category in COST_CATEGORIES
            }
        )


@dataclass(frozen=True, slots=True)
class CostForecast:
    p50: CostBreakdown
    p95_total: float
    reservation_ceiling: float

    def __post_init__(self) -> None:
        if self.p95_total + 1e-12 < self.p50.total:
            raise ValueError("p95 cost must not be below p50")
        if self.reservation_ceiling + 1e-12 < self.p95_total:
            raise ValueError("reservation ceiling must cover p95 cost")

    def to_dict(self) -> dict:
        return {
            "p50": self.p50.to_dict(),
            "p95_total": round(self.p95_total, 8),
            "reservation_ceiling": round(
                self.reservation_ceiling, 8
            ),
        }


def ledger_conserves(
    entries: list[Mapping[str, object]],
    *,
    phase: str,
    expected_total: float,
    tolerance: float = 1e-7,
) -> bool:
    attributed = {
        str(item["attribution_key"])
        for item in entries
        if item.get("phase") == phase
    }
    relevant = [
        item for item in entries if item.get("phase") == phase
    ]
    return (
        len(attributed) == len(relevant)
        and abs(
            sum(float(item["amount"]) for item in relevant)
            - float(expected_total)
        )
        <= tolerance
    )
