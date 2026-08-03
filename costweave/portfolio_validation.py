"""Exact small-DAG validation and sensitivity audits for v0.5.6.4.4."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping, Sequence

from .portfolio_audit import (
    GlobalPortfolioAuditEngine,
    PortfolioAuditError,
    PortfolioAuditSolution,
    PortfolioOption,
    PortfolioProblem,
    _bounded,
    _digest,
    _evidence_rank,
    _finite,
    _topological_order,
)


PORTFOLIO_VALIDATION_SCHEMA_VERSION = "v0.5.6.4.4"
PORTFOLIO_VALIDATION_POLICY_REVISION = "exact-oracle-sensitivity-v1"
ORACLE_STATUSES = ("optimal", "infeasible_proven", "skipped_bounds")
DIFFERENTIAL_STATES = (
    "not_run_bounds",
    "consistent_infeasible",
    "missed_feasible_assignment",
    "candidate_constraint_inconsistency",
    "exact_selection_match",
    "equivalent_objective",
    "suboptimal_candidate",
)
ROBUSTNESS_GRADES = (
    "fragile",
    "sensitive",
    "moderate",
    "stable_within_probed_range",
)
SENSITIVITY_PARAMETERS = (
    "direct_model_cost_usd",
    "success_lower",
    "latency_p95_ms",
    "rework_cost_if_failed_usd",
)


@dataclass(frozen=True, slots=True)
class ExactOracleResult:
    oracle_id: str
    problem_id: str
    comparison_group_hash: str
    topology_kind: str
    status: str
    feasible: bool | None
    selected_options: Mapping[str, str]
    metrics: Mapping[str, Any]
    constraint_checks: Mapping[str, bool]
    search_audit: Mapping[str, Any]
    evidence_levels: tuple[str, ...]
    directional_evidence_eligible: bool
    optimality_proven: bool
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = PORTFOLIO_VALIDATION_SCHEMA_VERSION
    policy_revision: str = PORTFOLIO_VALIDATION_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.schema_version != PORTFOLIO_VALIDATION_SCHEMA_VERSION:
            raise PortfolioAuditError("exact oracle schema is unsupported")
        if self.status not in ORACLE_STATUSES:
            raise PortfolioAuditError("exact oracle status is invalid")
        if self.status == "skipped_bounds":
            if self.feasible is not None or self.optimality_proven:
                raise PortfolioAuditError("skipped oracle cannot claim a result")
        elif self.feasible is None or not self.optimality_proven:
            raise PortfolioAuditError("executed oracle must prove its result")
        if self.status == "optimal" and (
            self.feasible is not True or not self.selected_options
        ):
            raise PortfolioAuditError("optimal oracle requires a selected portfolio")
        if self.status == "infeasible_proven" and (
            self.feasible is not False or self.selected_options
        ):
            raise PortfolioAuditError("infeasible oracle cannot select a portfolio")
        if not self.advisory_only or self.routing_authority:
            raise PortfolioAuditError("exact oracle cannot control routing")
        if self.may_change_actual_route or self.commercial_claims_allowed:
            raise PortfolioAuditError("exact oracle cannot intervene or grant claims")
        expected = "portfolio_oracle_" + _digest(self.identity())[:32]
        if self.oracle_id != expected:
            raise PortfolioAuditError("exact oracle id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "comparison_group_hash": self.comparison_group_hash,
            "topology_kind": self.topology_kind,
            "status": self.status,
            "feasible": self.feasible,
            "selected_options": dict(sorted(self.selected_options.items())),
            "metrics": dict(self.metrics),
            "constraint_checks": dict(sorted(self.constraint_checks.items())),
            "search_audit": dict(self.search_audit),
            "evidence_levels": list(self.evidence_levels),
            "directional_evidence_eligible": self.directional_evidence_eligible,
            "optimality_proven": self.optimality_proven,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"oracle_id": self.oracle_id, **self.identity()}


class ExactPortfolioOracle:
    """Exhaustively solves bounded small portfolio problems."""

    def __init__(
        self,
        *,
        maximum_nodes: int = 10,
        maximum_combinations: int = 100_000,
    ) -> None:
        if maximum_nodes < 1 or maximum_combinations < 1:
            raise PortfolioAuditError("oracle bounds must be positive")
        self.maximum_nodes = min(16, int(maximum_nodes))
        self.maximum_combinations = min(1_000_000, int(maximum_combinations))
        self._evaluator = GlobalPortfolioAuditEngine()

    def solve(self, problem: PortfolioProblem) -> ExactOracleResult:
        order = _topological_order(problem.nodes, problem.edges)
        node_map = {node.node_id: node for node in problem.nodes}
        options: dict[str, list[PortfolioOption]] = defaultdict(list)
        static_rejections: dict[str, int] = defaultdict(int)
        for option in problem.options:
            reason = self._evaluator._static_rejection(
                problem, node_map[option.node_id], option
            )
            if reason:
                static_rejections[reason] += 1
            else:
                options[option.node_id].append(option)
        for values in options.values():
            values.sort(key=lambda item: item.option_id)

        evidence_levels = tuple(
            sorted({option.evidence_level for option in problem.options})
        )
        directional = bool(evidence_levels) and min(
            _evidence_rank(item) for item in evidence_levels
        ) >= 2
        missing = tuple(node_id for node_id in order if not options[node_id])
        combinations = (
            0
            if missing
            else math.prod(len(options[node_id]) for node_id in order)
        )
        if missing:
            return self._result(
                problem,
                status="infeasible_proven",
                feasible=False,
                selected={},
                metrics={},
                checks={"feasible_assignment": False},
                search_audit=self._search_audit(
                    combinations=0,
                    assignments_examined=0,
                    states_expanded=0,
                    hard_pruned=0,
                    static_rejections=static_rejections,
                    skipped_reason=None,
                    infeasible_reason="no_static_option:" + ",".join(missing),
                ),
                evidence_levels=evidence_levels,
                directional=directional,
                optimality_proven=True,
            )

        if len(order) > self.maximum_nodes or combinations > self.maximum_combinations:
            return self._result(
                problem,
                status="skipped_bounds",
                feasible=None,
                selected={},
                metrics={},
                checks={},
                search_audit=self._search_audit(
                    combinations=combinations,
                    assignments_examined=0,
                    states_expanded=0,
                    hard_pruned=0,
                    static_rejections=static_rejections,
                    skipped_reason=(
                        "node_limit"
                        if len(order) > self.maximum_nodes
                        else "combination_limit"
                    ),
                ),
                evidence_levels=evidence_levels,
                directional=directional,
                optimality_proven=False,
            )

        assignments_examined = 0
        states_expanded = 0
        hard_pruned = 0
        best: tuple[
            float,
            int,
            tuple[str, ...],
            dict[str, PortfolioOption],
            dict[str, Any],
            dict[str, bool],
        ] | None = None
        feasible_count = 0

        def visit(index: int, selected: dict[str, PortfolioOption]) -> None:
            nonlocal assignments_examined, states_expanded, hard_pruned
            nonlocal best, feasible_count
            if index == len(order):
                assignments_examined += 1
                metrics = self._evaluator._metrics(problem, selected)
                checks = self._evaluator._constraint_checks(
                    problem, selected, metrics
                )
                if all(checks.values()):
                    feasible_count += 1
                    option_key = tuple(
                        selected[node_id].option_id for node_id in order
                    )
                    candidate = (
                        float(metrics["expected_total_loss_usd"]),
                        int(metrics["critical_path_p95_ms"]),
                        option_key,
                        dict(selected),
                        metrics,
                        checks,
                    )
                    if best is None or candidate[:3] < best[:3]:
                        best = candidate
                else:
                    hard_pruned += 1
                return
            node_id = order[index]
            for option in options[node_id]:
                states_expanded += 1
                selected[node_id] = option
                if self._evaluator._partial_constraints(problem, selected):
                    visit(index + 1, selected)
                else:
                    hard_pruned += 1
                selected.pop(node_id, None)

        visit(0, {})
        audit = self._search_audit(
            combinations=combinations,
            assignments_examined=assignments_examined,
            states_expanded=states_expanded,
            hard_pruned=hard_pruned,
            static_rejections=static_rejections,
            skipped_reason=None,
        )
        if best is None:
            audit["infeasible_reason"] = "all_assignments_failed_hard_constraints"
            return self._result(
                problem,
                status="infeasible_proven",
                feasible=False,
                selected={},
                metrics={},
                checks={"feasible_assignment": False},
                search_audit=audit,
                evidence_levels=evidence_levels,
                directional=directional,
                optimality_proven=True,
            )
        _, _, _, selected, metrics, checks = best
        audit["feasible_assignments"] = feasible_count
        return self._result(
            problem,
            status="optimal",
            feasible=True,
            selected=selected,
            metrics=metrics,
            checks=checks,
            search_audit=audit,
            evidence_levels=evidence_levels,
            directional=directional,
            optimality_proven=True,
        )

    def _search_audit(
        self,
        *,
        combinations: int,
        assignments_examined: int,
        states_expanded: int,
        hard_pruned: int,
        static_rejections: Mapping[str, int],
        skipped_reason: str | None,
        infeasible_reason: str | None = None,
    ) -> dict[str, Any]:
        result = {
            "algorithm": "exact-depth-first-enumeration",
            "maximum_nodes": self.maximum_nodes,
            "maximum_combinations": self.maximum_combinations,
            "candidate_combinations": combinations,
            "assignments_examined": assignments_examined,
            "states_expanded": states_expanded,
            "hard_constraint_pruned": hard_pruned,
            "static_rejections": dict(sorted(static_rejections.items())),
            "skipped_reason": skipped_reason,
        }
        if infeasible_reason:
            result["infeasible_reason"] = infeasible_reason
        return result

    @staticmethod
    def _result(
        problem: PortfolioProblem,
        *,
        status: str,
        feasible: bool | None,
        selected: Mapping[str, PortfolioOption],
        metrics: Mapping[str, Any],
        checks: Mapping[str, bool],
        search_audit: Mapping[str, Any],
        evidence_levels: tuple[str, ...],
        directional: bool,
        optimality_proven: bool,
    ) -> ExactOracleResult:
        identity = {
            "problem_id": problem.problem_id,
            "comparison_group_hash": problem.comparison_group_hash,
            "topology_kind": problem.topology_kind,
            "status": status,
            "feasible": feasible,
            "selected_options": {
                key: value.option_id for key, value in sorted(selected.items())
            },
            "metrics": dict(metrics),
            "constraint_checks": dict(sorted(checks.items())),
            "search_audit": dict(search_audit),
            "evidence_levels": list(evidence_levels),
            "directional_evidence_eligible": directional,
            "optimality_proven": optimality_proven,
            "advisory_only": True,
            "routing_authority": False,
            "may_change_actual_route": False,
            "commercial_claims_allowed": False,
            "schema_version": PORTFOLIO_VALIDATION_SCHEMA_VERSION,
            "policy_revision": PORTFOLIO_VALIDATION_POLICY_REVISION,
        }
        return ExactOracleResult(
            oracle_id="portfolio_oracle_" + _digest(identity)[:32],
            problem_id=problem.problem_id,
            comparison_group_hash=problem.comparison_group_hash,
            topology_kind=problem.topology_kind,
            status=status,
            feasible=feasible,
            selected_options=identity["selected_options"],
            metrics=metrics,
            constraint_checks=checks,
            search_audit=search_audit,
            evidence_levels=evidence_levels,
            directional_evidence_eligible=directional,
            optimality_proven=optimality_proven,
        )


@dataclass(frozen=True, slots=True)
class PortfolioDifferentialReport:
    report_id: str
    problem_id: str
    candidate_audit_id: str
    oracle_id: str
    validation_state: str
    exact_validation_passed: bool
    selection_match: bool | None
    objective_match: bool | None
    candidate_regret_usd: float | None
    candidate_relative_regret: float | None
    candidate_optimality_claim_valid: bool | None
    details: Mapping[str, Any]
    directional_evidence_eligible: bool
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = PORTFOLIO_VALIDATION_SCHEMA_VERSION
    policy_revision: str = PORTFOLIO_VALIDATION_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.schema_version != PORTFOLIO_VALIDATION_SCHEMA_VERSION:
            raise PortfolioAuditError("differential schema is unsupported")
        if self.validation_state not in DIFFERENTIAL_STATES:
            raise PortfolioAuditError("differential validation state is invalid")
        passed_states = {
            "consistent_infeasible",
            "exact_selection_match",
            "equivalent_objective",
        }
        if self.exact_validation_passed != (
            self.validation_state in passed_states
        ):
            raise PortfolioAuditError("differential pass state is inconsistent")
        if not self.advisory_only or self.routing_authority:
            raise PortfolioAuditError("differential report cannot control routing")
        if self.may_change_actual_route or self.commercial_claims_allowed:
            raise PortfolioAuditError("differential report cannot intervene")
        expected = "portfolio_diff_" + _digest(self.identity())[:32]
        if self.report_id != expected:
            raise PortfolioAuditError("portfolio differential id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "candidate_audit_id": self.candidate_audit_id,
            "oracle_id": self.oracle_id,
            "validation_state": self.validation_state,
            "exact_validation_passed": self.exact_validation_passed,
            "selection_match": self.selection_match,
            "objective_match": self.objective_match,
            "candidate_regret_usd": self.candidate_regret_usd,
            "candidate_relative_regret": self.candidate_relative_regret,
            "candidate_optimality_claim_valid": self.candidate_optimality_claim_valid,
            "details": dict(self.details),
            "directional_evidence_eligible": self.directional_evidence_eligible,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"report_id": self.report_id, **self.identity()}


def compare_with_exact_oracle(
    candidate: PortfolioAuditSolution,
    oracle: ExactOracleResult,
    *,
    tolerance_usd: float = 1e-8,
) -> PortfolioDifferentialReport:
    if candidate.problem_id != oracle.problem_id:
        raise PortfolioAuditError("candidate and oracle problem ids differ")
    tolerance = _finite(tolerance_usd, "tolerance_usd")
    selection_match: bool | None = None
    objective_match: bool | None = None
    regret: float | None = None
    relative: float | None = None
    if oracle.status == "skipped_bounds":
        state = "not_run_bounds"
        passed = False
    elif not candidate.feasible and oracle.feasible is False:
        state = "consistent_infeasible"
        passed = True
        selection_match = True
        objective_match = True
        regret = 0.0
        relative = 0.0
    elif not candidate.feasible and oracle.feasible:
        state = "missed_feasible_assignment"
        passed = False
        selection_match = False
        objective_match = False
    elif candidate.feasible and oracle.feasible is False:
        state = "candidate_constraint_inconsistency"
        passed = False
        selection_match = False
        objective_match = False
    else:
        candidate_loss = float(candidate.metrics["expected_total_loss_usd"])
        oracle_loss = float(oracle.metrics["expected_total_loss_usd"])
        regret = round(max(0.0, candidate_loss - oracle_loss), 8)
        relative = round(regret / max(abs(oracle_loss), 1e-12), 8)
        selection_match = dict(candidate.selected_options) == dict(
            oracle.selected_options
        )
        objective_match = abs(candidate_loss - oracle_loss) <= tolerance
        passed = objective_match
        state = (
            "exact_selection_match"
            if selection_match and objective_match
            else "equivalent_objective"
            if objective_match
            else "suboptimal_candidate"
        )
    claimed = bool(candidate.solver_audit.get("optimality_proven"))
    claim_valid = passed if claimed else None
    details = {
        "candidate_feasible": candidate.feasible,
        "oracle_status": oracle.status,
        "oracle_feasible": oracle.feasible,
        "candidate_expected_total_loss_usd": (
            candidate.metrics.get("expected_total_loss_usd")
            if candidate.feasible
            else None
        ),
        "oracle_expected_total_loss_usd": (
            oracle.metrics.get("expected_total_loss_usd")
            if oracle.feasible
            else None
        ),
        "candidate_selected_options": dict(candidate.selected_options),
        "oracle_selected_options": dict(oracle.selected_options),
        "tolerance_usd": tolerance,
    }
    identity = {
        "problem_id": candidate.problem_id,
        "candidate_audit_id": candidate.audit_id,
        "oracle_id": oracle.oracle_id,
        "validation_state": state,
        "exact_validation_passed": passed,
        "selection_match": selection_match,
        "objective_match": objective_match,
        "candidate_regret_usd": regret,
        "candidate_relative_regret": relative,
        "candidate_optimality_claim_valid": claim_valid,
        "details": details,
        "directional_evidence_eligible": oracle.directional_evidence_eligible,
        "advisory_only": True,
        "routing_authority": False,
        "may_change_actual_route": False,
        "commercial_claims_allowed": False,
        "schema_version": PORTFOLIO_VALIDATION_SCHEMA_VERSION,
        "policy_revision": PORTFOLIO_VALIDATION_POLICY_REVISION,
    }
    return PortfolioDifferentialReport(
        report_id="portfolio_diff_" + _digest(identity)[:32],
        problem_id=candidate.problem_id,
        candidate_audit_id=candidate.audit_id,
        oracle_id=oracle.oracle_id,
        validation_state=state,
        exact_validation_passed=passed,
        selection_match=selection_match,
        objective_match=objective_match,
        candidate_regret_usd=regret,
        candidate_relative_regret=relative,
        candidate_optimality_claim_valid=claim_valid,
        details=details,
        directional_evidence_eligible=oracle.directional_evidence_eligible,
    )


@dataclass(frozen=True, slots=True)
class SensitivityProbe:
    probe_id: str
    option_id: str
    parameter: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        _bounded(self.probe_id, "probe_id")
        _bounded(self.option_id, "option_id")
        if self.parameter not in SENSITIVITY_PARAMETERS:
            raise PortfolioAuditError("sensitivity parameter is invalid")
        if not self.values or len(self.values) > 21:
            raise PortfolioAuditError("probe must contain 1 to 21 values")
        checked = tuple(_finite(value, "probe value") for value in self.values)
        if len(set(checked)) != len(checked):
            raise PortfolioAuditError("sensitivity probe values must be unique")
        if self.parameter == "success_lower" and any(
            value > 1.0 for value in checked
        ):
            raise PortfolioAuditError("success sensitivity must stay in [0,1]")
        if self.parameter == "latency_p95_ms" and any(
            not value.is_integer() for value in checked
        ):
            raise PortfolioAuditError("latency sensitivity values must be integers")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["values"] = list(self.values)
        return result


@dataclass(frozen=True, slots=True)
class PortfolioSensitivityReport:
    sensitivity_id: str
    problem_id: str
    baseline_oracle_id: str
    baseline_selected_options: Mapping[str, str]
    baseline_expected_total_loss_usd: float
    probe_results: tuple[Mapping[str, Any], ...]
    tested_point_count: int
    robustness_grade: str
    minimum_flip_margin_ratio: float | None
    directional_evidence_eligible: bool
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = PORTFOLIO_VALIDATION_SCHEMA_VERSION
    policy_revision: str = PORTFOLIO_VALIDATION_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.schema_version != PORTFOLIO_VALIDATION_SCHEMA_VERSION:
            raise PortfolioAuditError("sensitivity schema is unsupported")
        if self.robustness_grade not in ROBUSTNESS_GRADES:
            raise PortfolioAuditError("sensitivity robustness grade is invalid")
        if self.tested_point_count < 1:
            raise PortfolioAuditError("sensitivity must contain tested points")
        actual_points = sum(
            len(tuple(item.get("tested_points", ())))
            for item in self.probe_results
        )
        if actual_points != self.tested_point_count:
            raise PortfolioAuditError("sensitivity tested point count is inconsistent")
        _finite(
            self.baseline_expected_total_loss_usd,
            "baseline_expected_total_loss_usd",
        )
        if self.minimum_flip_margin_ratio is not None:
            _finite(self.minimum_flip_margin_ratio, "minimum_flip_margin_ratio")
        if not self.advisory_only or self.routing_authority:
            raise PortfolioAuditError("sensitivity report cannot control routing")
        if self.may_change_actual_route or self.commercial_claims_allowed:
            raise PortfolioAuditError("sensitivity report cannot intervene")
        expected = "portfolio_sensitivity_" + _digest(self.identity())[:32]
        if self.sensitivity_id != expected:
            raise PortfolioAuditError("portfolio sensitivity id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "baseline_oracle_id": self.baseline_oracle_id,
            "baseline_selected_options": dict(
                sorted(self.baseline_selected_options.items())
            ),
            "baseline_expected_total_loss_usd": self.baseline_expected_total_loss_usd,
            "probe_results": [dict(item) for item in self.probe_results],
            "tested_point_count": self.tested_point_count,
            "robustness_grade": self.robustness_grade,
            "minimum_flip_margin_ratio": self.minimum_flip_margin_ratio,
            "directional_evidence_eligible": self.directional_evidence_eligible,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"sensitivity_id": self.sensitivity_id, **self.identity()}


class PortfolioSensitivityAnalyzer:
    def __init__(self, oracle: ExactPortfolioOracle | None = None) -> None:
        self.oracle = oracle or ExactPortfolioOracle()

    def analyze(
        self,
        problem: PortfolioProblem,
        probes: Sequence[SensitivityProbe],
    ) -> PortfolioSensitivityReport:
        items = tuple(probes)
        if not items or len(items) > 32:
            raise PortfolioAuditError("sensitivity analysis requires 1 to 32 probes")
        if sum(len(item.values) for item in items) > 128:
            raise PortfolioAuditError("sensitivity analysis supports at most 128 points")
        if len({item.probe_id for item in items}) != len(items):
            raise PortfolioAuditError("sensitivity probe ids must be unique")
        option_map = {item.option_id: item for item in problem.options}
        if any(item.option_id not in option_map for item in items):
            raise PortfolioAuditError("sensitivity probe references unknown option")
        baseline = self.oracle.solve(problem)
        if baseline.status != "optimal":
            raise PortfolioAuditError(
                "sensitivity baseline requires an exact feasible portfolio"
            )
        results: list[dict[str, Any]] = []
        flip_margins: list[float] = []
        total_points = 0
        for probe in sorted(items, key=lambda item: item.probe_id):
            original = option_map[probe.option_id]
            baseline_value = float(getattr(original, probe.parameter))
            points: list[dict[str, Any]] = []
            for value in sorted(float(item) for item in probe.values):
                total_points += 1
                mutated = self._mutate_option(original, probe.parameter, value)
                options = tuple(
                    mutated if item.option_id == original.option_id else item
                    for item in problem.options
                )
                result = self.oracle.solve(replace(problem, options=options))
                changed = (
                    result.status != "optimal"
                    or dict(result.selected_options)
                    != dict(baseline.selected_options)
                )
                distance = self._relative_distance(baseline_value, value)
                points.append(
                    {
                        "value": value,
                        "relative_distance_from_baseline": round(distance, 8),
                        "oracle_status": result.status,
                        "selected_options": dict(result.selected_options),
                        "expected_total_loss_usd": (
                            result.metrics.get("expected_total_loss_usd")
                            if result.status == "optimal"
                            else None
                        ),
                        "route_changed_from_baseline": changed,
                    }
                )
            flipped = [
                item for item in points if item["route_changed_from_baseline"]
            ]
            closest = min(
                flipped,
                key=lambda item: (
                    item["relative_distance_from_baseline"],
                    abs(item["value"] - baseline_value),
                    item["value"],
                ),
                default=None,
            )
            if closest is not None:
                flip_margins.append(
                    float(closest["relative_distance_from_baseline"])
                )
            results.append(
                {
                    "probe_id": probe.probe_id,
                    "option_id": probe.option_id,
                    "parameter": probe.parameter,
                    "baseline_value": baseline_value,
                    "flip_observed": closest is not None,
                    "nearest_flip_value": (
                        closest["value"] if closest is not None else None
                    ),
                    "flip_margin_ratio": (
                        closest["relative_distance_from_baseline"]
                        if closest is not None
                        else None
                    ),
                    "tested_points": points,
                }
            )
        margin = min(flip_margins, default=None)
        grade = self._grade(margin)
        identity = {
            "problem_id": problem.problem_id,
            "baseline_oracle_id": baseline.oracle_id,
            "baseline_selected_options": dict(
                sorted(baseline.selected_options.items())
            ),
            "baseline_expected_total_loss_usd": float(
                baseline.metrics["expected_total_loss_usd"]
            ),
            "probe_results": results,
            "tested_point_count": total_points,
            "robustness_grade": grade,
            "minimum_flip_margin_ratio": margin,
            "directional_evidence_eligible": baseline.directional_evidence_eligible,
            "advisory_only": True,
            "routing_authority": False,
            "may_change_actual_route": False,
            "commercial_claims_allowed": False,
            "schema_version": PORTFOLIO_VALIDATION_SCHEMA_VERSION,
            "policy_revision": PORTFOLIO_VALIDATION_POLICY_REVISION,
        }
        return PortfolioSensitivityReport(
            sensitivity_id="portfolio_sensitivity_" + _digest(identity)[:32],
            problem_id=problem.problem_id,
            baseline_oracle_id=baseline.oracle_id,
            baseline_selected_options=baseline.selected_options,
            baseline_expected_total_loss_usd=float(
                baseline.metrics["expected_total_loss_usd"]
            ),
            probe_results=tuple(results),
            tested_point_count=total_points,
            robustness_grade=grade,
            minimum_flip_margin_ratio=margin,
            directional_evidence_eligible=baseline.directional_evidence_eligible,
        )

    @staticmethod
    def _mutate_option(
        option: PortfolioOption, parameter: str, value: float
    ) -> PortfolioOption:
        if parameter == "success_lower":
            return replace(
                option,
                success_lower=value,
                success_mean=max(option.success_mean, value),
            )
        if parameter == "latency_p95_ms":
            integer = int(value)
            return replace(
                option,
                latency_p50_ms=min(option.latency_p50_ms, integer),
                latency_p95_ms=integer,
            )
        return replace(option, **{parameter: value})

    @staticmethod
    def _relative_distance(baseline: float, value: float) -> float:
        return abs(value - baseline) / max(abs(baseline), 0.01)

    @staticmethod
    def _grade(margin: float | None) -> str:
        if margin is None:
            return "stable_within_probed_range"
        if margin <= 0.05:
            return "fragile"
        if margin <= 0.20:
            return "sensitive"
        if margin <= 0.50:
            return "moderate"
        return "stable_within_probed_range"


def public_portfolio_validation_policy() -> dict[str, Any]:
    return {
        "schema_version": PORTFOLIO_VALIDATION_SCHEMA_VERSION,
        "policy_revision": PORTFOLIO_VALIDATION_POLICY_REVISION,
        "exact_oracle": {
            "algorithm": "exact-depth-first-enumeration",
            "default_maximum_nodes": 10,
            "default_maximum_combinations": 100_000,
            "proves_optimality_within_bounds": True,
            "fails_closed_outside_bounds": True,
        },
        "differential_validation": {
            "compares_beam_to_exact_oracle": True,
            "reports_selection_match": True,
            "reports_objective_regret": True,
        },
        "sensitivity_parameters": list(SENSITIVITY_PARAMETERS),
        "sensitivity_claim_scope": "tested-points-only",
        "current_evidence": "E0-contract-only",
        "advisory_only": True,
        "routing_authority": "legacy-resource-optimizer",
        "may_change_actual_route": False,
        "external_calls_required": False,
        "commercial_claims_allowed": False,
    }


__all__ = [
    "ExactOracleResult",
    "ExactPortfolioOracle",
    "DIFFERENTIAL_STATES",
    "PORTFOLIO_VALIDATION_POLICY_REVISION",
    "PORTFOLIO_VALIDATION_SCHEMA_VERSION",
    "PortfolioDifferentialReport",
    "PortfolioSensitivityAnalyzer",
    "PortfolioSensitivityReport",
    "ROBUSTNESS_GRADES",
    "SENSITIVITY_PARAMETERS",
    "SensitivityProbe",
    "compare_with_exact_oracle",
    "public_portfolio_validation_policy",
]
