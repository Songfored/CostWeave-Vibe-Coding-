"""Choose a bounded repair from paired, post-checkpoint whole-task outcomes."""

from __future__ import annotations

import math
from graphlib import TopologicalSorter, CycleError
from typing import TYPE_CHECKING, Any, Mapping

from .common import bounded_text, finite_number, stable_digest, unique_texts, validate_exact_fields, validate_sha256

if TYPE_CHECKING:
    from .execution_recovery import ExecutionSnapshotV1


POLICY_REVISION = "paired-final-repair-v1"
REPAIR_ACTIONS = ("RETRY_NODE", "SWITCH_MODEL", "REPLAN_SUBGRAPH", "DIAGNOSE_THEN_RETRY")
LOCAL_DIAGNOSTIC_REVISION = "required-blockers-v1"
_HARD_FAILURES = frozenset({
    "hard_security", "policy_or_safety", "approval_required", "authorization_required",
    "policy_denied", "budget", "budget_exceeded",
})
_REPAIRABLE_FAILURES = frozenset({
    "validator_conflict", "contract_error", "capability_insufficient", "transient", "evidence_failure",
    "truncated", "format", "semantic", "transport",
})


def _array(value: Any, name: str, maximum: int) -> list | tuple:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise ValueError(f"{name} must be an array with at most {maximum} items")
    return value


def _ids(value: Any, name: str) -> list[str]:
    return sorted(unique_texts(_array(value, name, 256), name, maximum_items=256))


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _arm(raw: Mapping[str, Any]) -> dict:
    validate_exact_fields(raw, required=frozenset({"run_id", "state_id", "policy_id", "final_passed", "cost_usd"}))
    if type(raw["final_passed"]) is not bool:
        raise ValueError("final_passed must be a terminal boolean")
    return {
        "run_id": bounded_text(raw["run_id"], "run_id"),
        "state_id": validate_sha256(raw["state_id"], "state_id"),
        "policy_id": bounded_text(raw["policy_id"], "policy_id"),
        "final_passed": raw["final_passed"],
        "cost_usd": finite_number(raw["cost_usd"], "post-checkpoint cost_usd", minimum=0),
    }


def parse_repair_request(request: Mapping[str, Any]) -> dict:
    validate_exact_fields(request, required=frozenset({
        "checkpoint_sha256", "validator_revision", "baseline_policy_id", "final_node_ids", "remaining_actions",
        "authorized_plan_ids", "plans", "pairs",
    }))
    finals = _ids(request["final_node_ids"], "final_node_ids")
    if not finals:
        raise ValueError("at least one required final node is needed")
    baseline = bounded_text(request["baseline_policy_id"], "baseline_policy_id")
    plans: dict[str, dict] = {}
    for plan in _array(request["plans"], "plans", 64):
        validate_exact_fields(plan, required=frozenset({
            "plan_id", "action", "target_node_ids", "max_cost_usd", "max_latency_ms", "action_count",
        }), optional=frozenset({"diagnostic_revision"}))
        key = bounded_text(plan["plan_id"], "plan_id")
        if key in plans or key == baseline or plan["action"] not in REPAIR_ACTIONS:
            raise ValueError("repair plans need distinct IDs and a supported action")
        targets = _ids(plan["target_node_ids"], "target_node_ids")
        count = _integer(plan["action_count"], "action_count", 1)
        if not targets or (plan["action"] != "REPLAN_SUBGRAPH" and count < len(targets)):
            raise ValueError("a node retry or switch requires one action per target")
        plans[key] = {
            "plan_id": key, "action": plan["action"], "target_node_ids": targets, "action_count": count,
            "max_cost_usd": finite_number(plan["max_cost_usd"], "max_cost_usd", minimum=0),
            "max_latency_ms": _integer(plan["max_latency_ms"], "max_latency_ms"),
        }
        if plan["action"] == "DIAGNOSE_THEN_RETRY":
            plans[key]["diagnostic_revision"] = bounded_text(plan.get("diagnostic_revision"), "diagnostic_revision")
        elif "diagnostic_revision" in plan:
            raise ValueError("diagnostic_revision belongs to DIAGNOSE_THEN_RETRY")
    allowed = _ids(request["authorized_plan_ids"], "authorized_plan_ids")
    if set(allowed) - plans.keys():
        raise ValueError("authorized_plan_ids contains an unknown plan")
    pairs = {}
    seen_arms: set[str] = set()
    for pair in _array(request["pairs"], "pairs", 512):
        validate_exact_fields(pair, required=frozenset({"pair_id", "baseline", "candidate"}))
        pair_id = bounded_text(pair["pair_id"], "pair_id")
        base, candidate = _arm(pair["baseline"]), _arm(pair["candidate"])
        run_ids = {base["run_id"], candidate["run_id"]}
        if pair_id in pairs or len(run_ids) != 2 or seen_arms.intersection(run_ids):
            raise ValueError("each paired trial needs distinct run and pair IDs")
        seen_arms.update(run_ids)
        if base["policy_id"] != baseline or candidate["policy_id"] not in plans:
            raise ValueError("observations must refer to the registered baseline and candidate")
        pairs[pair_id] = {"pair_id": pair_id, "baseline": base, "candidate": candidate}
    return {
        "checkpoint_sha256": validate_sha256(request["checkpoint_sha256"], "checkpoint_sha256"),
        "validator_revision": bounded_text(request["validator_revision"], "validator_revision"),
        "baseline_policy_id": baseline, "final_node_ids": finals,
        "remaining_actions": _integer(request["remaining_actions"], "remaining_actions"),
        "authorized_plan_ids": allowed, "plans": [plans[k] for k in sorted(plans)],
        "pairs": [pairs[k] for k in sorted(pairs)],
    }


