"""Budgeted shortest-path search over sets of accepted artifacts."""

from __future__ import annotations

import hashlib
import heapq
import math
from itertools import count
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

from .contracts import Job, Operation, canonical_json
from .verification_value import estimate_action
from .solver import BSAOPlanner


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
    field: str | None
    artifact_sha256: str | None
    operation: str | None
    request: str | None
    reason: str | None
    policy_version: int = 1
    kind: str | None = None
    path: tuple[str | int, ...] | None = None

    def to_dict(self) -> dict:
        result = {key: value for key, value in asdict(self).items() if value is not None}
        if self.path is not None:
            result["path"] = list(self.path)
        return result


@dataclass(frozen=True, slots=True)
class Plan:
    status: str
    operations: tuple[str, ...] = ()
    cost_units: int | None = None
    optimal: bool = False
    states_examined: int = 0
    fallback: FallbackAssessment | None = None
    audit: dict | None = None
    evidence: EvidenceDecision | None = None
    verification: dict | None = None

    def to_dict(self) -> dict:
        result = {"status": self.status, "operations": list(self.operations), "cost_units": self.cost_units,
                  "optimal": self.optimal, "states_examined": self.states_examined}
        if self.fallback is not None:
            result["fallback"] = asdict(self.fallback)
        if self.audit is not None:
            result["audit"] = self.audit
        if self.evidence is not None:
            result["evidence"] = self.evidence.to_dict()
        if self.verification is not None:
            result["verification"] = self.verification
        return result


def _relevant(operations, goals: set[str], available: set[str], excluded: set[str]) -> list[Operation]:
    producers = defaultdict(list)
    for op in operations:
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
            pending.extend(op.dependencies)
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
        missing = set(op.dependencies) - available
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
                priority = successor.cost_units + sum(estimates[p] for p in successor.dependencies)
                heapq.heappush(ready, (priority, child))
    if missing_goals:
        return None
    needed = goals - available
    path = []
    for op in reversed(order):
        if op.produces in needed:
            path.append(op)
            needed.remove(op.produces)
            needed.update(set(op.dependencies) - available)
    return tuple(reversed(path))


def _resource_plan(operations, available, goals, budget, limit, remaining_ms, risk):
    """Prefer the ordinary shortest path. Search nondominated resource labels only when it fails to fit."""
    if goals <= available:
        return Plan("complete", (), 0, True)
    cheapest = _cheapest_plan(operations, available, goals, budget, limit)
    if cheapest.status != "ready":
        return cheapest
    by_id = {op.id: op for op in operations}
    witness = [by_id[op_id] for op_id in cheapest.operations]
    if (sum(op.false_pass_risk for op in witness) <= risk + 1e-12
            and (remaining_ms is None or sum(op.latency_p50_ms for op in witness) <= remaining_ms)):
        return cheapest

    initial = frozenset(available)
    labels = {initial: [(0, 0, 0.0)]}
    serial = count()
    queue = [(0, 0, 0.0, next(serial), initial, ())]
    reasons = set()
    admitted = 1
    while queue:
        cost, duration, used_risk, _, state, path = heapq.heappop(queue)
        if (cost, duration, used_risk) not in labels[state]:
            continue
        if goals <= state:
            return Plan("ready", path, cost, True, admitted)
        for op in operations:
            if op.produces in state or not state.issuperset(op.dependencies):
                continue
            quote = cost + op.cost_units
            ms = duration + op.latency_p50_ms
            exposure = used_risk + op.false_pass_risk
            if quote > budget:
                reasons.add("over_budget")
                continue
            if remaining_ms is not None and ms > remaining_ms:
                reasons.add("deadline_unreachable")
                continue
            if exposure > risk + 1e-12:
                reasons.add("risk_exceeded")
                continue
            after = state | {op.produces}
            frontier = labels.setdefault(after, [])
            if any(c <= quote and t <= ms and r <= exposure for c, t, r in frontier):
                continue
            if admitted >= limit:
                return Plan("search_limit", states_examined=admitted)
            frontier[:] = [(c, t, r) for c, t, r in frontier
                           if not (quote <= c and ms <= t and exposure <= r)]
            frontier.append((quote, ms, exposure))
            heapq.heappush(queue, (quote, ms, exposure, next(serial), after, (*path, op.id)))
            admitted += 1
    status = next(iter(reasons)) if len(reasons) == 1 else "resource_constraints"
    return Plan(status, states_examined=admitted)


