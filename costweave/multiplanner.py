"""Bounded, identity-blind MultiPlanner engine for CostWeave v0.5.3."""

from __future__ import annotations

import asyncio
import hashlib
from itertools import combinations
from typing import Any, Iterable, Mapping

from .domain import Plan, RunRequest
from .planner import TaskAnalysis
from .planning_contracts import (
    AdjudicationResult,
    AnonymousProposal,
    DeliberationReceipt,
    PlanProposal,
    PlanningDecision,
    PlanningMode,
    ProposalConflict,
    ProposalScorecard,
)


SCORE_DIMENSIONS = (
    "goal_alignment",
    "executability",
    "cost_efficiency",
    "latency_efficiency",
    "risk_control",
    "evidence_quality",
)


class MultiPlannerEngine:
    """Runs a bounded proposal panel without introducing another manager layer."""

    async def deliberate(
        self,
        decision: PlanningDecision,
        request: RunRequest,
        analysis: TaskAnalysis,
    ) -> tuple[list[PlanProposal], DeliberationReceipt]:
        roles = list(decision.panel_policy.selected_roles)
        budget = decision.planning_budget
        feasible_count = min(
            len(roles),
            max(0, budget.max_calls),
            decision.limits.max_planners_per_round,
        )
        selected_roles = roles[:feasible_count]
        degraded = feasible_count < len(roles)
        if not selected_roles:
            adjudication = AdjudicationResult(
                outcome="not_required",
                rounds_used=0,
                selected_aliases=(),
                rejected_aliases=(),
                rejection_reasons={},
                compiler_directives=(),
                reasons=("No planning call was permitted by the hard budget.",),
                confidence=decision.confidence,
            )
            return [], DeliberationReceipt(
                decision_id=decision.decision_id,
                mode=decision.mode,
                parallel=False,
                proposal_count=0,
                anonymous_proposals=(),
                scorecards=(),
                conflicts=(),
                adjudication=adjudication,
                actual_cost=0.0,
                actual_latency_ms=0,
                calls_used=0,
                budget_degraded=bool(roles),
                early_stopped=True,
                stop_reason="hard planning budget allowed no proposal calls",
            )

        per_cost = budget.max_cost / max(1, feasible_count)
        per_latency = budget.max_latency_ms
        assignments = list(decision.panel_policy.selected_assignments)
        jobs = [
            self._generate_proposal(
                decision,
                request,
                analysis,
                role_id,
                assignments[index] if index < len(assignments) else {},
                per_cost,
                per_latency,
                index,
            )
            for index, role_id in enumerate(selected_roles)
        ]
        proposals = list(await asyncio.gather(*jobs))
        anonymous = self.anonymize(proposals)
        scorecards = self.score(anonymous, request, analysis)
        conflicts = self.detect_conflicts(anonymous)
        adjudication, early_stopped = self.adjudicate(
            anonymous,
            scorecards,
            conflicts,
            degraded=degraded,
        )
        latency = max(
            (item.estimated_planning_latency_ms for item in proposals),
            default=0,
        )
        cost = sum(item.estimated_planning_cost for item in proposals)
        stop_reason = (
            "panel reduced to remain within call budget"
            if degraded
            else (
                "clear constraint-scored leader; adjudication skipped"
                if early_stopped
                else "one bounded adjudication round completed"
            )
        )
        receipt = DeliberationReceipt(
            decision_id=decision.decision_id,
            mode=decision.mode,
            parallel=len(proposals) > 1,
            proposal_count=len(proposals),
            anonymous_proposals=tuple(anonymous),
            scorecards=tuple(scorecards),
            conflicts=tuple(conflicts),
            adjudication=adjudication,
            actual_cost=min(cost, budget.max_cost),
            actual_latency_ms=min(latency, budget.max_latency_ms),
            calls_used=len(proposals),
            budget_degraded=degraded,
            early_stopped=early_stopped,
            stop_reason=stop_reason,
        )
        return proposals, receipt

    async def _generate_proposal(
        self,
        decision: PlanningDecision,
        request: RunRequest,
        analysis: TaskAnalysis,
        role_id: str,
        assignment: Mapping[str, Any],
        cost: float,
        latency_ms: int,
        index: int,
    ) -> PlanProposal:
        # Yield once so every role is scheduled as an independent async job.
        await asyncio.sleep(0)
        templates: dict[str, dict[str, Any]] = {
            "domain-planner": {
                "outline": (
                    "freeze objective, deliverables, and exclusions",
                    "build dependency-aware work packages",
                    "attach acceptance criteria to every work package",
                ),
                "risks": (
                    "domain boundary or user intent may be misinterpreted",
                ),
                "acceptance": (
                    "trace every task to a requested deliverable",
                    "validate every output contract before integration",
                ),
                "strategy": {
                    "parallelism": "balanced",
                    "budget_posture": "balanced",
                    "risk_posture": "contract-first",
                    "verification": "per-node",
                },
                "evidence": (
                    "request constraints",
                    "task taxonomy and dependency structure",
                ),
            },
            "economics-planner": {
                "outline": (
                    "identify the critical path and independent branches",
                    "maximize safe parallel waves",
                    "place cost and latency stop lines before execution",
                ),
                "risks": (
                    "coordination overhead may erase parallelism gains",
                ),
                "acceptance": (
                    "compare forecast cost with the hard task budget",
                    "verify p95 latency against the deadline",
                ),
                "strategy": {
                    "parallelism": "aggressive",
                    "budget_posture": "lean",
                    "risk_posture": "expected-loss",
                    "verification": "critical-path",
                },
                "evidence": (
                    "planning and execution budget",
                    "critical-path latency estimate",
                ),
            },
            "risk-planner": {
                "outline": (
                    "mark high-loss and irreversible nodes",
                    "require independent evidence at critical gates",
                    "define local rollback and escalation boundaries",
                ),
                "risks": (
                    "low-confidence output may be accepted by downstream nodes",
                ),
                "acceptance": (
                    "independently review critical conclusions",
                    "limit repair scope and preserve rollback evidence",
                ),
                "strategy": {
                    "parallelism": "conservative",
                    "budget_posture": "reserve",
                    "risk_posture": "minimax",
                    "verification": "independent",
                },
                "evidence": (
                    "risk signals and irreversibility estimate",
                    "quality floor and failure-loss estimate",
                ),
            },
        }
        template = templates.get(role_id, templates["domain-planner"])
        hard_constraints = tuple(analysis.constraints) + (
            f"planning cost must not exceed {decision.planning_budget.max_cost:.6f}",
            f"planning calls must not exceed {decision.planning_budget.max_calls}",
            "management depth must remain two",
        )
        key = f"{decision.decision_id}:{role_id}:{request.goal}"
        estimated_latency = min(
            latency_ms,
            max(1, 700 + analysis.difficulty * 115 + index * 37),
        )
        return PlanProposal(
            proposal_id="proposal_" + hashlib.sha256(
                key.encode("utf-8")
            ).hexdigest()[:24],
            planner_role=role_id,
            objective_interpretation=request.goal,
            decomposition_outline=template["outline"],
            assumptions=tuple(analysis.constraints) or (
                "no additional user constraint beyond the run contract",
            ),
            risks=template["risks"],
            acceptance_strategy=template["acceptance"],
            estimated_planning_cost=cost,
            estimated_planning_latency_ms=estimated_latency,
            confidence=max(
                0.4,
                min(0.96, analysis.estimate_confidence - index * 0.015),
            ),
            assignment=assignment,
            hard_constraints=hard_constraints,
            strategy=template["strategy"],
            evidence=template["evidence"],
        )

    @staticmethod
    def anonymize(proposals: Iterable[PlanProposal]) -> list[AnonymousProposal]:
        result: list[AnonymousProposal] = []
        for index, proposal in enumerate(proposals):
            result.append(AnonymousProposal(
                alias=chr(ord("A") + index),
                objective_interpretation=proposal.objective_interpretation,
                decomposition_outline=proposal.decomposition_outline,
                assumptions=proposal.assumptions,
                risks=proposal.risks,
                acceptance_strategy=proposal.acceptance_strategy,
                hard_constraints=proposal.hard_constraints,
                strategy=proposal.strategy,
                evidence=proposal.evidence,
                confidence=proposal.confidence,
            ))
        return result

    def score(
        self,
        proposals: Iterable[AnonymousProposal],
        request: RunRequest,
        analysis: TaskAnalysis,
    ) -> list[ProposalScorecard]:
        weights = {
            "goal_alignment": 0.22,
            "executability": 0.20,
            "cost_efficiency": 0.15,
            "latency_efficiency": 0.12,
            "risk_control": 0.18,
            "evidence_quality": 0.13,
        }
        if analysis.stakes == "high":
            weights["risk_control"] += 0.08
            weights["cost_efficiency"] -= 0.04
            weights["latency_efficiency"] -= 0.04
        cards: list[ProposalScorecard] = []
        for proposal in proposals:
            strategy = dict(proposal.strategy)
            acceptance_count = len(proposal.acceptance_strategy)
            evidence_count = len(proposal.evidence)
            risk_posture = str(strategy.get("risk_posture", ""))
            budget_posture = str(strategy.get("budget_posture", ""))
            parallelism = str(strategy.get("parallelism", ""))
            scores = {
                "goal_alignment": min(
                    1.0,
                    0.62 + 0.06 * len(proposal.decomposition_outline),
                ),
                "executability": min(1.0, 0.55 + 0.08 * acceptance_count),
                "cost_efficiency": {
                    "lean": 0.94,
                    "balanced": 0.80,
                    "reserve": 0.67,
                }.get(budget_posture, 0.6),
                "latency_efficiency": {
                    "aggressive": 0.94,
                    "balanced": 0.80,
                    "conservative": 0.65,
                }.get(parallelism, 0.6),
                "risk_control": {
                    "minimax": 0.97,
                    "contract-first": 0.86,
                    "expected-loss": 0.78,
                }.get(risk_posture, 0.6),
                "evidence_quality": min(1.0, 0.55 + evidence_count * 0.14),
            }
            failed = tuple(
                constraint
                for constraint in proposal.hard_constraints
                if not str(constraint).strip()
            )
            hard_pass = not failed and bool(proposal.acceptance_strategy)
            total = sum(scores[key] * weights[key] for key in SCORE_DIMENSIONS)
            if not hard_pass:
                total *= 0.25
            cards.append(ProposalScorecard(
                alias=proposal.alias,
                scores=scores,
                total=max(0.0, min(1.0, total)),
                hard_constraints_passed=hard_pass,
                failed_constraints=failed,
                reasons=(
                    "scored against frozen constraints without planner identity",
                    f"quality floor={request.quality_floor:.2f}; stakes={analysis.stakes}",
                ),
            ))
        return sorted(cards, key=lambda item: (-item.total, item.alias))

    @staticmethod
    def detect_conflicts(
        proposals: Iterable[AnonymousProposal],
    ) -> list[ProposalConflict]:
        result: list[ProposalConflict] = []
        for left, right in combinations(proposals, 2):
            left_parallel = str(left.strategy.get("parallelism", ""))
            right_parallel = str(right.strategy.get("parallelism", ""))
            if {left_parallel, right_parallel} == {"aggressive", "conservative"}:
                key = f"parallelism:{left.alias}:{right.alias}"
                result.append(ProposalConflict(
                    conflict_id="conflict_" + hashlib.sha256(
                        key.encode("utf-8")
                    ).hexdigest()[:18],
                    conflict_type="parallelism_vs_control",
                    severity="high",
                    aliases=(left.alias, right.alias),
                    evidence=(
                        f"{left.alias} proposes {left_parallel} parallelism",
                        f"{right.alias} proposes {right_parallel} parallelism",
                    ),
                    resolution_required=True,
                ))
            left_budget = str(left.strategy.get("budget_posture", ""))
            right_budget = str(right.strategy.get("budget_posture", ""))
            if {left_budget, right_budget} == {"lean", "reserve"}:
                key = f"budget:{left.alias}:{right.alias}"
                result.append(ProposalConflict(
                    conflict_id="conflict_" + hashlib.sha256(
                        key.encode("utf-8")
                    ).hexdigest()[:18],
                    conflict_type="budget_reserve",
                    severity="medium",
                    aliases=(left.alias, right.alias),
                    evidence=(
                        f"{left.alias} uses {left_budget} budget posture",
                        f"{right.alias} uses {right_budget} budget posture",
                    ),
                    resolution_required=True,
                ))
        return result

    @staticmethod
    def adjudicate(
        proposals: Iterable[AnonymousProposal],
        scorecards: Iterable[ProposalScorecard],
        conflicts: Iterable[ProposalConflict],
        *,
        degraded: bool = False,
    ) -> tuple[AdjudicationResult, bool]:
        proposal_list = list(proposals)
        cards = sorted(scorecards, key=lambda item: (-item.total, item.alias))
        conflicts_list = list(conflicts)
        if not cards:
            return AdjudicationResult(
                outcome="not_required",
                rounds_used=0,
                selected_aliases=(),
                rejected_aliases=(),
                rejection_reasons={},
                compiler_directives=(),
                reasons=("No proposal was available.",),
                confidence=0.0,
            ), True
        passing = [item for item in cards if item.hard_constraints_passed]
        candidates = passing or cards
        leader = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None
        high_conflict = any(
            item.severity == "high" and item.resolution_required
            for item in conflicts_list
        )
        clear_leader = (
            runner_up is None or leader.total - runner_up.total >= 0.10
        )
        aliases = {item.alias for item in proposal_list}
        if clear_leader and not high_conflict:
            selected = (leader.alias,)
            rounds = 0
            outcome = "degraded" if degraded else "selected"
            reasons = (
                "The highest constraint score has a decisive margin.",
                "No unresolved high-severity conflict requires adjudication.",
            )
            early = True
        else:
            selected = tuple(item.alias for item in candidates[:2])
            rounds = 1
            outcome = "degraded" if degraded else "merged"
            reasons = (
                "One bounded adjudication reconciled score proximity or a material conflict.",
                "Selection is constraint-based; proposal counts and majority votes were not used.",
            )
            early = False
        rejected = tuple(sorted(aliases - set(selected)))
        rejection_reasons = {
            alias: (
                "failed a hard constraint"
                if any(
                    card.alias == alias and not card.hard_constraints_passed
                    for card in cards
                )
                else "lower constraint-weighted utility than the selected plan"
            )
            for alias in rejected
        }
        directives: list[str] = [
            "trace every DAG node to an acceptance criterion",
            "preserve the planning budget hard stop",
        ]
        selected_proposals = [
            item for item in proposal_list if item.alias in selected
        ]
        if any(
            item.strategy.get("parallelism") == "aggressive"
            for item in selected_proposals
        ):
            directives.append("parallelize independent branches only")
        if any(
            item.strategy.get("verification") == "independent"
            for item in selected_proposals
        ):
            directives.append("independently verify critical outputs")
            directives.append("retain rollback evidence for high-loss nodes")
        confidence = sum(
            card.total for card in cards if card.alias in selected
        ) / max(1, len(selected))
        return AdjudicationResult(
            outcome=outcome,
            rounds_used=rounds,
            selected_aliases=selected,
            rejected_aliases=rejected,
            rejection_reasons=rejection_reasons,
            compiler_directives=tuple(dict.fromkeys(directives)),
            reasons=reasons,
            confidence=max(0.0, min(1.0, confidence)),
        ), early