def _state_id(snapshot: ExecutionSnapshotV1, request: dict) -> str:
    state = snapshot.to_dict()
    # Evidence from forks is added after freezing. Wall clock and receipt IDs
    # are bookkeeping; the actual upstream artifacts and validator are pinned.
    for key in ("observed_at", "evidence_ids", "repair_allocation"):
        state.pop(key, None)
    contract = {key: value for key, value in request.items() if key != "pairs"}
    return stable_digest({"revision": POLICY_REVISION, "snapshot": state, "contract": contract}, length=64)


def repair_state_id(snapshot: ExecutionSnapshotV1, request: Mapping[str, Any]) -> str:
    """Identity to record on each arm before running the final validator."""
    return _state_id(snapshot, parse_repair_request(request))


def _blockers(snapshot: ExecutionSnapshotV1, finals: list[str]) -> set[str]:
    nodes = {node.node_id: node for node in snapshot.nodes}
    if set(finals) - nodes.keys():
        raise ValueError("final_node_ids must name nodes in the snapshot")
    parents = {
        node.node_id: [p for p in node.dependencies if node.dependency_types.get(p, "hard") != "soft"]
        for node in snapshot.nodes
    }
    try:
        order = tuple(TopologicalSorter(parents).static_order())
    except CycleError as exc:
        raise ValueError("cycle in required execution dependencies") from exc
    causes: dict[str, set[str]] = {}
    polluted = set(snapshot.polluted_node_ids)
    for key in order:
        inherited: set[str] = set()
        for parent in parents[key]:
            inherited.update(causes[parent])
        status = nodes[key].status
        if status in {"rejected", "suspect"}:
            inherited.add(key)
        elif not inherited and (status in {"blocked", "skipped"} or key in polluted):
            inherited.add(key)
        causes[key] = inherited
    return set().union(*(causes[key] for key in finals))