def _after_mandatory(job, artifacts, excluded, forced, budget, remaining_ms, risk, required=()):
    """Optimistic completion assuming accepted future outputs and feasible authorized clarifications."""
    available = set(artifacts)
    goals = set(job.goals) | set(required)
    operations = {op.id: op for op in job.operations}
    # A future report can authorize input we cannot yet request. Never expose that value to a predicate.
    potential_requests = [rule for rule in job.evidence_rules if rule.request and rule.artifact not in artifacts]
    for op in forced:
        available.add(op.produces)
        budget -= op.cost_units
        risk -= op.false_pass_risk
        if remaining_ms is not None:
            remaining_ms -= op.latency_p50_ms
    for rule in job.evidence_rules:
        if goals <= available:
            break
        if rule.artifact not in artifacts:
            # This rule may finish the job before a lower-priority observed rule can apply.
            # Its value is unknown, so only structural/resource impossibility can be proved.
            break
        if not rule.matches(artifacts):
            continue
        if rule.request is not None:
            available.add(rule.request)
            continue
        op = operations[rule.operation]
        if op.produces in available:
            continue
        if op.id in excluded:
            return Plan("evidence_action_rejected")
        if not set(op.dependencies) <= available:
            return Plan("evidence_prerequisite_missing")
        available.add(op.produces)
        budget -= op.cost_units
        risk -= op.false_pass_risk
        if remaining_ms is not None:
            remaining_ms -= op.latency_p50_ms
    if budget < 0:
        return Plan("over_budget")
    if risk < -1e-12:
        return Plan("risk_exceeded")
    if remaining_ms is not None and remaining_ms < 0:
        return Plan("deadline_unreachable")
    if goals <= available:
        return Plan("complete", (), 0, True)
    # A possible future clarification still requires its source report. These zero-cost
    # edges exist only inside the lookahead and can never become dispatch decisions.
    future = [op for op in job.operations if op.id not in excluded and op.produces not in available]
    for index, rule in enumerate(potential_requests):
        if rule.request not in available:
            synthetic_id = f"prospective-request-{index}"
            while synthetic_id in operations:
                synthetic_id += "_"
            future.append(Operation(synthetic_id, [rule.artifact], rule.request, "clarify", "present", 0))
    relevant = _relevant(future, goals, available, set())
    return _resource_plan(relevant, available, goals, budget, job.search_limit, remaining_ms, risk)


