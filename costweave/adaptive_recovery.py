"""Evidence-matched completion checkpoints, pollution analysis and recovery gates."""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from .contracts_v4 import utc_now
from .domain import TaskStatus
from .planning_contracts import ManagementLimits
from .recovery import RecoveryAction, RecoveryDecision


CHECKPOINT_SCHEMA_VERSION = "v0.5.5"
RECOVERY_GATE_REVISION = "adaptive-recovery-0.5.5"


@dataclass(frozen=True, slots=True)
class CompletionCheckpoint:
    checkpoint_id: str
    stage: str
    pcomplete: float
    threshold: float
    recovery_recommended: bool
    risk_band: str
    signals: Mapping[str, float | int]
    evidence: tuple[str, ...]
    created_at: str
    schema_version: str = CHECKPOINT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["signals"] = dict(self.signals)
        data["evidence"] = list(self.evidence)
        return data


class CompletionEstimator:
    THRESHOLDS = {
        "low": 0.60,
        "medium": 0.75,
        "high": 0.90,
    }

    def estimate(
        self,
        record: Any,
        *,
        stage: str,
        active_tasks: int = 0,
    ) -> CompletionCheckpoint:
        tasks = list(record.plan.tasks) if record.plan is not None else []
        eligible = [
            task for task in tasks
            if task.status != TaskStatus.INVALIDATED
        ]
        risk_band = self._risk_band(record, eligible)
        threshold = self.THRESHOLDS[risk_band]
        total = max(1, len(eligible))
        validated = sum(
            task.status == TaskStatus.VALIDATED for task in eligible
        )
        rejected = sum(
            task.status == TaskStatus.REJECTED for task in eligible
        )
        blocked = sum(
            task.status == TaskStatus.PENDING
            and bool(task.blocked_reason)
            for task in eligible
        )

        weighted_probability = 0.0
        weight_total = 0.0
        for task in eligible:
            weight = max(0.1, float(task.criticality))
            prior = float(
                task.predicted_success_lower_bound
                or task.predicted_success
                or 0.5
            )
            if task.status == TaskStatus.VALIDATED:
                score = float((task.validation or {}).get("score", prior))
                evidence_probability = prior * 0.35 + score * 0.65
            elif task.status == TaskStatus.REJECTED:
                evidence_probability = min(0.2, prior * 0.25)
            elif task.status == TaskStatus.SUSPECT:
                evidence_probability = prior * 0.55
            else:
                evidence_probability = prior
            weighted_probability += evidence_probability * weight
            weight_total += weight
        route_probability = (
            weighted_probability / weight_total
            if weight_total
            else 0.05
        )

        observed = validated + rejected
        test_pass_rate = (
            validated / observed if observed else 0.70
        )
        constraint_items = 0
        covered_constraints = 0
        evidence_items = 0
        covered_evidence = 0
        for task in eligible:
            criteria = (
                (task.validation or {}).get("criterion_results", [])
                if task.validation
                else []
            )
            if criteria:
                constraint_items += len(criteria)
                covered_constraints += sum(
                    bool(item.get("passed"))
                    for item in criteria
                    if isinstance(item, Mapping)
                )
                evidence_items += len(criteria)
                covered_evidence += sum(
                    bool(item.get("evidence_refs"))
                    for item in criteria
                    if isinstance(item, Mapping)
                )
            else:
                contract_criteria = (
                    task.success_contract.get("criteria", [])
                    if isinstance(task.success_contract, dict)
                    else []
                )
                constraint_items += len(contract_criteria)
                covered_constraints += len(contract_criteria)
        constraint_coverage = (
            covered_constraints / constraint_items
            if constraint_items else 0.0
        )
        evidence_completeness = (
            covered_evidence / evidence_items
            if evidence_items else (0.62 if eligible else 0.0)
        )
        conflict_count = len(
            (
                record.planning_deliberation
                or {}
            ).get("conflicts", [])
        )
        proposal_count = max(
            1,
            len(record.planning_proposals),
        )
        model_conflict = min(1.0, conflict_count / proposal_count)
        progress = validated / total
        budget_burn = min(
            1.5,
            float(record.spent) / max(0.000001, record.request.budget),
        )
        budget_health = max(
            0.0,
            1.0 - max(0.0, budget_burn - progress - 0.15),
        )
        dependency_health = max(
            0.0, 1.0 - blocked / total
        )
        remaining_critical_path = (
            sum(
                max(0.1, float(task.criticality))
                for task in eligible
                if task.status != TaskStatus.VALIDATED
            )
            / max(
                0.1,
                sum(
                    max(0.1, float(task.criticality))
                    for task in eligible
                ),
            )
        )
        pcomplete = (
            route_probability * 0.56
            + test_pass_rate * 0.12
            + constraint_coverage * 0.10
            + evidence_completeness * 0.08
            + budget_health * 0.07
            + dependency_health * 0.07
            - model_conflict * 0.04
            - min(0.08, rejected * 0.025)
        )
        if eligible and validated == len(eligible):
            pcomplete = max(
                pcomplete,
                min(
                    0.98,
                    sum(
                        float((task.validation or {}).get("score", 0.8))
                        for task in eligible
                    )
                    / len(eligible)
                    + 0.04,
                ),
            )
        pcomplete = round(max(0.05, min(0.98, pcomplete)), 6)
        ordinal = len(getattr(record, "completion_checkpoints", [])) + 1
        raw_id = (
            f"{record.id}:{getattr(record.plan, 'version', 0)}:"
            f"{stage}:{ordinal}"
        )
        checkpoint_id = "checkpoint_" + hashlib.sha256(
            raw_id.encode("utf-8")
        ).hexdigest()[:32]
        evidence = (
            f"{validated}/{len(eligible)} tasks validated",
            f"constraint coverage={constraint_coverage:.3f}",
            f"evidence completeness={evidence_completeness:.3f}",
            f"budget burn={budget_burn:.3f}",
            f"blocked dependencies={blocked}",
            f"active tasks={max(0, int(active_tasks))}",
        )
        return CompletionCheckpoint(
            checkpoint_id=checkpoint_id,
            stage=stage,
            pcomplete=pcomplete,
            threshold=threshold,
            recovery_recommended=pcomplete < threshold,
            risk_band=risk_band,
            signals={
                "route_probability": round(route_probability, 6),
                "test_pass_rate": round(test_pass_rate, 6),
                "constraint_coverage": round(
                    constraint_coverage, 6
                ),
                "evidence_completeness": round(
                    evidence_completeness, 6
                ),
                "model_conflict": round(model_conflict, 6),
                "budget_burn": round(budget_burn, 6),
                "budget_health": round(budget_health, 6),
                "blocked_dependencies": blocked,
                "remaining_critical_path": round(
                    remaining_critical_path, 6
                ),
                "validated_tasks": validated,
                "total_tasks": len(eligible),
                "active_tasks": max(0, int(active_tasks)),
            },
            evidence=evidence,
            created_at=utc_now(),
        )

    @staticmethod
    def _risk_band(record: Any, tasks: Iterable[Any]) -> str:
        task_list = list(tasks)
        if any(task.risk_level == "high" for task in task_list):
            return "high"
        difficulty = int(
            getattr(record.plan, "difficulty", 1)
            if record.plan is not None
            else 1
        )
        if difficulty >= 7 or any(
            task.risk_level not in {"low", "normal"}
            for task in task_list
        ):
            return "medium"
        return "low"


