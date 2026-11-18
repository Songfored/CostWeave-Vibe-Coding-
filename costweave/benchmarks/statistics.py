"""Small dependency-free paired bootstrap primitives for v0.6.0."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class BootstrapInterval:
    mean_difference: float
    lower: float
    upper: float
    samples: int
    seed: int

    def __post_init__(self) -> None:
        for name in ("mean_difference", "lower", "upper"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, float(value))
        if self.lower > self.upper:
            raise ValueError("bootstrap interval must satisfy lower <= upper")
        if isinstance(self.samples, bool) or not isinstance(self.samples, int) or self.samples < 1:
            raise ValueError("samples must be a positive int")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or not 0 <= self.seed <= (2**31 - 1):
            raise ValueError("seed must be between 0 and 2^31-1")

    def to_dict(self) -> dict[str, float | int]:
        return {
            "mean_difference": self.mean_difference,
            "lower": self.lower,
            "upper": self.upper,
            "samples": self.samples,
            "seed": self.seed,
        }


def _values(values: Sequence[float], name: str) -> tuple[float, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be a sequence")
    if not values:
        raise ValueError(f"{name} must not be empty")
    result: list[float] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{name}[{index}] must be finite")
        result.append(float(value))
    return tuple(result)


def _seed(seed: int) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be int")
    if not 0 <= seed <= (2**31 - 1):
        raise ValueError("seed must be between 0 and 2^31-1")
    return seed


def _samples(samples: int) -> int:
    if isinstance(samples, bool) or not isinstance(samples, int):
        raise TypeError("samples must be int")
    if not 1 <= samples <= 1_000_000:
        raise ValueError("samples must be between 1 and 1,000,000")
    return samples


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def paired_bootstrap_difference(
    candidate_values: Sequence[float],
    baseline_values: Sequence[float],
    *,
    samples: int,
    seed: int,
) -> BootstrapInterval:
    """Bootstrap paired per-case differences without mutating caller inputs."""

    candidate = _values(candidate_values, "candidate_values")
    baseline = _values(baseline_values, "baseline_values")
    if len(candidate) != len(baseline):
        raise ValueError("candidate and baseline sequences must have equal length")
    sample_count = _samples(samples)
    random_seed = _seed(seed)
    point = sum(a - b for a, b in zip(candidate, baseline)) / len(candidate)
    rng = random.Random(random_seed)
    bootstrapped: list[float] = []
    for _ in range(sample_count):
        indexes = [rng.randrange(len(candidate)) for _ in range(len(candidate))]
        bootstrapped.append(sum(candidate[index] - baseline[index] for index in indexes) / len(indexes))
    return BootstrapInterval(
        mean_difference=point,
        lower=_percentile(bootstrapped, 0.025),
        upper=_percentile(bootstrapped, 0.975),
        samples=sample_count,
        seed=random_seed,
    )


def paired_bootstrap_cost_reduction_pct(
    candidate_costs: Sequence[float],
    baseline_costs: Sequence[float],
    *,
    samples: int,
    seed: int,
) -> BootstrapInterval | None:
    """Return ``(baseline-candidate)/baseline`` percent intervals.

    A zero baseline is not converted to infinity.  If a resample contains no
    baseline cost, that draw is omitted; if all draws are undefined the result
    is ``None``.
    """

    candidate = _values(candidate_costs, "candidate_costs")
    baseline = _values(baseline_costs, "baseline_costs")
    if len(candidate) != len(baseline):
        raise ValueError("candidate and baseline sequences must have equal length")
    if any(value < 0 for value in candidate + baseline):
        raise ValueError("costs must be non-negative")
    sample_count = _samples(samples)
    random_seed = _seed(seed)
    baseline_point = sum(baseline)
    if baseline_point <= 0:
        return None
    candidate_point = sum(candidate)
    point = (baseline_point - candidate_point) / baseline_point * 100.0
    rng = random.Random(random_seed)
    bootstrapped: list[float] = []
    for _ in range(sample_count):
        indexes = [rng.randrange(len(candidate)) for _ in range(len(candidate))]
        candidate_total = sum(candidate[index] for index in indexes)
        baseline_total = sum(baseline[index] for index in indexes)
        if baseline_total > 0:
            bootstrapped.append((baseline_total - candidate_total) / baseline_total * 100.0)
    if not bootstrapped:
        return None
    return BootstrapInterval(
        mean_difference=point,
        lower=_percentile(bootstrapped, 0.025),
        upper=_percentile(bootstrapped, 0.975),
        samples=len(bootstrapped),
        seed=random_seed,
    )


def paired_bootstrap_p95_latency_regression_pct(
    candidate_latencies: Sequence[float],
    baseline_latencies: Sequence[float],
    *,
    samples: int,
    seed: int,
) -> BootstrapInterval | None:
    """Estimate the percent change in nearest-rank P95 across paired cases.

    Each draw uses the same case indices for both policies. A zero baseline
    P95 makes the point estimate undefined; zero-denominator draws are omitted.
    """
    candidate = _values(candidate_latencies, "candidate_latencies")
    baseline = _values(baseline_latencies, "baseline_latencies")
    if len(candidate) != len(baseline):
        raise ValueError("latency sequences must have equal length")
    if min(candidate) < 0 or min(baseline) < 0:
        raise ValueError("latencies must be non-negative")
    sample_count = _samples(samples)
    random_seed = _seed(seed)
    baseline_p95 = _percentile(baseline, 0.95)
    if baseline_p95 == 0:
        return None
    point = (_percentile(candidate, 0.95) - baseline_p95) / baseline_p95 * 100.0

    rng = random.Random(random_seed)
    draws: list[float] = []
    case_count = len(baseline)
    for _ in range(sample_count):
        indices = [rng.randrange(case_count) for _ in range(case_count)]
        baseline_draw_p95 = _percentile([baseline[i] for i in indices], 0.95)
        if baseline_draw_p95 > 0:
            candidate_draw_p95 = _percentile([candidate[i] for i in indices], 0.95)
            draws.append((candidate_draw_p95 - baseline_draw_p95) / baseline_draw_p95 * 100.0)
    if not draws:
        return None
    return BootstrapInterval(
        mean_difference=point,
        lower=_percentile(draws, 0.025),
        upper=_percentile(draws, 0.975),
        samples=len(draws),
        seed=random_seed,
    )


__all__ = [
    "BootstrapInterval",
    "paired_bootstrap_cost_reduction_pct",
    "paired_bootstrap_difference",
    "paired_bootstrap_p95_latency_regression_pct",
]