def _constraints(snapshot, request, plan, blockers, nodes) -> list[str]:
    targets = set(plan["target_node_ids"])
    if targets - nodes.keys():
        raise ValueError("unknown repair target node")
    action = plan["action"]
    remaining = {"RETRY_NODE": snapshot.max_retries, "DIAGNOSE_THEN_RETRY": snapshot.max_retries,
                 "SWITCH_MODEL": snapshot.max_switches,
                 "REPLAN_SUBGRAPH": snapshot.max_subgraph_replans}[action]
    reasons = []
    if plan["plan_id"] not in request["authorized_plan_ids"]:
        reasons.append("plan_not_authorized")
    if action == "DIAGNOSE_THEN_RETRY" and plan["diagnostic_revision"] != LOCAL_DIAGNOSTIC_REVISION:
        reasons.append("unregistered_local_diagnostic")
    if targets != blockers:
        reasons.append("plan_does_not_cover_exact_required_blockers")
    if plan["action_count"] > min(remaining, request["remaining_actions"]):
        reasons.append("insufficient_actions")
    if plan["max_cost_usd"] > snapshot.budget_remaining_usd:
        reasons.append("budget_exceeded")
    if plan["max_latency_ms"] > snapshot.deadline_remaining_ms:
        reasons.append("deadline_exceeded")
    if not snapshot.resource.provider_available or snapshot.resource.available_slots == 0:
        reasons.append("resource_unavailable")
    if any(nodes[key].attempts >= nodes[key].max_attempts for key in targets):
        reasons.append("attempt_limit")
    if action == "SWITCH_MODEL" and not (
        snapshot.resource.alternate_route_available and snapshot.resource.orthogonal_validator_available
        and all(nodes[key].alternate_executor_available for key in targets)
    ):
        reasons.append("alternate_executor_or_validator_unavailable")
    return reasons


def diagnose_required_blockers(snapshot: ExecutionSnapshotV1, final_node_ids: list[str]) -> dict:
    """Inspect typed snapshot fields locally. No prompts, plugins, or model calls."""
    blockers = _blockers(snapshot, _ids(final_node_ids, "final_node_ids"))
    failures = {}
    unknown = []
    hard = []
    for node in snapshot.nodes:
        if node.node_id not in blockers:
            continue
        labels = {node.failure_class} if node.failure_class else set()
        if node.node_id == snapshot.failure_node_id and snapshot.failure_class:
            labels.add(snapshot.failure_class)
        failures[node.node_id] = {
            "status": node.status, "failure_classes": sorted(labels), "validator_family": node.validator_family,
            "validation_score": node.validation_score, "remaining_attempts": node.max_attempts - node.attempts,
        }
        if labels & _HARD_FAILURES:
            hard.append(node.node_id)
        elif not labels or labels - _REPAIRABLE_FAILURES:
            unknown.append(node.node_id)
    return {
        "revision": LOCAL_DIAGNOSTIC_REVISION, "blocking_node_ids": sorted(blockers), "failed_checks": failures,
        "non_repairable_nodes": hard, "unclassified_nodes": unknown, "provider_calls": 0, "cost_usd": 0.0,
        "hard_stop": bool(hard or snapshot.failure_class in _HARD_FAILURES),
        "missing_information": list(snapshot.required_info_missing),
    }


