"""Unified, provenance-first, project-scoped evidence views for v0.5.8.3."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Mapping

from .canary_control import public_canary_policy
from .contracts_v4 import utc_now
from .evaluation_protocol import (
    EVALUATION_PROTOCOL_VERSION,
    SEED_TASK_SET_VERSION,
    load_seed_tasks,
)
from .event_store import RUNTIME_SCHEMA_VERSION
from .offline_replay import public_offline_replay_policy
from .outcome_dataset import OUTCOME_DATASET_SCHEMA_VERSION
from .planning_contracts import PLANNING_POLICY_VERSION, PLANNING_SCHEMA_VERSION
from .policy_registry import public_policy_registry_policy
from .production_shadow import public_production_shadow_policy
from .security_boundary import (
    DEFAULT_DATA_RESIDENCY,
    DEFAULT_PROJECT_ID,
    normalize_data_residency,
    normalize_project_id,
    public_security_policy,
    redact_structure,
)
from .resilience import public_resilience_policy


EVIDENCE_CONSOLE_SCHEMA_VERSION = "v0.5.8.3"
EVIDENCE_EXPORT_SCHEMA_VERSION = "v0.5.8.1-export"
EVIDENCE_TAXONOMY_VERSION = "evidence-kinds-v1"
EVIDENCE_KINDS = ("actual", "predicted", "simulated", "missing")
EVIDENCE_LEVELS = ("E0", "E1", "E2", "E3", "E4", "E5")
MAX_EXPORTED_EVENTS = 1_000
MAX_EXPORTED_RUNS = 20


class EvidenceConsoleError(ValueError):
    """Raised when an evidence view or package violates its contract."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _scoped_static_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove process-global counters before embedding policy metadata."""

    return {
        str(key): item
        for key, item in value.items()
        if str(key) != "runtime_counts"
    }


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in {float("inf"), float("-inf")}:
        return None
    return result


def _source(
    source_id: str,
    label: str,
    *,
    evidence_kind: str,
    evidence_level: str,
    version: str,
    api_path: str,
    record_count: int,
    captured_at: str,
    definition: str,
    latest_record_at: str | None = None,
) -> dict[str, Any]:
    if evidence_kind not in EVIDENCE_KINDS:
        raise EvidenceConsoleError("unsupported evidence kind")
    if evidence_level not in EVIDENCE_LEVELS:
        raise EvidenceConsoleError("unsupported evidence level")
    item = {
        "source_id": source_id,
        "label": label,
        "evidence_kind": evidence_kind,
        "evidence_level": evidence_level,
        "data_version": version,
        "record_count": _safe_int(record_count),
        "trace": {
            "api_path": api_path,
            "record_type": source_id,
            "captured_at": captured_at,
            "latest_record_at": latest_record_at,
        },
        "definition": definition,
    }
    item["source_hash"] = _digest(item)
    return item


def _metric(
    metric_id: str,
    label: str,
    value: Any,
    *,
    unit: str,
    evidence_kind: str,
    evidence_level: str,
    source_id: str,
    sample_size: int,
    task_families: list[str] | tuple[str, ...],
    definition: str,
    confidence_interval: Mapping[str, Any] | None = None,
    claim_eligible: bool = False,
    missing_reason: str | None = None,
) -> dict[str, Any]:
    if evidence_kind not in EVIDENCE_KINDS:
        raise EvidenceConsoleError("unsupported metric evidence kind")
    if evidence_level not in EVIDENCE_LEVELS:
        raise EvidenceConsoleError("unsupported metric evidence level")
    if evidence_kind == "missing" and value is not None:
        raise EvidenceConsoleError("missing metrics cannot contain a value")
    if claim_eligible and evidence_level in {"E0", "E1"}:
        raise EvidenceConsoleError("E0/E1 metrics cannot support commercial claims")
    return {
        "metric_id": metric_id,
        "label": label,
        "value": value,
        "unit": unit,
        "evidence_kind": evidence_kind,
        "evidence_level": evidence_level,
        "source_id": source_id,
        "sample_size": _safe_int(sample_size),
        "confidence_interval": (
            dict(confidence_interval) if confidence_interval is not None else None
        ),
        "applicable_task_families": sorted(
            {str(item) for item in task_families if str(item).strip()}
        ),
        "definition": definition,
        "claim_eligible": bool(claim_eligible),
        "missing_reason": missing_reason,
    }


