"""Beta posterior calibration with conservative hierarchical fallback."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


CALIBRATION_REVISION = "beta-hierarchical-v1"


@dataclass(frozen=True, slots=True)
class BetaPosterior:
    successes: float = 1.0
    failures: float = 1.0

    @property
    def samples(self) -> float:
        return max(0.0, self.successes + self.failures - 2.0)

    @property
    def mean(self) -> float:
        return self.successes / (self.successes + self.failures)

    def update(self, success: bool, weight: float = 1.0) -> "BetaPosterior":
        if weight <= 0:
            raise ValueError("weight must be positive")
        return BetaPosterior(
            self.successes + (weight if success else 0.0),
            self.failures + (0.0 if success else weight),
        )

    def interval(self, z: float = 1.96) -> tuple[float, float]:
        total = self.successes + self.failures
        variance = (
            self.successes * self.failures
            / (total * total * (total + 1.0))
        )
        radius = z * math.sqrt(variance)
        return max(0.0, self.mean - radius), min(1.0, self.mean + radius)


class HierarchicalCalibrator:
    def __init__(self, *, minimum_samples: int = 20) -> None:
        self.minimum_samples = int(minimum_samples)

    def choose(
        self,
        candidates: Iterable[tuple[str, BetaPosterior]],
    ) -> tuple[str, BetaPosterior, bool]:
        materialized = list(candidates)
        if not materialized:
            return "prior", BetaPosterior(), True
        for level, posterior in materialized:
            if posterior.samples >= self.minimum_samples:
                return level, posterior, False
        level, posterior = max(
            materialized, key=lambda item: item[1].samples
        )
        return level, posterior, True

    def calibrate(
        self,
        prior_probability: float,
        candidates: Iterable[tuple[str, BetaPosterior]],
    ) -> dict[str, float | str | bool]:
        level, posterior, sparse = self.choose(candidates)
        prior = max(0.001, min(0.999, float(prior_probability)))
        if sparse:
            weight = min(0.5, posterior.samples / max(1, self.minimum_samples) * 0.5)
        else:
            weight = min(0.9, posterior.samples / (posterior.samples + 20.0))
        probability = (1.0 - weight) * prior + weight * posterior.mean
        lower, upper = posterior.interval()
        return {
            "probability": round(probability, 8),
            "lower": round(min(probability, lower), 8),
            "upper": round(max(probability, upper), 8),
            "level": level,
            "samples": posterior.samples,
            "sparse": sparse,
            "revision": CALIBRATION_REVISION,
        }

