"""Budgeted Stochastic AND/OR Hypergraph (BSAO*) solver."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .contracts import Job, Operation


@dataclass(frozen=True, slots=True)
class DecisionAlternative:
    op_id: str
    produces: str
    quote_units: int
    expected_units: float
    pass_prob: float
    false_pass_risk: float
    expected_total_loss: float | None
    delta_vs_best: float | None
    admissible: bool
    reason: str


@dataclass(frozen=True, slots=True)
class DecisionAudit:
    selected_op: str | None
    action: str
    expected_total_loss: float
    abstain_loss: float
    alternatives: tuple[DecisionAlternative, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_op": self.selected_op,
            "action": self.action,
            "expected_total_loss": round(self.expected_total_loss, 6) if math.isfinite(self.expected_total_loss) else None,
            "abstain_loss": round(self.abstain_loss, 6) if math.isfinite(self.abstain_loss) else None,
            "alternatives": [
                {
                    **asdict(item),
                    "expected_total_loss": (
                        round(item.expected_total_loss, 6)
                        if item.expected_total_loss is not None and math.isfinite(item.expected_total_loss)
                        else None
                    ),
                    "delta_vs_best": (
                        round(item.delta_vs_best, 6)
                        if item.delta_vs_best is not None and math.isfinite(item.delta_vs_best)
                        else None
                    ),
                }
                for item in self.alternatives
            ],
        }


@dataclass(frozen=True, slots=True)
class BSAOResult:
    status: str
    operations: tuple[str, ...] = ()
    cost_units: int | None = None
    optimal: bool = True
    states_examined: int = 0
    expected_loss: float = 0.0
    audit: DecisionAudit | None = None


@dataclass(frozen=True, slots=True)
class ParallelWavePlan:
    primary_op: str
    operations: tuple[str, ...]
    reason: str
    hedged: bool = False


def select_parallel_wave(
    job: Job,
    primary_op_id: str,
    pass_sequence: tuple[str, ...],
    available: Collection[str],
    exhausted: Collection[str],
    in_flight_ops: Collection[str],
    in_flight_outputs: Collection[str],
    remaining_units: int,
    risk_remaining: float,
    elapsed_ms: int = 0,
) -> ParallelWavePlan:
    """Select an option-value-optimal parallel dispatch wave around primary_op_id."""
    if any(op._guard is not None for op in job.operations) and not isinstance(available, Mapping):
        raise ValueError("conditional operations require accepted values")
    ops_by_id = {op.id: op for op in job.operations}
    primary = ops_by_id[primary_op_id]
    if primary.guard_state(available) != "satisfied":
        return ParallelWavePlan(primary.id, (), "guard_unavailable")
    if job.evidence_rules or job.verification_policy or job.max_parallel <= 1 or job.planning_policy != "bsao":
        return ParallelWavePlan(primary.id, (primary.id,), "serial_policy")

    avail = set(available)
    exh = set(exhausted) | set(in_flight_ops)
    produced = set(in_flight_outputs) | {primary.produces}
    wave: list[Operation] = [primary]
    rem_budget = remaining_units - primary.cost_units
    rem_risk = risk_remaining - primary.false_pass_risk
    failure_pen = (
        float(job.failure_penalty_units)
        if job.failure_penalty_units is not None
        else float(max(1_000_000, job.budget_units * 100 + 10_000))
    )

    # 1. Option-Value AND-Sibling Parallelism
    # Consider ready siblings that appear on the optimal pass continuation (or remaining rel_ops).
    rel_ops = relevant_operations(job.operations, frozenset(job.goals), frozenset(avail), frozenset(exh))
    seq_rank = {op_id: idx for idx, op_id in enumerate(pass_sequence)}
    ready_siblings = [
        op for op in rel_ops
        if op.id != primary.id
        and op.id in seq_rank
        and op.id not in exh
        and op.produces not in avail
        and op.produces not in produced
        and op.guard_state(available) == "satisfied"
        and avail.issuperset(op.dependencies)
        and op.cost_units <= rem_budget
        and op.false_pass_risk <= rem_risk + 1e-12
        and (job.deadline_ms is None or elapsed_ms + op.latency_p50_ms <= job.deadline_ms)
    ]
    ready_siblings.sort(key=lambda o: (o.id not in seq_rank, seq_rank.get(o.id, 9999), o.cost_units, o.id))

    reasons: list[str] = []
    for sib in ready_siblings:
        if len(wave) >= job.max_parallel:
            break
        if sib.cost_units > rem_budget or sib.false_pass_risk > rem_risk + 1e-12:
            continue
        latency_gain = job.latency_weight_units_per_sec * (min(primary.latency_p50_ms, sib.latency_p50_ms) / 1000.0)
        option_loss = (1.0 - primary.pass_prob) * sib.effective_expected_units
        deadline_pressure = (
            job.deadline_ms is not None
            and elapsed_ms + primary.latency_p50_ms + sib.latency_p50_ms > job.deadline_ms
        )
        if latency_gain > option_loss + 1e-9 or deadline_pressure:
            wave.append(sib)
            produced.add(sib.produces)
            rem_budget -= sib.cost_units
            rem_risk -= sib.false_pass_risk
            reasons.append("deadline_parallel" if deadline_pressure else "option_value_parallel")

    # 2. OR Deadline Hedging when serial retry would breach hard deadline
    hedged = False
    if (
        len(wave) < job.max_parallel
        and job.deadline_ms is not None
        and primary.pass_prob < 1.0
    ):
        backups = [
            op for op in rel_ops
            if op.id != primary.id
            and op.id not in exh
            and op.produces == primary.produces
            and op.guard_state(available) == "satisfied"
            and avail.issuperset(op.dependencies)
            and op.cost_units <= rem_budget
            and op.false_pass_risk <= rem_risk + 1e-12
            and elapsed_ms + op.latency_p95_ms <= job.deadline_ms
        ]
        backups.sort(key=lambda o: (-o.pass_prob, o.effective_expected_units, o.id))
        for backup in backups:
            serial_breach = elapsed_ms + primary.latency_p95_ms + backup.latency_p95_ms > job.deadline_ms
            if serial_breach and (1.0 - primary.pass_prob) * failure_pen > backup.effective_expected_units:
                wave.append(backup)
                rem_budget -= backup.cost_units
                rem_risk -= backup.false_pass_risk
                hedged = True
                reasons.append("or_deadline_hedge")
                break

    reason = ",".join(dict.fromkeys(reasons)) if reasons else "option_value_serialize"
    return ParallelWavePlan(primary.id, tuple(op.id for op in wave), reason, hedged=hedged)


def relevant_operations(
    operations: tuple[Operation, ...],
    goals: frozenset[str],
    available: frozenset[str],
    exhausted: frozenset[str],
) -> tuple[Operation, ...]:
    """Prune backward from missing goals, then forward-close from available artifacts."""
    producers: dict[str, list[Operation]] = defaultdict(list)
    for op in operations:
        if op.id not in exhausted and op.produces not in available:
            producers[op.produces].append(op)

    pending = list(goals - available)
    visited = set(available)
    backward: list[Operation] = []
    while pending:
        artifact = pending.pop()
        if artifact in visited:
            continue
        visited.add(artifact)
        for op in producers.get(artifact, ()):
            backward.append(op)
            for req in op.dependencies:
                if req not in available:
                    pending.append(req)

    reachable = set(available)
    while True:
        added = False
        for op in backward:
            if op.produces not in reachable and reachable.issuperset(op.dependencies):
                reachable.add(op.produces)
                added = True
        if not added:
            break

    if not goals.issubset(reachable):
        return ()
    return tuple(sorted((op for op in backward if reachable.issuperset(op.dependencies)), key=lambda op: op.id))


class BSAOPlanner:
    """Exact memoized Bellman solver over canonical stochastic AND/OR hypergraph states."""

    def __init__(self, job: Job, *, risk_bins: int = 50):
        self.job = job
        self.ops_by_id = {op.id: op for op in job.operations}
        self.goals = frozenset(job.goals)
        if type(risk_bins) is not int or risk_bins < 1:
            raise ValueError("risk_bins must be a positive integer")
        self.risk_bins = risk_bins if any(op.false_pass_risk for op in job.operations) else 0
        if job.failure_penalty_units is not None:
            self.terminal_failure_penalty = float(job.failure_penalty_units)
        else:
            scale = max(1_000_000, (job.budget_units + job.corruption_penalty_units) * 10_000 + 100_000)
            self.terminal_failure_penalty = float(scale)

        # Bitmask indexing for fast recursive state representation.
        self._ops = job.operations
        self._n_ops = len(self._ops)
        self._op_index = {op.id: i for i, op in enumerate(self._ops)}
        art_names = sorted({a for op in self._ops for a in (*op.dependencies, op.produces)} | self.goals)
        self._art_bit = {name: 1 << i for i, name in enumerate(art_names)}
        self._goal_mask = sum(self._art_bit[g] for g in self.goals)
        self._op_req = [sum(self._art_bit[r] for r in op.dependencies) for op in self._ops]
        self._op_prod = [self._art_bit[op.produces] for op in self._ops]
        self._op_step_cost = [self._step_cost(op) for op in self._ops]
        self._op_priority = [
            (self._op_step_cost[i] / op.pass_prob, op.cost_units, op.id)
            for i, op in enumerate(self._ops)
        ]

        # Precompute per-artifact consumer/producer masks for fast pruning.
        self._prod_ops_of_art: dict[int, list[int]] = defaultdict(list)
        for i, op in enumerate(self._ops):
            self._prod_ops_of_art[self._op_prod[i]].append(i)

        self._memo: dict[tuple[int, int, int, int, int], tuple[float, int | None]] = {}
        self._states_examined = 0
        self._capped = False

    def _step_cost(self, op: Operation) -> float:
        return (
            op.effective_expected_units
            + self.job.latency_weight_units_per_sec * (op.latency_p50_ms / 1000.0)
            + op.false_pass_risk * self.job.corruption_penalty_units
        )

    def _quantize_risk(self, risk_remaining: float) -> int:
        if self.risk_bins <= 0 or self.job.max_risk_alpha <= 0.0:
            return 0
        ratio = max(0.0, min(1.0, risk_remaining / self.job.max_risk_alpha))
        return math.floor(ratio * self.risk_bins + 1e-9)

    def _effective_risk(self, r_bin: int) -> float:
        if self.risk_bins <= 0:
            return 1.0
        if self.job.max_risk_alpha <= 0.0:
            return 0.0
        return (r_bin / self.risk_bins) * self.job.max_risk_alpha

    def _prune_masks(self, avail_mask: int, exh_mask: int) -> tuple[int, int]:
        """Return (relevant_op_mask, needed_input_art_mask) or (0, 0) if goals unreachable."""
        missing_goals = self._goal_mask & ~avail_mask
        if not missing_goals:
            return 0, 0

        # Backward pass over unproduced artifacts from missing goals.
        needed_arts = missing_goals
        visited_arts = avail_mask
        backward_mask = 0
        prod_map = self._prod_ops_of_art
        op_req = self._op_req

        frontier = missing_goals
        while frontier:
            lsb = frontier & -frontier
            frontier ^= lsb
            if visited_arts & lsb:
                continue
            visited_arts |= lsb
            for i in prod_map.get(lsb, ()):
                if not (exh_mask & (1 << i)):
                    backward_mask |= 1 << i
                    req_m = op_req[i] & ~visited_arts
                    frontier |= req_m

        if not backward_mask:
            return 0, 0

        # Forward closure over backward_mask ops.
        reachable = avail_mask
        candidates = backward_mask
        op_prod = self._op_prod
        while candidates:
            added = False
            rem = candidates
            while rem:
                bit = rem & -rem
                rem ^= bit
                idx = bit.bit_length() - 1
                if (op_req[idx] & ~reachable) == 0:
                    reachable |= op_prod[idx]
                    candidates ^= bit
                    added = True
            if not added:
                break

        if (self._goal_mask & ~reachable) != 0:
            return 0, 0

        rel_mask = backward_mask & ~candidates
        needed_inputs = self._goal_mask
        rem = rel_mask
        while rem:
            bit = rem & -rem
            rem ^= bit
            needed_inputs |= op_req[bit.bit_length() - 1]
        return rel_mask, avail_mask & needed_inputs

    def _greedy_rollout(
        self,
        avail_mask: int,
        exh_mask: int,
        budget: int,
        risk_remaining: float,
        elapsed_ms: int,
        rel_mask: int,
    ) -> tuple[float, int | None]:
        curr_avail = avail_mask
        curr_exh = exh_mask
        rem_b = budget
        rem_r = risk_remaining
        curr_t = elapsed_ms
        total_loss = 0.0
        first_idx: int | None = None
        ops = self._ops
        op_req = self._op_req
        op_prod = self._op_prod
        step_costs = self._op_step_cost
        deadline = self.job.deadline_ms

        while (self._goal_mask & ~curr_avail) != 0:
            best_idx: int | None = None
            best_key = (math.inf, 0, "")
            rem = rel_mask & ~curr_exh
            while rem:
                bit = rem & -rem
                rem ^= bit
                i = bit.bit_length() - 1
                if (op_prod[i] & curr_avail) or (op_req[i] & ~curr_avail):
                    continue
                op = ops[i]
                if op.cost_units > rem_b or op.false_pass_risk > rem_r + 1e-12:
                    continue
                if deadline is not None and curr_t + op.latency_p50_ms > deadline:
                    continue
                key = self._op_priority[i]
                if key < best_key:
                    best_key = key
                    best_idx = i
            if best_idx is None:
                return self.terminal_failure_penalty, first_idx
            if first_idx is None:
                first_idx = best_idx
            chosen = ops[best_idx]
            total_loss += step_costs[best_idx]
            rem_b -= chosen.cost_units
            rem_r -= chosen.false_pass_risk
            curr_t += chosen.latency_p50_ms
            curr_exh |= 1 << best_idx
            curr_avail |= op_prod[best_idx]
        return min(self.terminal_failure_penalty, total_loss), first_idx

    def _solve(
        self,
        avail_mask: int,
        exh_mask: int,
        budget: int,
        r_bin: int,
        elapsed_ms: int,
        *,
        is_root: bool = False,
    ) -> tuple[float, int | None]:
        if (self._goal_mask & ~avail_mask) == 0:
            return 0.0, None
        if budget < 0:
            return self.terminal_failure_penalty, None

        rel_mask, canonical_avail = self._prune_masks(avail_mask, exh_mask)
        if rel_mask == 0:
            return self.terminal_failure_penalty, None

        # Clamp budget above total remaining quote of relevant operations.
        total_rel_cost = 0
        rem_c = rel_mask
        while rem_c:
            bit = rem_c & -rem_c
            rem_c ^= bit
            total_rel_cost += self._ops[bit.bit_length() - 1].cost_units
        key_budget = budget if budget < total_rel_cost else total_rel_cost

        state_key = (canonical_avail, rel_mask, key_budget, r_bin, elapsed_ms if self.job.deadline_ms is not None else 0)
        if not is_root:
            cached = self._memo.get(state_key)
            if cached is not None:
                return cached

        effective_risk = self._effective_risk(r_bin)
        state_cap = self.job.search_limit if self._n_ops < 18 else min(self.job.search_limit, 1200)
        if not is_root and (self._capped or self._states_examined >= state_cap):
            if not self._capped:
                self._capped = True
                return self._greedy_rollout(avail_mask, exh_mask, budget, effective_risk, elapsed_ms, rel_mask)
            return self._greedy_rollout(avail_mask, exh_mask, budget, effective_risk, elapsed_ms, rel_mask)

        self._states_examined += 1

        allow_abstain = self.job.failure_penalty_units is not None
        best_loss = self.terminal_failure_penalty if (allow_abstain or not is_root) else math.inf
        best_idx: int | None = None

        ops = self._ops
        op_req = self._op_req
        op_prod = self._op_prod
        step_costs = self._op_step_cost
        deadline = self.job.deadline_ms

        ready: list[int] = []
        rem = rel_mask
        while rem:
            bit = rem & -rem
            rem ^= bit
            i = bit.bit_length() - 1
            if (op_req[i] & ~avail_mask) != 0:
                continue
            op = ops[i]
            if op.cost_units <= budget and op.false_pass_risk <= effective_risk + 1e-12:
                if deadline is None or elapsed_ms + op.latency_p50_ms <= deadline:
                    ready.append(i)
        ready.sort(key=self._op_priority.__getitem__)

        for i in ready:
            if self._capped:
                break
            step_cost = step_costs[i]
            if step_cost >= best_loss:
                continue
            op = ops[i]
            next_exh = exh_mask | (1 << i)
            next_budget = budget - op.cost_units
            next_r_bin = self._quantize_risk(effective_risk - op.false_pass_risk)

            loss_pass, _ = self._solve(
                avail_mask | op_prod[i],
                next_exh,
                next_budget,
                next_r_bin,
                elapsed_ms + op.latency_p50_ms,
            )
            if step_cost + op.pass_prob * loss_pass >= best_loss:
                continue

            loss_fail, _ = self._solve(
                avail_mask,
                next_exh,
                next_budget,
                r_bin,
                elapsed_ms + op.latency_p50_ms,
            )

            q_val = step_cost + op.pass_prob * loss_pass + (1.0 - op.pass_prob) * loss_fail
            if q_val < best_loss - 1e-9:
                best_loss = q_val
                best_idx = i

        if is_root and best_idx is None and not allow_abstain:
            best_loss = self.terminal_failure_penalty

        if not self._capped:
            self._memo[state_key] = (best_loss, best_idx)
        return best_loss, best_idx

    def _build_audit(
        self,
        avail_mask: int,
        exh_mask: int,
        budget: int,
        r_bin: int,
        elapsed_ms: int,
        selected_idx: int | None,
        best_loss: float,
    ) -> DecisionAudit:
        rel_mask, _ = self._prune_masks(avail_mask, exh_mask)
        effective_risk = self._effective_risk(r_bin)
        alternatives: list[DecisionAlternative] = []
        abstain_loss = (
            float(self.job.failure_penalty_units)
            if self.job.failure_penalty_units is not None
            else math.inf
        )
        selected_op = self._ops[selected_idx].id if selected_idx is not None else None

        rem = rel_mask
        while rem:
            bit = rem & -rem
            rem ^= bit
            i = bit.bit_length() - 1
            if (self._op_req[i] & ~avail_mask) != 0:
                continue
            op = self._ops[i]
            if op.cost_units > budget:
                alternatives.append(DecisionAlternative(
                    op.id, op.produces, op.cost_units, op.effective_expected_units,
                    op.pass_prob, op.false_pass_risk, None, None, False, "over_budget",
                ))
                continue
            if op.false_pass_risk > effective_risk + 1e-12:
                alternatives.append(DecisionAlternative(
                    op.id, op.produces, op.cost_units, op.effective_expected_units,
                    op.pass_prob, op.false_pass_risk, None, None, False, "risk_exceeded",
                ))
                continue
            if self.job.deadline_ms is not None and elapsed_ms + op.latency_p50_ms > self.job.deadline_ms:
                alternatives.append(DecisionAlternative(
                    op.id, op.produces, op.cost_units, op.effective_expected_units,
                    op.pass_prob, op.false_pass_risk, None, None, False, "deadline_exceeded",
                ))
                continue

            step_cost = self._op_step_cost[i]
            next_exh = exh_mask | (1 << i)
            next_budget = budget - op.cost_units
            next_r_bin = self._quantize_risk(effective_risk - op.false_pass_risk)
            loss_pass, _ = self._solve(
                avail_mask | self._op_prod[i], next_exh, next_budget, next_r_bin, elapsed_ms + op.latency_p50_ms,
            )
            loss_fail, _ = self._solve(
                avail_mask, next_exh, next_budget, r_bin, elapsed_ms + op.latency_p50_ms,
            )
            q_val = step_cost + op.pass_prob * loss_pass + (1.0 - op.pass_prob) * loss_fail
            if self.job.failure_penalty_units is None and q_val >= self.terminal_failure_penalty - 1e-6:
                reason = "unreachable_within_constraints"
            elif q_val >= abstain_loss - 1e-9:
                reason = "negative_net_value"
            elif op.id == selected_op:
                reason = "selected"
            else:
                reason = "higher_expected_loss"
            delta = (q_val - best_loss) if math.isfinite(best_loss) else None
            alternatives.append(DecisionAlternative(
                op.id, op.produces, op.cost_units, op.effective_expected_units,
                op.pass_prob, op.false_pass_risk, q_val, delta, True, reason,
            ))

        alternatives.sort(key=lambda a: (
            not a.admissible,
            a.expected_total_loss if a.expected_total_loss is not None else math.inf,
            a.op_id,
        ))
        action = "execute" if selected_op is not None else ("abstain" if self.job.failure_penalty_units is not None else "blocked")
        return DecisionAudit(
            selected_op=selected_op,
            action=action,
            expected_total_loss=best_loss,
            abstain_loss=abstain_loss,
            alternatives=tuple(alternatives[:5]),
        )

    def _trace_pass_sequence(
        self,
        first_idx: int,
        avail_mask: int,
        exh_mask: int,
        budget: int,
        r_bin: int,
        elapsed_ms: int,
    ) -> tuple[tuple[str, ...], int]:
        seq: list[str] = []
        total_quote = 0
        curr_avail = avail_mask
        curr_exh = exh_mask
        rem_budget = budget
        curr_r_bin = r_bin
        curr_elapsed = elapsed_ms
        next_idx: int | None = first_idx

        while next_idx is not None and not (curr_exh & (1 << next_idx)) and (self._goal_mask & ~curr_avail) != 0:
            op = self._ops[next_idx]
            if op.cost_units > rem_budget or (self._op_req[next_idx] & ~curr_avail) != 0:
                break
            seq.append(op.id)
            total_quote += op.cost_units
            curr_avail |= self._op_prod[next_idx]
            curr_exh |= 1 << next_idx
            rem_budget -= op.cost_units
            curr_r_bin = self._quantize_risk(self._effective_risk(curr_r_bin) - op.false_pass_risk)
            curr_elapsed += op.latency_p50_ms
            if (self._goal_mask & ~curr_avail) == 0:
                break
            _, next_idx = self._solve(curr_avail, curr_exh, rem_budget, curr_r_bin, curr_elapsed)
        return tuple(seq), total_quote

    def _risk_feasible(self, rel_mask: int, avail_mask: int, r_bin: int) -> bool | None:
        memo: dict[tuple[int, int], bool] = {}

        def dfs(a_mask: int, rb: int) -> bool | None:
            if (self._goal_mask & ~a_mask) == 0:
                return True
            key = (a_mask, rb)
            if key in memo:
                return memo[key]
            if len(memo) >= self.job.search_limit:
                return None
            memo[key] = False
            eff_r = self._effective_risk(rb)
            rem = rel_mask
            while rem:
                bit = rem & -rem
                rem ^= bit
                i = bit.bit_length() - 1
                if not (self._op_prod[i] & a_mask) and (self._op_req[i] & ~a_mask) == 0:
                    fpr = self._ops[i].false_pass_risk
                    if fpr <= eff_r + 1e-12:
                        feasible = dfs(a_mask | self._op_prod[i], self._quantize_risk(eff_r - fpr))
                        if feasible is None:
                            return None
                        if feasible:
                            memo[key] = True
                            return True
            return False

        return dfs(avail_mask, r_bin)

    def plan(
        self,
        available: Collection[str],
        excluded: Collection[str],
        remaining_units: int,
        *,
        risk_remaining: float | None = None,
        elapsed_ms: int = 0,
        anticipated: Collection[str] = (),
    ) -> BSAOResult:
        guarded = [op for op in self._ops if op._guard is not None]
        pending = False
        if guarded:
            if not isinstance(available, Mapping):
                raise ValueError("conditional operations require accepted values")
            excluded = set(excluded)
            for op in guarded:
                state = op.guard_state(available)
                if state == "falsified":
                    excluded.add(op.id)
                elif state == "pending" and op.id not in excluded:
                    pending = True
        names = set(available) | set(anticipated)
        avail_mask = sum(self._art_bit[name] for name in names if name in self._art_bit)
        exh_mask = sum(1 << self._op_index[op_id] for op_id in excluded if op_id in self._op_index)

        if (self._goal_mask & ~avail_mask) == 0:
            status = "complete" if self.goals.issubset(available) else "awaiting_receipt"
            return BSAOResult(status=status, operations=(), cost_units=0, optimal=status == "complete", states_examined=0)

        risk_val = self.job.max_risk_alpha if risk_remaining is None else max(0.0, float(risk_remaining))
        r_bin = self._quantize_risk(risk_val)

        rel_mask, _ = self._prune_masks(avail_mask, exh_mask)
        if rel_mask == 0:
            return BSAOResult(
                status="unreachable", optimal=True, states_examined=0,
                expected_loss=self.terminal_failure_penalty,
            )

        # A non-aligned risk bin rounds capacity down and may exclude feasible competitors.
        scale = self.risk_bins / self.job.max_risk_alpha if self.job.max_risk_alpha else 0
        approximate_risk = any(not math.isclose(value * scale, round(value * scale), abs_tol=1e-9)
                               for value in (risk_val, *(op.false_pass_risk for op in self._ops)))
        self._memo.clear()
        self._states_examined = 0
        self._capped = False

        best_loss, best_idx = self._solve(avail_mask, exh_mask, remaining_units, r_bin, elapsed_ms, is_root=True)
        audit = self._build_audit(avail_mask, exh_mask, remaining_units, r_bin, elapsed_ms, best_idx, best_loss)

        if best_idx is not None:
            if anticipated and self._ops[best_idx].guard_state(available) != "satisfied":
                return BSAOResult(status="awaiting_receipt", optimal=False, states_examined=self._states_examined)
            ops_seq, total_quote = self._trace_pass_sequence(
                best_idx, avail_mask, exh_mask, remaining_units, r_bin, elapsed_ms,
            )
            if not ops_seq:
                op = self._ops[best_idx]
                ops_seq = (op.id,)
                total_quote = op.cost_units
            return BSAOResult(
                status="ready",
                operations=ops_seq,
                cost_units=total_quote,
                optimal=not pending and not approximate_risk and not self._capped,
                states_examined=self._states_examined,
                expected_loss=best_loss,
                audit=audit,
            )

        has_feasible_path = any(
            alt.admissible and alt.reason == "negative_net_value"
            for alt in audit.alternatives
        )
        if self.job.failure_penalty_units is not None and has_feasible_path:
            return BSAOResult(
                status="abstain",
                optimal=not pending and not approximate_risk and not self._capped,
                states_examined=self._states_examined,
                expected_loss=best_loss,
                audit=audit,
            )

        if self._capped:
            status = "search_limit"
        else:
            risk_feasible = self._risk_feasible(rel_mask, avail_mask, r_bin)
            if risk_feasible is None:
                status = "search_limit"
            elif not risk_feasible:
                status = "risk_exceeded"
            elif audit.alternatives and all(alt.reason == "deadline_exceeded" for alt in audit.alternatives):
                status = "deadline_unreachable"
            else:
                status = "over_budget"

        return BSAOResult(
            status=status,
            optimal=not pending and not approximate_risk and not self._capped if status != "search_limit" else False,
            states_examined=self._states_examined,
            expected_loss=self.terminal_failure_penalty,
            audit=audit,
        )