def _evidence_choice(job, artifacts, excluded, remaining_units, remaining_ms, risk):
    operations = {op.id: op for op in job.operations}
    excluded = excluded | {op.id for op in job.operations if op.guard_state(artifacts) == "falsified"}
    for index, rule in enumerate(job.evidence_rules):
        op = operations.get(rule.operation)
        if (op.produces if op is not None else rule.request) in artifacts:
            continue
        if rule.acquire and rule.artifact not in artifacts:
            goal = {rule.artifact}
            available = set(artifacts)
            producers = _relevant(job.operations, goal, available, excluded)
            acquisition = _resource_plan(producers, available, goal, remaining_units, job.search_limit,
                                         remaining_ms, risk)
            evidence = EvidenceDecision(index, rule.artifact, rule.field, None, None, None, None,
                                        kind="acquire", path=rule.path)
            if acquisition.status != "ready":
                return replace(acquisition, optimal=False, evidence=evidence)
            # The cheapest way to acquire a report can strand an otherwise affordable goal.
            # Try other immediately executable report ancestors when that first step is ruled out.
            first_id = acquisition.operations[0]
            choices = [operations[first_id], *(candidate for candidate in producers
                       if candidate.id != first_id and available.issuperset(candidate.dependencies))]
            failures = []
            for first in choices[:job.search_limit]:
                completion = _after_mandatory(job, artifacts, excluded, [first], remaining_units,
                                              remaining_ms, risk, required=goal)
                if completion.status not in ("ready", "complete"):
                    failures.append(completion)
                    continue
                if first.id == first_id:
                    return replace(acquisition, optimal=False, evidence=evidence)
                tail = _resource_plan([candidate for candidate in producers if candidate.produces != first.produces],
                                      available | {first.produces}, goal, remaining_units - first.cost_units,
                                      job.search_limit, None if remaining_ms is None else remaining_ms - first.latency_p50_ms,
                                      risk - first.false_pass_risk)
                if tail.status in ("ready", "complete"):
                    return Plan("ready", (first.id, *tail.operations), first.cost_units + tail.cost_units,
                                evidence=evidence, states_examined=completion.states_examined + tail.states_examined)
                failures.append(tail)
            if len(choices) > job.search_limit or any(fail.status == "search_limit" for fail in failures):
                return Plan("search_limit", evidence=evidence)
            return replace(failures[0], evidence=evidence)
        if not rule.matches(artifacts):
            continue
        digest = hashlib.sha256(canonical_json(artifacts[rule.artifact]).encode("utf-8")).hexdigest()
        evidence = EvidenceDecision(index, rule.artifact, rule.field, digest, rule.operation,
                                    rule.request, rule.reason, path=rule.path)
        if op is None:
            return Plan("waiting_for_input", evidence=evidence)
        if op.id in excluded:
            return Plan("evidence_action_rejected", evidence=evidence)
        if not set(op.dependencies) <= artifacts.keys():
            return Plan("evidence_prerequisite_missing", evidence=evidence)
        continuation = _after_mandatory(job, artifacts, excluded, [op], remaining_units, remaining_ms, risk)
        if continuation.status not in ("ready", "complete"):
            return replace(continuation, evidence=evidence)
        return Plan("ready", (op.id,), op.cost_units, states_examined=continuation.states_examined,
                    evidence=evidence)
    return None


def _verification_choice(job, policy, artifacts, excluded, remaining_units, remaining_ms, risk):
    source = artifacts.get(policy["artifact"])
    stratum = source.get(policy["field"]) if isinstance(source, dict) else None
    model = policy["model"]
    decision = {
        "version": 1, "model_sha256": hashlib.sha256(canonical_json(model).encode()).hexdigest(),
        "artifact": policy["artifact"], "field": policy["field"],
        "remaining_ms": remaining_ms, "remaining_units": remaining_units,
        "min_on_time_correct": policy["min_on_time_correct"], "candidates": {},
    }
    if policy["artifact"] in artifacts:
        decision["artifact_sha256"] = hashlib.sha256(canonical_json(source).encode()).hexdigest()
    # This model is calibrated for complete routes at their initial decision, not conditional retries.
    if excluded.intersection(policy["route_ms"]):
        decision["selected_reason"] = "verification_route_failed"
        return Plan("verification_route_failed", verification=decision)
    eligible = []
    fallback = None
    reasons = set()
    for op in job.operations:
        if op.id not in policy["route_ms"]:
            continue
        duration = policy["route_ms"][op.id]
        row = {"quote_units": op.cost_units, "estimated_ms": duration}
        decision["candidates"][op.id] = row
        if op.id in excluded or not set(op.dependencies) <= artifacts.keys() or op.guard_state(artifacts) != "satisfied":
            reason = "unavailable"
        elif op.cost_units > remaining_units:
            reason = "over_budget"
        elif op.false_pass_risk > risk + 1e-12:
            reason = "risk_exceeded"
        elif max(duration, op.latency_p50_ms) > remaining_ms or remaining_ms == 0:
            reason = "deadline_unreachable"
        else:
            estimate = (estimate_action(model, stratum, op.id, remaining_ms=remaining_ms)
                        if isinstance(stratum, str) and stratum else {"status": "missing_stratum"})
            row.update(estimate)
            if estimate["status"] != "supported":
                reason = "insufficient_evidence"
                if op.id == policy["fallback_operation"]:
                    fallback = op
            elif estimate["quality_lower"] < policy["min_on_time_correct"]:
                reason = "quality_threshold_not_met"
            else:
                reason = "eligible"
                # Among routes meeting the floor, compare spend per on-time correct delivery.
                probability = estimate["quality"]
                ratio = estimate["mean_cost_units"] / probability if probability else None
                row["estimated_cost_per_on_time_correct"] = ratio
                eligible.append((ratio is None, ratio or 0, -estimate["quality_lower"], duration, op.id, op))
        row["eligibility"] = reason
        reasons.add(reason)
    if eligible:
        operation = min(eligible)[-1]
        decision["selected_reason"] = "lowest_estimated_cost_per_on_time_correct"
    elif fallback is not None:
        operation = fallback
        decision["selected_reason"] = "fixed_fallback_without_quality_estimate"
    else:
        # Mixed causes remain visible per action in the persisted decision.
        status = "deadline_expired" if remaining_ms == 0 else "no_eligible_verification_route"
        if len(reasons) == 1 and remaining_ms > 0:
            status = next(iter(reasons))
        decision["selected_reason"] = status
        return Plan(status, verification=decision)
    return Plan("ready", (operation.id,), operation.cost_units, verification=decision)


