"""Calibrate complete routes on independent, paired, externally scored tasks.

A route's outcome is timely and correct when its audited outcome is correct and
its elapsed time fits the remaining deadline. The empirical distribution of
correct completion times supports any deadline, including one chosen at runtime.
All model fields are JSON data. Case identifiers and original task data are
used only while fitting. A fitted or loaded model must be validated before use.
"""

from __future__ import annotations

import math
from collections import Counter


def _settings(model):
    from .contracts import MAX_OPERATIONS, MAX_UNITS, _integer, _name

    actions = model["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_OPERATIONS:
        raise ValueError("actions must be a nonempty list of operation names")
    for name in actions:
        _name(name, "action")
    if len(set(actions)) != len(actions) or model["baseline"] not in actions:
        raise ValueError("actions must be unique and include the baseline operation")
    _integer(model["min_support"], "min_support", 1, MAX_UNITS)
    alpha = model["alpha"]
    if type(alpha) not in (float, int) or not 0 < alpha < 1:
        raise ValueError("alpha must be between zero and one")


def _empty_totals():
    return {"cost_units": 0, "baseline_cost_units": 0, "elapsed_ms": 0, "baseline_elapsed_ms": 0}


def _time_key(pair):
    return tuple(-1 if ms is None else ms for ms in pair)


def fit_verification_model(rows, *, baseline, actions, min_support=12, alpha=0.05) -> dict:
    """Fit on unique calibration cases with predeclared strata and route IDs.

    Each row has case_id, stratum and outcomes. Each observed action maps to
    correct (bool), cost_units and elapsed_ms (nonnegative integers including
    the entire route). Missing outcomes are omitted. Such rows count against
    coverage; a partly observed action cannot authorize a choice.
    """
    from .contracts import MAX_UNITS, _fields, _integer, _name

    if not isinstance(actions, (tuple, list)):
        raise ValueError("actions must be a list or tuple of operation names")
    model = {"version": 1, "baseline": baseline, "actions": list(actions), "min_support": min_support,
             "alpha": alpha, "strata": {}}
    _settings(model)
    model["actions"].sort()
    seen = set()
    for row in rows:
        _fields(row, {"case_id", "stratum", "outcomes"}, set(), "calibration row")
        case_id = _name(row["case_id"], "case_id")
        if case_id in seen:
            raise ValueError("case_id must be unique across calibration rows")
        seen.add(case_id)
        stratum = _name(row["stratum"], "stratum")
        outcomes = row["outcomes"]
        if not isinstance(outcomes, dict) or outcomes.keys() - model["actions"]:
            raise ValueError("outcomes must map declared operation names to observations")
        for observation in outcomes.values():
            _fields(observation, {"correct", "cost_units", "elapsed_ms"}, set(), "route outcome")
            if type(observation["correct"]) is not bool:
                raise ValueError("correct must be an audited boolean")
            for key in ("cost_units", "elapsed_ms"):
                _integer(observation[key], key, 0, MAX_UNITS)
        group = model["strata"].setdefault(stratum, {"cases": 0, "routes": {}})
        group["cases"] += 1
        direct = outcomes.get(baseline)
        if direct is None:
            continue
        for name, outcome in outcomes.items():
            cell = group["routes"].get(name)
            if cell is None:
                cell = group["routes"][name] = {**_empty_totals(), "times": Counter()}
            for key in ("cost_units", "elapsed_ms"):
                cell[key] += outcome[key]
                cell["baseline_" + key] += direct[key]
            pair = (direct["elapsed_ms"] if direct["correct"] else None,
                    outcome["elapsed_ms"] if outcome["correct"] else None)
            cell["times"][pair] += 1

    for group in model["strata"].values():
        for cell in group["routes"].values():
            counts = cell["times"]
            cell["times"] = [[*pair, counts[pair]] for pair in sorted(counts, key=_time_key)]
    validate_verification_model(model)
    return model


def validate_verification_model(model) -> None:
    """Validate a deserialized v1 model before it is admitted to a frozen job."""
    from .contracts import MAX_UNITS, _fields, _integer, _name

    _fields(model, {"version", "baseline", "actions", "min_support", "alpha", "strata"}, set(), "verification model")
    _integer(model["version"], "verification model version", 1, 1)
    _settings(model)
    if not isinstance(model["strata"], dict):
        raise ValueError("strata must be an object")
    for name, group in model["strata"].items():
        _name(name, "stratum")
        _fields(group, {"cases", "routes"}, set(), "calibration stratum")
        cases = _integer(group["cases"], "cases", 1, MAX_UNITS)
        routes = group["routes"]
        if not isinstance(routes, dict) or routes.keys() - model["actions"]:
            raise ValueError("routes must map declared operation names to paired observations")
        direct_times = Counter()
        for action, cell in routes.items():
            _fields(cell, {"times", *_empty_totals()}, set(), "route calibration")
            if not isinstance(cell["times"], list) or not cell["times"]:
                raise ValueError("times must be a nonempty array of paired completion-time counts")
            pairs = 0
            route_ms = direct_ms = 0
            observed_direct = Counter()
            seen = set()
            for entry in cell["times"]:
                if not isinstance(entry, list) or len(entry) != 3:
                    raise ValueError("expected [baseline_correct_ms, route_correct_ms, count]")
                left, right, count = entry
                for ms in (left, right):
                    if ms is not None:
                        _integer(ms, "correct completion time", 0, MAX_UNITS)
                _integer(count, "paired observations", 1, cases)
                if (left, right) in seen or (action == model["baseline"] and left != right):
                    raise ValueError("invalid paired completion-time counts")
                seen.add((left, right))
                observed_direct[left] += count
                pairs += count
                direct_ms += (left or 0) * count
                route_ms += (right or 0) * count
            if pairs > cases:
                raise ValueError("paired observations exceed calibration cases")
            for key in _empty_totals():
                _integer(cell[key], key, 0, MAX_UNITS * pairs)
            if cell["elapsed_ms"] < route_ms or cell["baseline_elapsed_ms"] < direct_ms:
                raise ValueError("observed completion times exceed total elapsed time")
            if action == model["baseline"]:
                if cell["cost_units"] != cell["baseline_cost_units"] or cell["elapsed_ms"] != cell["baseline_elapsed_ms"]:
                    raise ValueError("baseline totals must agree")
                direct_times = observed_direct
        for cell in routes.values():
            paired_direct = Counter()
            for left, _, count in cell["times"]:
                paired_direct[left] += count
            if paired_direct - direct_times:
                raise ValueError("candidate has paired outcomes outside the baseline cohort")


def estimate_action(model, stratum, action, *, remaining_ms=None) -> dict:
    """Return paired effects and conservative joint quality for a validated model.

    For independent cases in predeclared strata, the DKW bound applies
    simultaneously over time thresholds. An incorrect outcome has infinite
    correct-completion time. Bonferroni allocates alpha across strata, routes
    and both marginal distributions of each pair. Separate route and baseline
    bounds yield the conservative lower bound on their difference.
    """
    from .contracts import MAX_UNITS, _integer

    if remaining_ms is not None:
        _integer(remaining_ms, "remaining_ms", 0, MAX_UNITS)
    if action not in model["actions"]:
        raise ValueError("action must be a declared calibration route")
    group = model["strata"].get(stratum)
    cell = group["routes"].get(action) if group is not None else None
    pairs = corrected = spoiled = successes = 0
    if cell is not None:
        for left, right, count in cell["times"]:
            direct_ok = left is not None and (remaining_ms is None or left <= remaining_ms)
            route_ok = right is not None and (remaining_ms is None or right <= remaining_ms)
            pairs += count
            successes += count if route_ok else 0
            corrected += count if route_ok and not direct_ok else 0
            spoiled += count if direct_ok and not route_ok else 0
    result = {"status": "unknown_stratum" if group is None else "insufficient_support",
              "pairs": pairs, "missing_pairs": None if group is None else group["cases"] - pairs,
              "corrected": corrected, "spoiled": spoiled, "ties": pairs - corrected - spoiled,
              "quality": None, "quality_lower": None, "net_gain": None, "gain_lower": None,
              "mean_cost_units": None, "mean_elapsed_ms": None, "mean_extra_cost_units": None,
              "mean_extra_elapsed_ms": None}
    if group is None or pairs < model["min_support"] or pairs != group["cases"]:
        return result
    comparisons = 4 * len(model["actions"]) * len(model["strata"])
    radius = math.sqrt((math.log(comparisons) - math.log(model["alpha"])) / (2 * pairs))
    quality = successes / pairs
    gain = (corrected - spoiled) / pairs
    result.update(status="supported", quality=quality, quality_lower=max(0, quality - radius),
                  net_gain=gain, gain_lower=0 if action == model["baseline"] else max(-1, gain - 2 * radius),
                  mean_cost_units=cell["cost_units"] / pairs, mean_elapsed_ms=cell["elapsed_ms"] / pairs,
                  mean_extra_cost_units=(cell["cost_units"] - cell["baseline_cost_units"]) / pairs,
                  mean_extra_elapsed_ms=(cell["elapsed_ms"] - cell["baseline_elapsed_ms"]) / pairs)
    return result
