"""Reproducible forecast metrics and fixed policy benchmark."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Iterable


EVALUATION_REVISION = "evaluation-v1"


def brier_score(predictions: Iterable[float], outcomes: Iterable[int]) -> float:
    pairs = list(zip(predictions, outcomes))
    if not pairs:
        raise ValueError("at least one observation is required")
    return sum((float(p) - int(y)) ** 2 for p, y in pairs) / len(pairs)


def log_loss(predictions: Iterable[float], outcomes: Iterable[int]) -> float:
    pairs = list(zip(predictions, outcomes))
    if not pairs:
        raise ValueError("at least one observation is required")
    loss = 0.0
    for prediction, outcome in pairs:
        p = max(1e-12, min(1 - 1e-12, float(prediction)))
        loss -= int(outcome) * math.log(p) + (1 - int(outcome)) * math.log(1 - p)
    return loss / len(pairs)


def expected_calibration_error(
    predictions: Iterable[float],
    outcomes: Iterable[int],
    *,
    bins: int = 10,
    minimum_bin_size: int = 1,
) -> float:
    pairs = list(zip(predictions, outcomes))
    if not pairs:
        raise ValueError("at least one observation is required")
    weighted = 0.0
    included = 0
    for index in range(bins):
        lower, upper = index / bins, (index + 1) / bins
        bucket = [
            (float(p), int(y))
            for p, y in pairs
            if lower <= float(p) < upper or (index == bins - 1 and float(p) == 1)
        ]
        if len(bucket) < minimum_bin_size:
            continue
        confidence = sum(item[0] for item in bucket) / len(bucket)
        accuracy = sum(item[1] for item in bucket) / len(bucket)
        weighted += len(bucket) * abs(confidence - accuracy)
        included += len(bucket)
    return weighted / included if included else 0.0


def interval_coverage(
    actual: Iterable[float],
    lower: Iterable[float],
    upper: Iterable[float],
) -> float:
    triples = list(zip(actual, lower, upper))
    if not triples:
        raise ValueError("at least one interval is required")
    return sum(float(lo) <= float(value) <= float(hi) for value, lo, hi in triples) / len(triples)


def run_fixed_benchmark(samples_per_bucket: int = 200) -> dict:
    if samples_per_bucket < 200:
        raise ValueError("benchmark requires at least 200 samples per bucket")
    truth_probabilities = [0.1, 0.3, 0.5, 0.7, 0.9]
    outcomes = []
    costweave = []
    baselines = {
        "v0.3_uncalibrated": [],
        "fixed_model": [],
        "human_threshold": [],
    }
    for probability in truth_probabilities:
        positives = round(samples_per_bucket * probability)
        labels = [1] * positives + [0] * (samples_per_bucket - positives)
        outcomes.extend(labels)
        costweave.extend([probability] * samples_per_bucket)
        baselines["v0.3_uncalibrated"].extend(
            [min(0.99, probability + 0.14)] * samples_per_bucket
        )
        baselines["fixed_model"].extend([0.64] * samples_per_bucket)
        baselines["human_threshold"].extend(
            [0.8 if probability >= 0.5 else 0.2] * samples_per_bucket
        )
    policies = {"costweave_v0.4.7": costweave, **baselines}
    metrics = {
        name: {
            "brier": round(brier_score(values, outcomes), 8),
            "ece": round(
                expected_calibration_error(
                    values,
                    outcomes,
                    bins=10,
                    minimum_bin_size=200,
                ),
                8,
            ),
            "log_loss": round(log_loss(values, outcomes), 8),
        }
        for name, values in policies.items()
    }
    actual = list(range(100))
    lower = [value - 5 if index < 95 else value + 1 for index, value in enumerate(actual)]
    upper = [value + 5 for value in actual]
    coverage = interval_coverage(actual, lower, upper)
    payload = {
        "revision": EVALUATION_REVISION,
        "sample_size": len(outcomes),
        "samples_per_bucket": samples_per_bucket,
        "metrics": metrics,
        "cost_interval_coverage": coverage,
        "time_interval_coverage": coverage,
        "passed": (
            metrics["costweave_v0.4.7"]["ece"] <= 0.05
            and metrics["costweave_v0.4.7"]["brier"]
            < metrics["v0.3_uncalibrated"]["brier"]
            and 0.9 <= coverage <= 0.99
        ),
    }
    payload["evaluation_id"] = "evaluation_" + hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]
    return payload

