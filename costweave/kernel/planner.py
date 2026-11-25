"""Budgeted shortest-path search over sets of accepted artifacts."""

from __future__ import annotations

import hashlib
import heapq
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

from .contracts import Job, Operation, canonical_json


@dataclass(frozen=True, slots=True)
class FallbackAssessment:
    speculative_operation: str
    remaining_if_rejected: int
    fallback_cost_units: int | None
    decision: str


@dataclass(frozen=True, slots=True)
class EvidenceDecision:
    rule_index: int
    artifact: str
    field: str
    artifact_sha256: str | None
    operation: str | None
    request: str | None
    reason: str | None
    policy_version: int = 1
    kind: str | None = None

    def to_dict(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True, slots=True)
class Plan:
    status: str
    operations: tuple[str, ...] = ()
    cost_units: int | None = None
    optimal: bool = False
    states_examined: int = 0
    fallback: FallbackAssessment | None = None
    evidence: EvidenceDecision | None = None

    def to_dict(self) -> dict:
        result = {"status": self.status, "operations": list(self.operations), "cost_units": self.cost_units,
                  "optimal": self.optimal, "states_examined": self.states_examined}
        if self.fallback is not None:
            result["fallback"] = asdict(self.fallback)
        if self.evidence is not None:
            result["evidence"] = self.evidence.to_dict()
        return result


def _relevant(job: Job, goals: set[str], available: set[str], excluded: set[str]) -> list[Operation]:
    producers = defaultdict(list)
    for op in job.operations:
        if op.id not in excluded:
            producers[op.produces].append(op)
    pending = list(goals)
    seen = set(available)
    useful = []
    while pending:
        artifact = pending.pop()
        if artifact in seen:
            continue
        seen.add(artifact)
        for op in producers[artifact]:
            useful.append(op)
            pending.extend(op.requires)
    return sorted(useful, key=lambda op: op.id)


def _witness(operations: list[Operation], available: set[str], goals: set[str]) -> tuple[Operation, ...] | None:
    """Find one executable derivation, or prove goals outside the reachable closure.

    Tree costs prioritize discovery. The extracted witness counts each producer
    once, even when its output serves several consumers.
    """
    needed_inputs = {}
    consumers = defaultdict(list)
    ready = []
    for index, op in enumerate(operations):
        missing = set(op.requires) - available
        needed_inputs[index] = len(missing)
        for artifact in missing:
            consumers[artifact].append(index)
        if not missing:
            heapq.heappush(ready, (op.cost_units, index))
    estimates = dict.fromkeys(available, 0)
    order = []
    missing_goals = goals - available
    while ready and missing_goals:
        estimate, index = heapq.heappop(ready)
        op = operations[index]
        if op.produces in estimates:
            continue
        estimates[op.produces] = estimate
        order.append(op)
        missing_goals.discard(op.produces)
        for child in consumers[op.produces]:
            needed_inputs[child] -= 1
            if needed_inputs[child] == 0:
                successor = operations[child]
                priority = successor.cost_units + sum(estimates[p] for p in successor.requires)
                heapq.heappush(ready, (priority, child))
    if missing_goals:
        return None
    needed = goals - available
    path = []
    for op in reversed(order):
        if op.produces in needed:
            path.append(op)
            needed.remove(op.produces)
            needed.update(set(op.requires) - available)
    return tuple(reversed(path))


def _evidence_choice(job: Job, artifacts: Mapping, excluded: set[str], remaining_units: int) -> Plan | None:
    operations = {op.id: op for op in job.operations}
    for index, rule in enumerate(job.evidence_rules):
        op = operations.get(rule.operation)
        if (op.produces if op is not None else rule.request) in artifacts:
            continue
        if rule.acquire and rule.artifact not in artifacts:
            goal = {rule.artifact}
            available = set(artifacts)
            producers = _relevant(job, goal, available, excluded)
            acquisition = _cheapest_plan(producers, available, goal, remaining_units, job.search_limit)
            evidence = EvidenceDecision(index, rule.artifact, rule.field, None, None, None, None, kind="acquire")
            # This plan prices the report only. The terminal path depends on its value.
            return replace(acquisition, optimal=False, evidence=evidence)
        report = artifacts.get(rule.artifact)
        if not isinstance(report, dict) or rule.field not in report:
            continue
        value = report[rule.field]
        if type(value) is not type(rule.equals) or value != rule.equals:
            continue
        digest = hashlib.sha256(canonical_json(report).encode("utf-8")).hexdigest()
        evidence = EvidenceDecision(index, rule.artifact, rule.field, digest, rule.operation, rule.request, rule.reason)
        if op is None:
            return Plan("waiting_for_input", evidence=evidence)
        if op.id in excluded:
            return Plan("evidence_action_rejected", evidence=evidence)
        if not set(op.requires) <= artifacts.keys():
            return Plan("evidence_prerequisite_missing", evidence=evidence)
        if op.cost_units > remaining_units:
            return Plan("over_budget", evidence=evidence)
        # Only this observation-conditioned action is authorized. Later costs depend on its outcome.
        return Plan("ready", (op.id,), op.cost_units, evidence=evidence)
    return None


def decide(job: Job, artifacts: Mapping[str, Any], excluded: Collection[str], remaining_units: int) -> Plan:
    """Choose the next action from admitted artifact values and the frozen job rules."""
    if not isinstance(artifacts, Mapping):
        raise ValueError("artifacts must map names to accepted JSON values")
    return plan(job, artifacts, excluded, remaining_units)