def _task_families(run: Mapping[str, Any] | None) -> list[str]:
    if not run:
        return []
    analysis = ((run.get("plan") or {}).get("analysis") or {})
    values = analysis.get("task_types") or []
    return sorted({str(item) for item in values if str(item).strip()})


def _latest_event_at(events: list[dict[str, Any]]) -> str | None:
    values = [
        str(item.get("occurred_at") or item.get("at") or "")
        for item in events
        if item.get("occurred_at") or item.get("at")
    ]
    return max(values) if values else None


def _record_project(record: Any) -> str:
    request = getattr(record, "request", None)
    return normalize_project_id(
        getattr(request, "project_id", DEFAULT_PROJECT_ID)
    )


def _record_residency(record: Any) -> str:
    request = getattr(record, "request", None)
    return normalize_data_residency(
        getattr(request, "data_residency", DEFAULT_DATA_RESIDENCY)
    )


def _scoped_get(
    manager: Any,
    run_id: str,
    project_id: str,
    data_residency: str,
) -> Any:
    try:
        return manager.get(
            run_id,
            project_id=project_id,
            data_residency=data_residency,
        )
    except TypeError:
        record = manager.get(run_id)
        if record is not None:
            if _record_project(record) != project_id:
                return None
            if _record_residency(record) != data_residency:
                return None
        return record


def _scoped_list(
    manager: Any,
    project_id: str,
    data_residency: str,
) -> list[dict[str, Any]]:
    try:
        return list(
            manager.list(
                project_id=project_id,
                data_residency=data_residency,
            )
        )
    except TypeError:
        result = []
        for item in manager.list():
            request = item.get("request") or {}
            item_project = normalize_project_id(
                request.get("project_id", DEFAULT_PROJECT_ID)
            )
            item_residency = normalize_data_residency(
                request.get("data_residency", DEFAULT_DATA_RESIDENCY)
            )
            if (
                item_project == project_id
                and item_residency == data_residency
            ):
                result.append(item)
        return result