@dataclass(frozen=True, slots=True)
class PollutionReport:
    failed_task_id: str
    requested_scope: str
    invalidated_task_ids: tuple[str, ...]
    suspect_task_ids: tuple[str, ...]
    preserved_task_ids: tuple[str, ...]
    propagation_paths: tuple[Mapping[str, Any], ...]
    evidence: tuple[str, ...]
    created_at: str
    schema_version: str = CHECKPOINT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "invalidated_task_ids",
            "suspect_task_ids",
            "preserved_task_ids",
            "propagation_paths",
            "evidence",
        ):
            data[key] = list(data[key])
        return data


class PollutionAnalyzer:
    """Propagate hard/validation pollution while preserving independent siblings."""

    def analyze(
        self,
        plan: Any,
        *,
        failed_task_id: str,
        requested_scope: str,
        failure_class: str,
    ) -> PollutionReport:
        known = {task.id for task in plan.tasks}
        if failed_task_id not in known:
            raise ValueError(f"unknown failed task: {failed_task_id}")
        children: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for task in plan.tasks:
            for dependency in task.dependencies:
                kind = task.dependency_types.get(dependency, "hard")
                children[dependency].append((task.id, kind))

        invalidated = {failed_task_id}
        suspect: set[str] = set()
        paths: list[dict[str, Any]] = []
        queue = deque([failed_task_id])
        while queue:
            current = queue.popleft()
            for child, kind in children[current]:
                if kind in {"hard", "validation"}:
                    paths.append(
                        {
                            "from_task": current,
                            "to_task": child,
                            "dependency_kind": kind,
                            "effect": "invalidate",
                        }
                    )
                    if child not in invalidated:
                        invalidated.add(child)
                        suspect.discard(child)
                        queue.append(child)
                elif child not in invalidated:
                    suspect.add(child)
                    paths.append(
                        {
                            "from_task": current,
                            "to_task": child,
                            "dependency_kind": kind,
                            "effect": "suspect",
                        }
                    )
        preserved = known - invalidated - suspect
        evidence = [
            f"failure_class={failure_class}",
            "hard and validation edges propagate invalidation",
            "soft edges produce suspicion without automatic deletion",
            (
                "global planning is allowed to reconsider all nodes, but "
                "validated artifacts are deleted only when lineage is polluted"
                if requested_scope == "global"
                else "recovery remains within the evidence-linked subgraph"
            ),
        ]
        return PollutionReport(
            failed_task_id=failed_task_id,
            requested_scope=requested_scope,
            invalidated_task_ids=tuple(sorted(invalidated)),
            suspect_task_ids=tuple(sorted(suspect)),
            preserved_task_ids=tuple(sorted(preserved)),
            propagation_paths=tuple(paths),
            evidence=tuple(evidence),
            created_at=utc_now(),
        )