def plan(job: Job, available: Collection[str], excluded: Collection[str], remaining_units: int) -> Plan:
    """Select the next step from accepted artifacts, settled attempts and the remaining budget."""
    if type(remaining_units) is not int or not 0 <= remaining_units <= job.budget_units:
        raise ValueError("remaining_units must be an integer within the job budget")
    if isinstance(available, (str, bytes)) or isinstance(excluded, (str, bytes)):
        raise ValueError("available and excluded must be collections of names")
    artifacts = available if isinstance(available, Mapping) else None
    if job.evidence_rules and artifacts is None:
        raise ValueError("evidence rules require an artifact mapping with accepted values")
    available = set(available)
    excluded = set(excluded)
    declared = job.inputs.keys() | {op.produces for op in job.operations}
    declared.update(rule.request for rule in job.evidence_rules if rule.request is not None)
    if available - declared or excluded - {op.id for op in job.operations}:
        raise ValueError("unknown available artifact or excluded operation")
    goals = set(job.goals)
    if goals <= available:
        return Plan("complete", (), 0, True)
    if job.evidence_rules:
        choice = _evidence_choice(job, artifacts, excluded, remaining_units)
        if choice is not None:
            return choice
    operations = _relevant(job, goals, available, excluded)
    cheapest = _cheapest_plan(operations, available, goals, remaining_units, job.search_limit)
    if job.planning_policy != "preserve_fallback" or cheapest.status != "ready":
        return cheapest
    first = next(op for op in operations if op.id == cheapest.operations[0])
    if first.route_role != "speculative" or first.cost_units == 0:
        return cheapest

    # A rejection yields no artifact. Charging the full quote models the largest
    # permitted loss; a real receipt can leave more budget on the next decision.
    remaining_if_rejected = remaining_units - first.cost_units
    fallback_ops = [op for op in operations if op.route_role == "fallback"]
    fallback = _cheapest_plan(fallback_ops, available, goals, remaining_units, job.search_limit)
    selected = cheapest
    if fallback.status != "ready":
        decision = fallback.status
    elif fallback.cost_units <= remaining_if_rejected:
        decision = "fallback_still_fits"
    elif fallback.optimal:
        # Only a proven minimum establishes that every declared fallback would
        # become unaffordable. This choice optimizes a different objective.
        selected = replace(fallback, optimal=False)
        decision = "take_fallback"
    else:
        decision = "search_limit"
    assessment = FallbackAssessment(first.id, remaining_if_rejected, fallback.cost_units, decision)
    return replace(selected, states_examined=cheapest.states_examined + fallback.states_examined,
                   fallback=assessment)


def _cheapest_plan(operations: list[Operation], available: set[str], goals: set[str],
                   remaining_units: int, search_limit: int) -> Plan:
    """Find a least-cost successful derivation, admitting at most search_limit states."""
    witness = _witness(operations, available, goals)
    if witness is None:
        return Plan("unreachable")
    witness_cost = sum(op.cost_units for op in witness)
    incumbent = None
    if witness_cost <= remaining_units:
        incumbent = Plan("ready", tuple(op.id for op in witness), witness_cost)

    # Project out the initial artifacts. For a goal set G, the sum of the
    # cheapest producer for each missing goal is an admissible, consistent
    # lower bound, since an operation has exactly one output.
    names = sorted({name for op in operations for name in (*op.requires, op.produces)} - available)
    bits = {name: 1 << index for index, name in enumerate(names)}
    goal_mask = sum(bits[name] for name in goals - available)
    goal_floor = {}
    edges = []
    for op in operations:
        output = bits[op.produces]
        required = sum(bits[name] for name in op.requires if name not in available)
        edges.append((required, output, op))
        if output & goal_mask:
            goal_floor[output] = min(goal_floor.get(output, op.cost_units), op.cost_units)
    lower = sum(goal_floor.values())
    if lower > remaining_units:
        return Plan("over_budget")
    queue = [(lower, 0, 0, 0)]  # lower bound, negative progress, spent, state bitmask
    best = {0: 0}
    parents = {}
    admitted = 1
    examined = 0
    while queue:
        bound, _, spent, state = heapq.heappop(queue)
        if spent != best[state]:
            continue
        if incumbent is not None and bound >= incumbent.cost_units:
            break
        examined += 1
        for required, output, op in edges:
            if state & output or required & ~state:
                continue
            after = state | output
            quote = spent + op.cost_units
            if after in best and best[after] <= quote:
                continue
            after_bound = bound + op.cost_units - goal_floor.get(output, 0)
            if after_bound > remaining_units or (incumbent is not None and after_bound >= incumbent.cost_units):
                continue
            if after & goal_mask == goal_mask:
                path = [op.id]
                cursor = state
                while cursor:
                    cursor, predecessor = parents[cursor]
                    path.append(predecessor)
                incumbent = Plan("ready", tuple(reversed(path)), quote)
                continue
            if admitted >= search_limit:
                if incumbent is None:
                    return Plan("search_limit", states_examined=examined)
                return Plan("ready", incumbent.operations, incumbent.cost_units, False, examined)
            best[after] = quote
            parents[after] = (state, op.id)
            heapq.heappush(queue, (after_bound, -after.bit_count(), quote, after))
            admitted += 1
    if incumbent is not None:
        return Plan("ready", incumbent.operations, incumbent.cost_units, True, examined)
    return Plan("over_budget", states_examined=examined)