def decide(job: Job, artifacts: Mapping[str, Any], excluded: Collection[str], remaining_units: int,
           *, remaining_ms: int | None = None, elapsed_ms: int = 0, risk_remaining: float | None = None,
           anticipated: Collection[str] = ()) -> Plan:
    """Choose an action using observed values and separately identified in-flight names."""
    if not isinstance(artifacts, Mapping):
        raise ValueError("artifacts must map names to accepted JSON values")
    return plan(job, artifacts, excluded, remaining_units, remaining_ms=remaining_ms,
                elapsed_ms=elapsed_ms, risk_remaining=risk_remaining, anticipated=anticipated)


def plan(job: Job, available: Collection[str], excluded: Collection[str], remaining_units: int,
         *, remaining_ms: int | None = None, elapsed_ms: int = 0, risk_remaining: float | None = None,
         anticipated: Collection[str] = ()) -> Plan:
    """Plan and record the observed guards, including conditions that removed a route."""
    decision = _plan_snapshot(job, available, excluded, remaining_units, remaining_ms=remaining_ms,
                              elapsed_ms=elapsed_ms, risk_remaining=risk_remaining, anticipated=anticipated)
    guards = {}
    for op in job.operations:
        condition = op.when
        if condition is None:
            continue
        name = condition["artifact"]
        guard = {"artifact": name, "state": op.guard_state(available)}
        if name in available:
            guard["artifact_sha256"] = hashlib.sha256(canonical_json(available[name]).encode()).hexdigest()
        guards[op.id] = guard
    return replace(decision, audit={**(decision.audit or {}), "guards": guards}) if guards else decision