class PlanCompiler:
    """Compiles an adjudicated planning receipt into the existing artifact DAG."""

    def compile(self, plan: Plan, receipt: DeliberationReceipt) -> Plan:
        adjudication = receipt.adjudication
        if not receipt.anonymous_proposals:
            return plan
        plan.analysis = dict(plan.analysis)
        plan.analysis["multiplanner"] = {
            "schema_version": receipt.schema_version,
            "fingerprint": receipt.to_dict()["fingerprint"],
            "selected_aliases": list(adjudication.selected_aliases),
            "rejected_aliases": list(adjudication.rejected_aliases),
            "outcome": adjudication.outcome,
            "rounds_used": adjudication.rounds_used,
            "compiler_directives": list(adjudication.compiler_directives),
            "conflict_count": len(receipt.conflicts),
            "parallel": receipt.parallel,
        }
        plan.rationale = (
            f"{plan.rationale} MultiPlanner compiled "
            f"{adjudication.outcome} proposal set "
            f"{', '.join(adjudication.selected_aliases)} into the DAG."
        )
        for conflict in receipt.conflicts:
            if conflict.severity == "high":
                risk = (
                    f"MultiPlanner conflict {conflict.conflict_type}: "
                    + "; ".join(conflict.evidence)
                )
                if risk not in plan.risks:
                    plan.risks.append(risk)
        directives = set(adjudication.compiler_directives)
        for task in plan.tasks:
            if "trace every DAG node to an acceptance criterion" in directives:
                criterion = "Trace this node to the requested final deliverable"
                if criterion not in task.acceptance_criteria:
                    task.acceptance_criteria.append(criterion)
            if "preserve the planning budget hard stop" in directives:
                guard = "Do not expand scope beyond the approved run budget"
                if guard not in task.exclude:
                    task.exclude.append(guard)
            if (
                "independently verify critical outputs" in directives
                and (task.criticality >= 0.7 or task.risk_level == "high")
            ):
                criterion = "Require independent evidence before accepting critical output"
                if criterion not in task.acceptance_criteria:
                    task.acceptance_criteria.append(criterion)
                if criterion not in task.critical_criteria:
                    task.critical_criteria.append(criterion)
            if (
                "retain rollback evidence for high-loss nodes" in directives
                and task.risk_level == "high"
            ):
                criterion = "Retain rollback evidence before irreversible execution"
                if criterion not in task.acceptance_criteria:
                    task.acceptance_criteria.append(criterion)
        return plan