def select_final_repair(snapshot: ExecutionSnapshotV1, request: Mapping[str, Any]) -> dict:
    """Rank only bounded commitments supported by paired final outcomes.

    Each arm's cost is its complete remaining-path USD spend, including failed
    attempts. The plan quote bounds the candidate's remaining-path spend.
    With independent pairs, Hoeffding gives a one-sided bound on the mean
    utility gain (pass reward less cost). Bonferroni covers plan selection.
    This function executes no action or provider call.
    """
    req = parse_repair_request(request)
    state = _state_id(snapshot, req)
    nodes = {node.node_id: node for node in snapshot.nodes}
    diagnosis = diagnose_required_blockers(snapshot, req["final_node_ids"])
    blockers = set(diagnosis["blocking_node_ids"])
    candidates = []
    for plan in req["plans"]:
        reasons = _constraints(snapshot, req, plan, blockers, nodes)
        if diagnosis["hard_stop"]:
            reasons.append("failure_disallows_repair")
        if diagnosis["unclassified_nodes"] or snapshot.required_info_missing:
            reasons.append("failure_requires_diagnosis")
        observed = [pair for pair in req["pairs"] if pair["candidate"]["policy_id"] == plan["plan_id"]]
        pairs = [pair for pair in observed if pair["baseline"]["state_id"] == pair["candidate"]["state_id"] == state]
        count = len(pairs)
        gain = incremental_cost = lower = candidate_rate = baseline_rate = None
        if count:
            candidate_rate = sum(p["candidate"]["final_passed"] for p in pairs) / count
            baseline_rate = sum(p["baseline"]["final_passed"] for p in pairs) / count
            gain = candidate_rate - baseline_rate
            incremental_cost = math.fsum(p["candidate"]["cost_usd"] - p["baseline"]["cost_usd"] for p in pairs) / count
            # Clip baseline savings to the quote. The resulting per-pair net
            # utility in [-risk-quote, risk+quote] lower-bounds its true value.
            quote = plan["max_cost_usd"]
            clipped_cost_delta = math.fsum(
                p["candidate"]["cost_usd"] - min(p["baseline"]["cost_usd"], quote) for p in pairs
            ) / count
            radius = (snapshot.risk_loss_usd + quote) * math.sqrt(2 * math.log(20 * len(req["plans"])) / count)
            lower = snapshot.risk_loss_usd * gain - clipped_cost_delta - radius
            if any(p["candidate"]["cost_usd"] > plan["max_cost_usd"] for p in pairs):
                reasons.append("observed_cost_exceeds_quote")
            if gain <= 0:
                reasons.append("no_observed_final_pass_gain")
            if lower <= 0:
                reasons.append("positive_net_gain_unestablished")
        else:
            reasons.append("no_matching_pairs")
        used = {p["pair_id"] for p in pairs}
        candidates.append({
            **plan, "eligible": not reasons, "reason_codes": reasons, "pair_ids": [p["pair_id"] for p in pairs],
            "excluded_pair_ids": [p["pair_id"] for p in observed if p["pair_id"] not in used],
            "paired_runs": pairs, "candidate_final_pass_rate": candidate_rate, "baseline_final_pass_rate": baseline_rate,
            "paired_final_pass_gain": gain, "mean_incremental_cost_usd": incremental_cost,
            "net_gain_lower_usd": lower,
        })
    allowed = [p for p in candidates if p["eligible"]]
    choice = None
    if diagnosis["hard_stop"]:
        action, reason = "STOP_AND_RETURN_PARTIAL", "failure_disallows_repair"
    elif snapshot.required_info_missing:
        action, reason = "REQUEST_MISSING_INFO", "required_information_missing"
    elif diagnosis["unclassified_nodes"]:
        action, reason = "REQUEST_MISSING_INFO", "failure_requires_diagnosis"
    elif not blockers:
        action, reason = "CONTINUE", "no_required_failure"
    elif allowed:
        choice = min(allowed, key=lambda p: (-p["net_gain_lower_usd"], p["mean_incremental_cost_usd"], p["plan_id"]))
        action, reason = choice["action"], "paired_final_gain_within_limits"
    elif any(set(p["reason_codes"]) <= {"no_matching_pairs", "positive_net_gain_unestablished"} for p in candidates):
        action, reason = "REQUEST_MISSING_INFO", "insufficient_paired_evidence"
    else:
        action, reason = "STOP_AND_RETURN_PARTIAL", "no_authorized_feasible_repair"
    return {
        "policy_revision": POLICY_REVISION, "state_id": state, "checkpoint_sha256": req["checkpoint_sha256"],
        "validator_revision": req["validator_revision"], "baseline_policy_id": req["baseline_policy_id"],
        "blockers": sorted(blockers), "final_node_ids": req["final_node_ids"], "candidates": candidates,
        "action": action, "selected_plan_id": choice["plan_id"] if choice else None,
        "reason_codes": [reason], "advisory_only": True,
        "diagnosis": ({
            **diagnosis,
            "missing_paired_evidence_for": [
                p["plan_id"] for p in candidates if set(p["reason_codes"]) <= {
                    "no_matching_pairs", "positive_net_gain_unestablished"
                } and not p["eligible"]
            ],
        } if action in {"DIAGNOSE_THEN_RETRY", "REQUEST_MISSING_INFO", "STOP_AND_RETURN_PARTIAL"} else None),
    }