def _plan_snapshot(job: Job, available: Collection[str], excluded: Collection[str], remaining_units: int,
         *, remaining_ms: int | None = None, elapsed_ms: int = 0, risk_remaining: float | None = None,
         anticipated: Collection[str] = ()) -> Plan:
    if type(remaining_units) is not int or not 0 <= remaining_units <= job.budget_units:
        raise ValueError("remaining_units must be an integer within the job budget")
    if any(isinstance(names, (str, bytes)) for names in (available, excluded, anticipated)):
        raise ValueError("artifact and operation names must be collections")
    if type(elapsed_ms) is not int or elapsed_ms < 0:
        raise ValueError("elapsed_ms must be a nonnegative integer")
    risk = job.max_risk_alpha if risk_remaining is None else risk_remaining
    if type(risk) not in (float, int) or not math.isfinite(risk) or not 0 <= risk <= job.max_risk_alpha + 1e-12:
        raise ValueError("risk_remaining must be within the job risk budget")
    artifacts = available if isinstance(available, Mapping) else None
    verification = job.verification_policy
    guarded = any(op.when is not None for op in job.operations)
    if (job.evidence_rules or verification or guarded) and artifacts is None:
        raise ValueError("conditional decisions require an artifact mapping with accepted values")
    horizons = [ms for ms in (job.deadline_ms, verification["deadline_ms"] if verification else None)
                if ms is not None]
    horizon = min(horizons) if horizons else None
    if remaining_ms is not None and (type(remaining_ms) is not int or remaining_ms < 0
                                      or (horizon is not None and remaining_ms > horizon)):
        raise ValueError("remaining_ms must be inside the job deadline")
    if horizon is not None:
        if remaining_ms is None:
            remaining_ms = max(0, horizon - elapsed_ms)
        if job.deadline_ms is not None:
            elapsed_ms = max(elapsed_ms, job.deadline_ms - remaining_ms)
    available = set(available)
    anticipated = set(anticipated)
    excluded = set(excluded)
    declared = job.inputs.keys() | {op.produces for op in job.operations}
    declared.update(rule.request for rule in job.evidence_rules if rule.request is not None)
    if (available | anticipated) - declared or excluded - {op.id for op in job.operations}:
        raise ValueError("unknown available artifact or excluded operation")
    goals = set(job.goals)
    if goals <= available:
        return Plan("complete", (), 0, True)
    if remaining_ms == 0 and not verification:
        return Plan("deadline_expired")
    if job.evidence_rules and remaining_ms != 0:
        choice = _evidence_choice(job, artifacts, excluded, remaining_units, remaining_ms, risk)
        if choice is not None:
            return choice
    if verification:
        return _verification_choice(job, verification, artifacts, excluded, remaining_units, remaining_ms, risk)
    if not job.evidence_rules:
        available.update(anticipated)
    if goals <= available:
        return Plan("awaiting_receipt")
    if job.planning_policy == "bsao":
        bsao = BSAOPlanner(job).plan(
            artifacts if guarded else available,
            excluded,
            remaining_units,
            risk_remaining=risk_remaining,
            elapsed_ms=elapsed_ms,
            anticipated=anticipated if guarded and not job.evidence_rules else (),
        )
        return Plan(
            status=bsao.status,
            operations=bsao.operations,
            cost_units=bsao.cost_units,
            optimal=bsao.optimal,
            states_examined=bsao.states_examined,
            audit=bsao.audit.to_dict() if bsao.audit is not None else None,
        )
    if guarded:
        excluded.update(op.id for op in job.operations if op.guard_state(artifacts) == "falsified")
    operations = _relevant(job.operations, goals, available, excluded)
    cheapest = _resource_plan(operations, available, goals, remaining_units, job.search_limit, remaining_ms, risk)
    if guarded and any(op.guard_state(artifacts) == "pending" for op in operations):
        cheapest = replace(cheapest, optimal=False)
    if guarded and anticipated and cheapest.operations:
        primary = next(op for op in operations if op.id == cheapest.operations[0])
        if primary.guard_state(artifacts) != "satisfied":
            return Plan("awaiting_receipt")
    if job.planning_policy != "preserve_fallback" or cheapest.status != "ready":
        return cheapest
    first = next(op for op in operations if op.id == cheapest.operations[0])
    if first.route_role != "speculative" or first.cost_units == 0:
        return cheapest

    # A rejection yields no artifact. Charging the full quote models the largest
    # permitted loss; a real receipt can leave more budget on the next decision.
    remaining_if_rejected = remaining_units - first.cost_units
    fallback_ops = [op for op in operations if op.route_role == "fallback"]
    fallback = _resource_plan(fallback_ops, available, goals, remaining_units, job.search_limit, remaining_ms, risk)
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
    names = sorted({name for op in operations for name in (*op.dependencies, op.produces)} - available)
    bits = {name: 1 << index for index, name in enumerate(names)}
    goal_mask = sum(bits[name] for name in goals - available)
    goal_floor = {}
    edges = []
    for op in operations:
        output = bits[op.produces]
        required = sum(bits[name] for name in op.dependencies if name not in available)
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
