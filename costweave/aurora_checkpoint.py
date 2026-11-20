"""Read terminal Aurora evidence without loading providers or replaying calls."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


CHECKPOINT_VERSION = "aurora-terminal-blockers-1"
FINAL_NODE = "final_assembly"
_REVISION_NODES = ("deployment_plan_v2", "financial_review_v2", "provider_selection_v2", "security_review_v2")
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,95}")


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("invalid identifier in Aurora report")
    return value


def _failure(row: Mapping[str, Any]) -> str | None:
    status = row.get("status")
    if status not in ("completed", "failed", "blocked_budget_preflight", "blocked_budget_hard_cap",
                      "skipped_learned_terminal_failure", "completed_local_deterministic"):
        return None
    diagnostic = str(row.get("diagnostic") or "").lower()
    if row.get("error") == "PromptInjectionError" or "requires a separate security approval" in diagnostic:
        return "authorization_required"
    if status in {"blocked_budget_preflight", "blocked_budget_hard_cap"}:
        return "budget_blocked"
    if status == "skipped_learned_terminal_failure":
        return "prior_failure_skip"
    if status == "failed":
        return "execution_failed"
    return "validation_rejected"


def terminal_checkpoint(
    policy_id: str,
    nodes: Sequence[Mapping[str, Any]],
    *,
    final_accepted: bool | None,
) -> dict[str, Any]:
    """Follow only recorded missing dependencies from the terminal artifact."""
    _identifier(policy_id)
    if not isinstance(nodes, (list, tuple)) or len(nodes) > 128:
        raise ValueError("invalid Aurora node list")
    indexed = {}
    for row in nodes:
        if not isinstance(row, Mapping):
            raise ValueError("invalid Aurora node record")
        node = _identifier(row.get("node_id"))
        if node in indexed:
            raise ValueError("duplicate Aurora node")
        indexed[node] = row
    if final_accepted is not None and not isinstance(final_accepted, bool):
        raise ValueError("invalid final acceptance flag")
    blockers: dict[str, dict[str, Any]] = {}
    unknown: set[str] = set()
    visited: set[str] = set()
    active: set[str] = set()

    def visit(node: str) -> None:
        if node in active:
            unknown.add(node)
            return
        if node in visited:
            return
        visited.add(node)
        row = indexed.get(node, {})
        accepted = row.get("accepted")
        dependencies = row.get("missing_dependencies")
        if row.get("status") == "skipped_missing_dependencies" and accepted is False:
            if not isinstance(dependencies, list) or not dependencies or len(dependencies) > 128:
                unknown.add(node)
                return
            children = sorted({_identifier(dep) for dep in dependencies})
        elif node == "incident_replan" and accepted is True:
            # The provisional command is discarded when its revisions fail.
            children = [rev for rev in _REVISION_NODES if indexed.get(rev, {}).get("accepted") is not True]
            if not any(rev in indexed for rev in _REVISION_NODES):
                children = []
            if not children:
                unknown.add(node)
                return
        else:
            category = _failure(row) if accepted is False else None
            if category is None:
                unknown.add(node)
                return
            checks = row.get("checks")
            failed_checks = []
            if isinstance(checks, Mapping):
                failed_checks = sorted(
                    key for key, passed in checks.items()
                    if isinstance(key, str) and _ID.fullmatch(key) and passed is False
                )
            blockers[node] = {"node_id": node, "reason": category, "failed_checks": failed_checks}
            if category == "authorization_required":
                blockers[node]["automatic_model_fallback_allowed"] = False
            return
        active.add(node)
        for child in children:
            visit(child)
        active.remove(node)

    final = indexed.get(FINAL_NODE, {})
    if final_accepted is True and final.get("accepted") is True and final.get("status") in (
        "completed", "completed_local_deterministic", "reused_prior_checkpoint",
    ):
        status = "accepted"
    elif final_accepted is False and final.get("accepted") is False:
        visit(FINAL_NODE)
        status = "incomplete_evidence" if unknown else "blocked"
    else:
        unknown.add(FINAL_NODE)
        status = "incomplete_evidence"
    return {
        "schema_version": CHECKPOINT_VERSION,
        "policy_id": policy_id,
        "final_node_id": FINAL_NODE,
        "status": status,
        "blockers": [blockers[node] for node in sorted(blockers)],
        "unknown_node_ids": sorted(unknown),
    }


def analyze_report(path: Path) -> dict[str, Any]:
    """Analyze a local result file. No provider, credentials, or ledger access."""
    limit = 32 * 1024 * 1024
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Aurora report exceeds 32 MiB")
    report = json.loads(data)
    policies = report.get("policy_results") if isinstance(report, dict) else None
    if not isinstance(policies, list) or not 1 <= len(policies) <= 128:
        raise ValueError("invalid Aurora policy results")
    results = []
    seen: set[str] = set()
    for policy in policies:
        if not isinstance(policy, Mapping):
            raise ValueError("invalid Aurora policy record")
        policy_id = _identifier(policy.get("policy_id"))
        if policy_id in seen:
            raise ValueError("duplicate Aurora policy")
        seen.add(policy_id)
        accepted = policy.get("accepted")
        if "hard_gates_passed" in policy and policy["hard_gates_passed"] is not accepted:
            accepted = None
        results.append(terminal_checkpoint(policy_id, policy.get("nodes"), final_accepted=accepted))
    return {
        "schema_version": CHECKPOINT_VERSION,
        "report_sha256": hashlib.sha256(data).hexdigest(),
        "provider_calls": 0,
        "policy_checkpoints": results,
    }