def _scoped_ledger(
    manager: Any,
    project_id: str,
    data_residency: str,
    *,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    try:
        return list(
            manager.ledger(
                run_id=run_id,
                project_id=project_id,
                data_residency=data_residency,
            )
        )
    except TypeError:
        allowed = {
            str(item.get("id"))
            for item in _scoped_list(manager, project_id, data_residency)
        }
        if run_id:
            if run_id not in allowed:
                return []
            return list(manager.ledger(run_id=run_id))
        return [
            item for item in manager.ledger()
            if str(item.get("run_id")) in allowed
        ]


def build_evidence_console(
    manager: Any,
    *,
    run_id: str | None = None,
    project_id: str = DEFAULT_PROJECT_ID,
    data_residency: str = DEFAULT_DATA_RESIDENCY,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a compact evidence snapshot without creating a second fact store."""

    captured_at = generated_at or utc_now()
    project = normalize_project_id(project_id)
    residency = normalize_data_residency(data_residency)
    record = (
        _scoped_get(manager, run_id, project, residency)
        if run_id
        else None
    )
    if run_id and record is None:
        raise KeyError(run_id)
    run = record.snapshot() if record is not None else None
    events = (
        manager.events(run_id, after_revision=0, limit=MAX_EXPORTED_EVENTS)
        if run_id
        else []
    )
    scoped_runs = _scoped_list(manager, project, residency)
    run_count = len(scoped_runs)
    event_count = (
        len(events)
        if run_id
        else sum(
            len(
                manager.events(
                    str(item.get("id") or ""),
                    after_revision=0,
                    limit=MAX_EXPORTED_EVENTS,
                )
            )
            for item in scoped_runs
            if item.get("id")
        )
    )
    external_attempts = None
    task_families = _task_families(run)
    planning = (run or {}).get("planning") or {}
    decision = planning.get("decision") or {}
    plan = (run or {}).get("plan") or {}
    routing = plan.get("routing_summary") or {}
    metrics_block = (run or {}).get("metrics") or {}
    policy_snapshot = ((run or {}).get("runtime") or {}).get("policy_snapshot")
    ledger_entries = _scoped_ledger(
        manager,
        project,
        residency,
        run_id=run_id,
    )
    placements = manager.placements(run_id) if run_id else []
    resources = manager.resources()
    actual_cost = round(
        sum(
            float(item.get("amount") or 0.0)
            for item in ledger_entries
            if str(item.get("phase") or "") == "actual"
        ),
        8,
    )
    latest_at = _latest_event_at(events) or (
        str((run or {}).get("finished_at") or (run or {}).get("created_at") or "")
        or None
    )

    sources = [
        _source(
            "runtime_events",
            "运行投影与事件流",
            evidence_kind="actual",
            evidence_level="E1",
            version=RUNTIME_SCHEMA_VERSION,
            api_path=(
                f"/api/v1/runs/{run_id}/events" if run_id else "/api/v1/runs"
            ),
            record_count=(len(events) if run_id else event_count),
            captured_at=captured_at,
            latest_record_at=latest_at,
            definition="CostWeave 实际写入的控制平面状态；不等于外部模型效果。",
        ),
        _source(
            "planning_receipts",
            "规划与 Planner 回执",
            evidence_kind="actual",
            evidence_level="E1",
            version=PLANNING_POLICY_VERSION,
            api_path=(
                f"/api/v1/runs/{run_id}/planning" if run_id else "/api/v1/planning/policy"
            ),
            record_count=(1 if decision else 0),
            captured_at=captured_at,
            latest_record_at=latest_at,
            definition="实际采用的规划模式、面板和裁决回执；成功概率仍是预测值。",
        ),
        _source(
            "cost_ledger",
            "成本与 Placement 账本",
            evidence_kind="simulated",
            evidence_level="E1",
            version="placement-ledger-v1",
            api_path=(
                f"/api/v1/resources/ledger?run_id={run_id}"
                if run_id
                else "/api/v1/resources/ledger"
            ),
            record_count=len(ledger_entries),
            captured_at=captured_at,
            latest_record_at=latest_at,
            definition="当前执行器写入的控制账本；未与 Provider 账单对账时按模拟值展示。",
        ),
        _source(
            "routing_placements",
            "路由与 Placement 回执",
            evidence_kind="actual",
            evidence_level="E1",
            version="placement-contract-v1",
            api_path=(
                f"/api/v1/runs/{run_id}/placements"
                if run_id
                else "/api/v1/resources"
            ),
            record_count=len(placements),
            captured_at=captured_at,
            latest_record_at=latest_at,
            definition="实际写入控制平面的模型与资源池选择；不证明外部模型已执行。",
        ),
        _source(
            "resource_capacity",
            "资源容量快照",
            evidence_kind="simulated",
            evidence_level="E0",
            version="resource-inventory-v1",
            api_path="/api/v1/resources",
            record_count=len(resources),
            captured_at=captured_at,
            definition="当前由目录派生的容量快照，不是真实 CPU、GPU、显存或 Provider 遥测。",
        ),
        _source(
            "outcome_records",
            "Outcome 与独立验收记录",
            evidence_kind="simulated",
            evidence_level="E1",
            version=OUTCOME_DATASET_SCHEMA_VERSION,
            api_path="/api/v1/outcomes/governance",
            record_count=0,
            captured_at=captured_at,
            definition="默认运行来自模拟执行器；真实标签不足时不能支持效果结论。",
        ),
        _source(
            "evaluation_protocol",
            "冻结任务集与基线评测",
            evidence_kind="simulated",
            evidence_level="E0",
            version=f"{EVALUATION_PROTOCOL_VERSION}/{SEED_TASK_SET_VERSION}",
            api_path="/api/v1/evaluation/protocol",
            record_count=len(load_seed_tasks()),
            captured_at=captured_at,
            definition="确定性 fixture 评测，只验证协议和公式，不代表真实模型表现。",
        ),
        _source(
            "production_shadow",
            "Production-like Shadow",
            evidence_kind="actual",
            evidence_level="E1",
            version=public_production_shadow_policy()["schema_version"],
            api_path="/api/v1/shadow/policy",
            record_count=0,
            captured_at=captured_at,
            definition="旁路控制记录的实际数量；只有明确的真实数据来源才能升级证据等级。",
        ),
        _source(
            "policy_registry",
            "签名策略注册表",
            evidence_kind="actual",
            evidence_level="E1",
            version=public_policy_registry_policy()["schema_version"],
            api_path="/api/v1/policies/registry",
            record_count=0,
            captured_at=captured_at,
            definition="实际注册、门报告和状态机记录；注册表 Active 不等于真实路由 Active。",
        ),
        _source(
            "bounded_canary",
            "有界 Canary 与回滚",
            evidence_kind="actual",
            evidence_level="E1",
            version=public_canary_policy()["schema_version"],
            api_path="/api/v1/canary/policy",
            record_count=0,
            captured_at=captured_at,
            definition="运行时策略快照和回滚的控制记录；候选选择不等于 Provider 调用。",
        ),
        _source(
            "security_boundary",
            "安全边界与项目隔离",
            evidence_kind="actual",
            evidence_level="E1",
            version=public_security_policy()["schema_version"],
            api_path="/api/v1/security/policy",
            record_count=0,
            captured_at=captured_at,
            definition="项目隔离、密钥租约、网络许可和外部副作用合同的控制面记录。",
        ),
        _source(
            "resilience_campaigns",
            "Stress、Fault 与 Migration 活动",
            evidence_kind="simulated",
            evidence_level="E0",
            version=public_resilience_policy()["schema_version"],
            api_path="/api/v1/resilience/status",
            record_count=0,
            captured_at=captured_at,
            definition=(
                "离线故障注入、压力与恢复记录；默认不触发真实 Provider，"
                "不能替代生产负载证据。"
            ),
        ),
    ]

    metrics = [
        _metric(
            "recorded_runs",
            "已记录运行",
            run_count,
            unit="runs",
            evidence_kind="actual",
            evidence_level="E1",
            source_id="runtime_events",
            sample_size=run_count,
            task_families=task_families,
            definition="当前权威运行投影中的运行数量。",
        ),
        _metric(
            "external_provider_attempts",
            "外部 Provider 尝试",
            external_attempts,
            unit="attempts",
            evidence_kind="missing",
            evidence_level="E0",
            source_id="runtime_events",
            sample_size=0,
            task_families=task_families,
            definition="当前请求范围内受预算保护的外部 Adapter 尝试数。",
            missing_reason="外部尝试账本尚未提供可证明的项目与驻留区联合范围计数。",
        ),
        _metric(
            "fixture_evaluation_cases",
            "模拟评测案例",
            len(load_seed_tasks()),
            unit="cases",
            evidence_kind="simulated",
            evidence_level="E0",
            source_id="evaluation_protocol",
            sample_size=len(load_seed_tasks()),
            task_families=[],
            definition="冻结 fixture 任务数，不计入真实商业样本。",
        ),
        _metric(
            "resource_pools",
            "容量资源池",
            len(resources),
            unit="pools",
            evidence_kind="simulated",
            evidence_level="E0",
            source_id="resource_capacity",
            sample_size=len(resources),
            task_families=task_families,
            definition="目录派生的资源池数量；尚未连接实时硬件或 Provider 容量遥测。",
        ),
        _metric(
            "verified_success_rate",
            "真实验收成功率",
            None,
            unit="percent",
            evidence_kind="missing",
            evidence_level="E0",
            source_id="outcome_records",
            sample_size=0,
            task_families=[],
            definition="通过预注册硬验收条件的真实任务数除以全部合格真实任务数。",
            missing_reason="尚无满足 E2 门槛并完成独立验收的真实任务样本。",
        ),
        _metric(
            "cost_per_accepted_task",
            "真实 CPS",
            None,
            unit="USD/accepted-task",
            evidence_kind="missing",
            evidence_level="E0",
            source_id="cost_ledger",
            sample_size=0,
            task_families=[],
            definition="规划、执行、验证、工具、重试、升级和返工总成本除以真实验收成功数。",
            missing_reason="当前账本没有与真实 Provider 账单和真实验收结果完成对账。",
        ),
        _metric(
            "time_to_accepted_result_p95",
            "真实完成时延 P95",
            None,
            unit="milliseconds",
            evidence_kind="missing",
            evidence_level="E0",
            source_id="runtime_events",
            sample_size=0,
            task_families=[],
            definition="从真实任务进入到最终验收完成的端到端 P95。",
            missing_reason="尚无足量真实端到端任务形成可报告分位数。",
        ),
        _metric(
            "resilience_campaigns",
            "离线韧性活动",
            None,
            unit="campaigns",
            evidence_kind="missing",
            evidence_level="E0",
            source_id="resilience_campaigns",
            sample_size=0,
            task_families=[],
            definition=(
                "持久化的离线压力、故障、迁移和恢复活动数量；"
                "不计入真实效果样本。"
            ),
            missing_reason="韧性存储当前仅支持本地项目维度，未提供驻留区联合范围计数。",
        ),
    ]

    if run is not None:
        panel = decision.get("panel_policy") or {}
        run_status = str(run.get("status") or "unknown")
        metrics.extend(
            [
                _metric(
                    "planning_mode",
                    "本次规划模式",
                    decision.get("mode") or "unavailable",
                    unit="mode",
                    evidence_kind="actual",
                    evidence_level="E1",
                    source_id="planning_receipts",
                    sample_size=1 if decision else 0,
                    task_families=task_families,
                    definition="该运行实际采用的 Planner / MultiPlanner 决定。",
                ),
                _metric(
                    "planner_panel_size",
                    "Planner 面板规模",
                    _safe_int(panel.get("size")),
                    unit="roles",
                    evidence_kind="actual",
                    evidence_level="E1",
                    source_id="planning_receipts",
                    sample_size=1 if decision else 0,
                    task_families=task_families,
                    definition="本次规划实际选择的互补角色数量。",
                ),
                _metric(
                    "estimated_total_cost_usd",
                    "预计总成本",
                    _safe_float(routing.get("estimated_total_cost_usd")),
                    unit="USD",
                    evidence_kind="predicted",
                    evidence_level="E0",
                    source_id="planning_receipts",
                    sample_size=1 if routing else 0,
                    task_families=task_families,
                    definition="路由前根据目录、任务图和资源约束产生的成本预测。",
                ),
                _metric(
                    "routing_strategy",
                    "本次路由策略",
                    routing.get("strategy") or "unavailable",
                    unit="strategy",
                    evidence_kind="actual",
                    evidence_level="E1",
                    source_id="routing_placements",
                    sample_size=1 if routing else 0,
                    task_families=task_families,
                    definition="该运行实际写入计划的路由策略标识。",
                ),
                _metric(
                    "selected_placements",
                    "已选 Placement",
                    sum(str(item.get("status")) == "selected" for item in placements),
                    unit="placements",
                    evidence_kind="actual",
                    evidence_level="E1",
                    source_id="routing_placements",
                    sample_size=len(placements),
                    task_families=task_families,
                    definition="实际写入控制平面的已选模型/部署/资源池回执数量。",
                ),
                _metric(
                    "critical_path_p95_ms",
                    "预计关键路径 P95",
                    _safe_float(routing.get("critical_path_p95_ms")),
                    unit="milliseconds",
                    evidence_kind="predicted",
                    evidence_level="E0",
                    source_id="planning_receipts",
                    sample_size=1 if routing else 0,
                    task_families=task_families,
                    definition="路由时对 DAG 关键路径的启发式 P95 预测。",
                ),
                _metric(
                    "success_probability",
                    "预计成功概率",
                    _safe_float(metrics_block.get("success_probability")),
                    unit="probability",
                    evidence_kind="predicted",
                    evidence_level="E0",
                    source_id="outcome_records",
                    sample_size=1,
                    task_families=task_families,
                    definition="执行中由启发式证据更新的完成概率，不是真实成功率。",
                ),
                _metric(
                    "simulated_spend",
                    "模拟执行成本",
                    actual_cost,
                    unit="USD",
                    evidence_kind="simulated",
                    evidence_level="E1",
                    source_id="cost_ledger",
                    sample_size=len(ledger_entries),
                    task_families=task_families,
                    definition="离线模拟执行器写入 actual phase 的控制账本金额，未与 Provider 账单对账。",
                ),
                _metric(
                    "validated_tasks",
                    "模拟验收任务",
                    _safe_int(metrics_block.get("validated_tasks")),
                    unit="tasks",
                    evidence_kind="simulated",
                    evidence_level="E1",
                    source_id="outcome_records",
                    sample_size=_safe_int(metrics_block.get("total_tasks")),
                    task_families=task_families,
                    definition="默认模拟执行器中通过验证合同的节点数。",
                ),
                _metric(
                    "orchestrator_duration_ms",
                    "控制平面用时",
                    _safe_float(metrics_block.get("duration_ms")),
                    unit="milliseconds",
                    evidence_kind="actual",
                    evidence_level="E1",
                    source_id="runtime_events",
                    sample_size=1 if run_status in {"completed", "failed", "cancelled"} else 0,
                    task_families=task_families,
                    definition="该离线运行在本机控制平面实际经过的墙钟时间，不代表外部模型时延。",
                ),
            ]
        )

    kind_counts = Counter(item["evidence_kind"] for item in metrics)
    coverage = {kind: kind_counts.get(kind, 0) for kind in EVIDENCE_KINDS}
    business_claims = [
        {
            "claim_id": "verified_quality_improvement",
            "label": "真实质量提升",
            "status": "not_supported",
            "evidence_level": "E0",
            "sample_size": 0,
            "confidence_interval": None,
            "applicable_task_families": [],
            "reason": "没有满足 E2+ 的独立真实验收样本。",
        },
        {
            "claim_id": "verified_cost_reduction",
            "label": "真实成本降低",
            "status": "not_supported",
            "evidence_level": "E0",
            "sample_size": 0,
            "confidence_interval": None,
            "applicable_task_families": [],
            "reason": "没有完成真实账单、验收结果和基线的同口径对账。",
        },
        {
            "claim_id": "verified_latency_improvement",
            "label": "真实响应提速",
            "status": "not_supported",
            "evidence_level": "E0",
            "sample_size": 0,
            "confidence_interval": None,
            "applicable_task_families": [],
            "reason": "没有足量真实端到端样本和置信区间。",
        },
    ]
    result = {
        "schema_version": EVIDENCE_CONSOLE_SCHEMA_VERSION,
        "taxonomy_version": EVIDENCE_TAXONOMY_VERSION,
        "generated_at": captured_at,
        "scope": {
            "kind": "run" if run_id else "workspace",
            "run_id": run_id,
            "project_id": project,
            "data_residency": residency,
        },
        "summary": {
            "evidence_level": "E0",
            "commercial_claims_allowed": False,
            "real_effect_sample_size": 0,
            "confidence_intervals_available": 0,
            "applicable_real_task_families": [],
            "external_provider_attempts": external_attempts,
            "external_provider_attempts_scope_available": False,
            "policy_snapshot": policy_snapshot,
            "message": "控制机制可验证；真实质量、成本与时延收益仍缺少 E2+ 证据。",
        },
        "coverage": coverage,
        "metrics": metrics,
        "business_claims": business_claims,
        "sources": sources,
        "controls": {
            "planning_schema": PLANNING_SCHEMA_VERSION,
            "offline_replay": _scoped_static_policy(
                public_offline_replay_policy()
            ),
            "production_shadow": {
                **_scoped_static_policy(public_production_shadow_policy()),
                "scope_counts_available": False,
            },
            "policy_registry": {
                **_scoped_static_policy(public_policy_registry_policy()),
                "scope_counts_available": False,
            },
            "bounded_canary": {
                **_scoped_static_policy(public_canary_policy()),
                "scope_counts_available": False,
            },
            "security_boundary": {
                **_scoped_static_policy(public_security_policy()),
                "scope_counts_available": False,
            },
            "resilience": {
                **_scoped_static_policy(public_resilience_policy()),
                "scope_counts_available": False,
            },
        },
    }
    result["snapshot_hash"] = _digest(result)
    return result


def _redact_secrets(value: Any) -> Any:
    return redact_structure(value)


def _record_count(records: Mapping[str, Any]) -> int:
    total = 0
    for value in records.values():
        if value is None:
            continue
        if isinstance(value, list):
            total += len(value)
        else:
            total += 1
    return total


def build_evidence_export(
    manager: Any,
    *,
    run_id: str | None = None,
    project_id: str = DEFAULT_PROJECT_ID,
    data_residency: str = DEFAULT_DATA_RESIDENCY,
    catalog_snapshot: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Freeze console provenance and bounded raw records into one JSON package."""

    captured_at = generated_at or utc_now()
    project = normalize_project_id(project_id)
    residency = normalize_data_residency(data_residency)
    console = build_evidence_console(
        manager,
        run_id=run_id,
        project_id=project,
        data_residency=residency,
        generated_at=captured_at,
    )
    record = (
        _scoped_get(manager, run_id, project, residency)
        if run_id
        else None
    )
    if run_id and record is None:
        raise KeyError(run_id)
    events = (
        manager.events(run_id, after_revision=0, limit=MAX_EXPORTED_EVENTS)
        if run_id
        else []
    )
    records: dict[str, Any] = {
        "run": record.snapshot() if record is not None else None,
        "run_summaries": _scoped_list(
            manager, project, residency
        )[:MAX_EXPORTED_RUNS],
        "events": events,
        "planning": manager.planning(run_id) if run_id else None,
        "placements": manager.placements(run_id) if run_id else [],
        "cost_ledger": _scoped_ledger(
            manager,
            project,
            residency,
            run_id=run_id,
        ),
        "validations": manager.validations(run_id) if run_id else [],
        "failures": manager.failures(run_id) if run_id else [],
        "artifacts": manager.artifacts(run_id) if run_id else [],
        "forecast": manager.forecast(run_id) if run_id else None,
        "runtime_health": {
            "runtime_schema": RUNTIME_SCHEMA_VERSION,
            "project_id": project,
            "data_residency": residency,
            "scoped_run_count": len(
                _scoped_list(manager, project, residency)
            ),
            "exported_event_count": len(events),
            "global_counts_included": False,
        },
        "catalog_snapshot": dict(catalog_snapshot or {}),
    }
    safe_records = _redact_secrets(records)
    package = {
        "schema_version": EVIDENCE_EXPORT_SCHEMA_VERSION,
        "generated_at": captured_at,
        "scope": console["scope"],
        "manifest": {
            "console_schema_version": EVIDENCE_CONSOLE_SCHEMA_VERSION,
            "taxonomy_version": EVIDENCE_TAXONOMY_VERSION,
            "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
            "source_count": len(console["sources"]),
            "metric_count": len(console["metrics"]),
            "record_count": _record_count(safe_records),
            "event_limit": MAX_EXPORTED_EVENTS,
            "events_truncated": len(events) >= MAX_EXPORTED_EVENTS,
            "commercial_claims_allowed": False,
            "secrets_redacted": True,
            "raw_artifact_content_included": False,
            "project_isolated": True,
            "data_residency_isolated": True,
            "logical_project_erasure": True,
            "physical_project_erasure": False,
        },
        "console": console,
        "records": safe_records,
    }
    package["integrity"] = {
        "algorithm": "sha256",
        "canonical_json": True,
        "package_hash": _digest(package),
    }
    validate_evidence_export(package)
    return package


def validate_evidence_export(package: Mapping[str, Any]) -> None:
    if package.get("schema_version") != EVIDENCE_EXPORT_SCHEMA_VERSION:
        raise EvidenceConsoleError("unsupported evidence export schema")
    integrity = package.get("integrity")
    if not isinstance(integrity, Mapping):
        raise EvidenceConsoleError("evidence export integrity is missing")
    semantic = dict(package)
    semantic.pop("integrity", None)
    expected = _digest(semantic)
    if integrity.get("package_hash") != expected:
        raise EvidenceConsoleError("evidence export integrity failed")
    console = package.get("console")
    if not isinstance(console, Mapping):
        raise EvidenceConsoleError("evidence console snapshot is missing")
    if console.get("schema_version") != EVIDENCE_CONSOLE_SCHEMA_VERSION:
        raise EvidenceConsoleError("console schema and export schema disagree")
    scope = package.get("scope")
    console_scope = console.get("scope")
    if not isinstance(scope, Mapping) or scope != console_scope:
        raise EvidenceConsoleError("console and export scope disagree")
    try:
        if (
            normalize_project_id(scope.get("project_id"))
            != scope.get("project_id")
            or normalize_data_residency(scope.get("data_residency"))
            != scope.get("data_residency")
        ):
            raise EvidenceConsoleError("evidence export scope is not canonical")
    except (TypeError, ValueError) as exc:
        raise EvidenceConsoleError("evidence export scope is invalid") from exc
    source_ids = {
        str(item.get("source_id"))
        for item in console.get("sources", [])
        if isinstance(item, Mapping)
    }
    for item in console.get("metrics", []):
        if not isinstance(item, Mapping):
            raise EvidenceConsoleError("invalid metric payload")
        if item.get("evidence_kind") not in EVIDENCE_KINDS:
            raise EvidenceConsoleError("invalid metric evidence kind")
        if str(item.get("source_id")) not in source_ids:
            raise EvidenceConsoleError("metric source is not traceable")


def public_evidence_console_policy() -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_CONSOLE_SCHEMA_VERSION,
        "export_schema_version": EVIDENCE_EXPORT_SCHEMA_VERSION,
        "taxonomy_version": EVIDENCE_TAXONOMY_VERSION,
        "evidence_kinds": list(EVIDENCE_KINDS),
        "evidence_levels": list(EVIDENCE_LEVELS),
        "source_trace_required": True,
        "sample_size_required_for_business_claims": True,
        "confidence_interval_required_for_business_claims": True,
        "task_family_scope_required_for_business_claims": True,
        "fixture_values_count_as_real": False,
        "missing_values_rendered_explicitly": True,
        "commercial_claims_allowed_without_e2": False,
        "export_secrets_redacted": True,
        "export_artifact_content_included": False,
        "project_isolation_required": True,
        "data_residency_isolation_required": True,
        "logical_project_erasure_supported": True,
        "physical_project_erasure_supported": False,
    }


__all__ = [
    "EVIDENCE_CONSOLE_SCHEMA_VERSION",
    "EVIDENCE_EXPORT_SCHEMA_VERSION",
    "EVIDENCE_KINDS",
    "EVIDENCE_LEVELS",
    "EvidenceConsoleError",
    "build_evidence_console",
    "build_evidence_export",
    "public_evidence_console_policy",
    "validate_evidence_export",
]
