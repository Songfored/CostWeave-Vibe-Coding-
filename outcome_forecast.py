"""Versioned multi-outcome forecasts for CostWeave v0.4.7."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Mapping


FORECAST_POLICY_REVISION = "outcome-forecast-v1"


class OutcomeCategory(StrEnum):
    FIRST_PASS = "first_pass_success"
    REPAIRED = "success_after_repair"
    UPGRADED = "success_after_upgrade"
    HUMAN = "human_intervention"
    FAILED = "final_failure"


OUTCOME_CATEGORIES = tuple(item.value for item in OutcomeCategory)


@dataclass(frozen=True, slots=True)
class OutcomeForecast:
    forecast_id: str
    run_id: str
    probabilities: dict[str, float]
    cumulative_success: dict[str, float]
    quality: dict[str, float]
    cost: dict[str, float]
    completion_seconds: dict[str, float]
    confidence: float
    sample_size: int
    seed: int
    out_of_distribution: bool
    ood_reasons: tuple[str, ...]
    major_failure_modes: tuple[dict[str, Any], ...]
    critical_false_pass_probability: float
    policy_revision: str = FORECAST_POLICY_REVISION
    calibration_revision: str = "beta-hierarchical-v1"

    def __post_init__(self) -> None:
        keys = set(self.probabilities)
        if keys != set(OUTCOME_CATEGORIES):
            raise ValueError("forecast must contain every exclusive outcome")
        if any(not 0.0 <= value <= 1.0 for value in self.probabilities.values()):
            raise ValueError("outcome probabilities must be in [0,1]")
        if abs(sum(self.probabilities.values()) - 1.0) > 1e-8:
            raise ValueError("exclusive outcome probabilities must sum to 1")
        cumulative = list(self.cumulative_success.values())
        if any(right + 1e-12 < left for left, right in zip(cumulative, cumulative[1:])):
            raise ValueError("cumulative success must be monotonic")
        for interval in (self.quality, self.cost, self.completion_seconds):
            if interval["p95"] + 1e-12 < interval["p50"]:
                raise ValueError("p95 cannot be lower than p50")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def create(cls, *, run_id: str, **values: Any) -> "OutcomeForecast":
        identity = {
            "run_id": run_id,
            "seed": values["seed"],
            "sample_size": values["sample_size"],
            "policy_revision": values.get(
                "policy_revision", FORECAST_POLICY_REVISION
            ),
        }
        forecast_id = "forecast_" + hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        return cls(forecast_id=forecast_id, run_id=run_id, **values)


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    outcome_id: str
    run_id: str
    category: str
    success: bool
    quality: float
    cost: float
    completion_seconds: float
    critical_false_pass: bool
    attribution: dict[str, Any]
    recorded_at: str

    def __post_init__(self) -> None:
        if self.category not in OUTCOME_CATEGORIES:
            raise ValueError("unknown outcome category")
        if self.success != (self.category != OutcomeCategory.FAILED.value):
            raise ValueError("success flag conflicts with category")
        if not self.attribution_complete(self.attribution):
            raise ValueError("outcome attribution is incomplete")

    @staticmethod
    def attribution_complete(value: Mapping[str, Any]) -> bool:
        required = {
            "model_ids",
            "prompt_version",
            "tool_version",
            "policy_revisions",
            "data_versions",
            "plan_revision",
        }
        return required <= set(value) and all(value[key] not in (None, "", [], {}) for key in required)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