@dataclass(frozen=True, slots=True)
class RecoveryGateReceipt:
    allowed: bool
    proposed_action: str
    effective_action: str
    reason: str
    requires_approval: bool
    authority_preserved: bool
    checks: tuple[Mapping[str, Any], ...]
    policy_revision: str = RECOVERY_GATE_REVISION

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["checks"] = [dict(item) for item in self.checks]
        return data


class RecoveryGate:
    IRREVERSIBLE_TOOLS = frozenset(
        {
            "delete",
            "database_migration",
            "deploy_production",
            "payment",
            "send_external",
            "write_production",
        }
    )

    def __init__(self, limits: ManagementLimits | None = None) -> None:
        self.limits = limits or ManagementLimits()

    def evaluate(
        self,
        *,
        record: Any,
        task: Any,
        failure: Any,
        decision: RecoveryDecision,
        approval_status: str | None = None,
    ) -> RecoveryGateReceipt:
        checks: list[dict[str, Any]] = []

        def add(name: str, passed: bool, evidence: str) -> None:
            checks.append(
                {
                    "name": name,
                    "passed": bool(passed),
                    "evidence": evidence,
                }
            )

        global_allowed = (
            decision.action != RecoveryAction.REPLAN_GLOBAL.value
            or int(record.global_replans)
            < self.limits.max_full_replans
        )
        add(
            "global_replan_limit",
            global_allowed,
            (
                f"used={record.global_replans}; "
                f"limit={self.limits.max_full_replans}"
            ),
        )
        node_escalations = sum(
            item.get("decision", {}).get("action")
            == RecoveryAction.UPGRADE_NODE.value
            and str(
                item.get("failure", {}).get("task_id", "")
            ).split("-recovery-v", 1)[0]
            == str(task.id).split("-recovery-v", 1)[0]
            for item in record.recovery_history
        )
        node_allowed = (
            decision.action != RecoveryAction.UPGRADE_NODE.value
            or node_escalations < self.limits.max_node_escalations
        )
        add(
            "node_escalation_limit",
            node_allowed,
            (
                f"used={node_escalations}; "
                f"limit={self.limits.max_node_escalations}"
            ),
        )
        high_risk_evidence = (
            task.risk_level != "high"
            or float(failure.confidence) >= 0.75
        )
        add(
            "high_risk_evidence",
            high_risk_evidence,
            (
                f"risk={task.risk_level}; "
                f"failure_confidence={failure.confidence:.3f}"
            ),
        )
        irreversible = bool(
            set(task.requires_tools) & self.IRREVERSIBLE_TOOLS
        ) or task.approval_kind in {
            "irreversible-action",
            "production-change",
            "database-migration",
        }
        requires_approval = bool(task.requires_approval or irreversible)
        approval_ok = (
            not irreversible
            or approval_status in {"approved", None}
        )
        add(
            "irreversible_action_gate",
            approval_ok,
            (
                "a fresh replacement approval will be required"
                if irreversible
                else "no irreversible tool declared"
            ),
        )
        authority_preserved = bool(task.data_permissions)
        add(
            "authority_boundary",
            authority_preserved,
            "replacement must copy, never widen, permissions and tools",
        )
        allowed = (
            bool(decision.allowed)
            and global_allowed
            and node_allowed
            and high_risk_evidence
            and approval_ok
            and authority_preserved
        )
        effective_action = (
            decision.action
            if allowed
            else RecoveryAction.STOP.value
        )
        failed_checks = [
            item["name"] for item in checks if not item["passed"]
        ]
        reason = (
            decision.reason
            if allowed
            else "recovery gate rejected: " + ", ".join(failed_checks)
        )
        return RecoveryGateReceipt(
            allowed=allowed,
            proposed_action=decision.action,
            effective_action=effective_action,
            reason=reason,
            requires_approval=requires_approval,
            authority_preserved=authority_preserved,
            checks=tuple(checks),
        )
