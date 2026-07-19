from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .compat_v4 import (
    contracts_v4_enabled,
    set_run_status,
    set_task_status,
    v3_snapshot_to_v4,
)
from .contracts_v4 import new_run_id
from .domain import Event, Plan, RunRequest, RunStatus, TaskContract, TaskStatus
from .catalog_store import CATALOG_STORE, CatalogStore
from .executor import SimulatedExecutor, utc_now
from .planner import HeuristicPlanner
from .router import PredictiveRouter
from .validator import ContractValidator, PlanValidationError


@dataclass
class RunRecord:
    id: str
    request: RunRequest
    status: RunStatus = RunStatus.CREATED
    plan: Plan | None = None
    events: list[Event] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    current_success_probability: float = 0.0
    spent: float = 0.0
    replans: int = 0
    peak_parallelism: int = 0
    error: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            duration_ms = None
            if self.started_at:
                end = self.finished_at or utc_now()
                from datetime import datetime
                duration_ms = round((datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))).total_seconds() * 1000)
            snapshot = {
                "id": self.id,
                "request": {
                    "goal": self.request.goal,
                    "mode": self.request.mode.value,
                    "budget": self.request.budget,
                    "quality_floor": self.request.quality_floor,
                    "max_concurrency": self.request.max_concurrency,
                    "simulate_replan": self.request.simulate_replan,
                },
                "status": self.status.value,
                "plan": self.plan.to_dict() if self.plan else None,
                "events": [event.to_dict() for event in self.events],
                "metrics": {
                    "success_probability": round(self.current_success_probability, 4),
                    "spent": round(self.spent, 4),
                    "budget": self.request.budget,
                    "replans": self.replans,
                    "peak_parallelism": self.peak_parallelism,
                    "duration_ms": duration_ms,
                    "validated_tasks": sum(task.status == TaskStatus.VALIDATED for task in self.plan.tasks) if self.plan else 0,
                    "total_tasks": len(self.plan.tasks) if self.plan else 0,
                },
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
            }
            return (
                v3_snapshot_to_v4(snapshot)
                if contracts_v4_enabled()
                else snapshot
            )


class OrchestrationEngine:
    def __init__(self, catalog_store: CatalogStore | None = None):
        self.planner = HeuristicPlanner()
        self.router = PredictiveRouter(store=catalog_store or CATALOG_STORE)
        self.validator = ContractValidator()
        self.executor = SimulatedExecutor()

    async def run(self, record: RunRecord) -> None:
        record.started_at = utc_now()
        set_run_status(
            record,
            RunStatus.PLANNING,
            source="engine.run.planning",
        )
        self._event(record, "planning", "本地总指挥正在判断难度并生成任务图")
        try:
            run_router = self.router.freeze()
            plan = self.planner.plan(record.request)
            issues = self.validator.validate_plan(plan)
            if issues:
                raise PlanValidationError("；".join(issues))
            record.plan = plan
            plan.model_snapshot = run_router.catalog_revision
            plan.routing_summary = run_router.assign(
                plan.tasks,
                record.request.mode,
                record.request.quality_floor,
                record.request.budget,
            )
            self._recalculate_probability(record)
            self._event(
                record,
                "plan_ready",
                f"生成计划 v{plan.version}：难度 L{plan.difficulty}/10，共 {len(plan.tasks)} 个节点，"
                f"预计 ${plan.routing_summary['estimated_total_cost_usd']:.4f}",
            )
            self._event(record, "plan_validated", "任务画像、合同字段、硬约束、模型候选和 DAG 检查通过")
            set_run_status(
                record,
                RunStatus.EXECUTING,
                source="engine.run.executing",
            )
            await self._execute_dag(record, run_router)
            if all(task.status in {TaskStatus.VALIDATED, TaskStatus.INVALIDATED} for task in record.plan.tasks):
                set_run_status(
                    record,
                    RunStatus.COMPLETED,
                    source="engine.run.completed",
                )
                self._recalculate_probability(record)
                self._event(record, "completed", "最终成果通过全局验收，运行结束")
            else:
                raise RuntimeError("执行结束时仍有未处理节点")
        except Exception as exc:  # final safety boundary for background runs
            set_run_status(
                record,
                RunStatus.FAILED,
                source="engine.run.failed",
            )
            record.error = str(exc)
            self._event(record, "failed", f"运行失败：{exc}")
        finally:
            record.finished_at = utc_now()

    async def _execute_dag(
        self,
        record: RunRecord,
        run_router: PredictiveRouter,
    ) -> None:
        active: dict[asyncio.Task, TaskContract] = {}
        injected = False
        try:
            while True:
                pending = [task for task in record.plan.tasks if task.status == TaskStatus.PENDING]
                if not pending and not active:
                    return

                validated_ids = {task.id for task in record.plan.tasks if task.status == TaskStatus.VALIDATED}
                ready = [task for task in pending if set(task.dependencies) <= validated_ids]
                slots = record.request.max_concurrency - len(active)
                reserved = sum(item.estimated_cost for item in active.values())
                for task in sorted(ready, key=lambda item: item.priority, reverse=True)[:max(0, slots)]:
                    if record.spent + reserved + task.estimated_cost > record.request.budget + 1e-9:
                        raise RuntimeError(
                            f"预算守卫拒绝启动 {task.title}：已用 ${record.spent:.4f}，"
                            f"在途 ${reserved:.4f}，节点预计 ${task.estimated_cost:.4f}，预算 ${record.request.budget:.4f}"
                        )
                    set_task_status(
                        record,
                        task,
                        TaskStatus.RUNNING,
                        source="engine.task.started",
                    )
                    task.attempt += 1
                    task.started_at = utc_now()
                    dependency_results = [
                        item.result for item in record.plan.tasks
                        if item.id in task.dependencies and item.result is not None
                    ]
                    should_inject = (
                        record.request.simulate_replan
                        and not injected
                        and task.id.startswith("branch-")
                    )
                    if should_inject:
                        injected = True
                    worker = run_router.get_worker(task.selected_worker)
                    future = asyncio.create_task(self.executor.execute(task, worker, dependency_results, should_inject))
                    active[future] = task
                    reserved += task.estimated_cost
                    self._event(
                        record,
                        "task_started",
                        f"{task.title} → {worker.name}（预测 {task.predicted_success:.0%} / ${task.estimated_cost:.4f}）",
                        task.id,
                        {"routing_candidates": task.routing_candidates},
                    )

                record.peak_parallelism = max(record.peak_parallelism, len(active))
                if not active:
                    blocked = [task.id for task in pending]
                    raise RuntimeError(f"DAG无法继续，阻塞节点：{blocked}")

                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for future in done:
                    task = active.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        task.finished_at = utc_now()
                        set_task_status(
                            record,
                            task,
                            TaskStatus.REJECTED,
                            source="engine.task.adapter_rejected",
                        )
                        task.validation = {
                            "passed": False,
                            "level": "L0-adapter",
                            "findings": [f"执行适配器异常：{exc}"],
                            "score": 0.0,
                        }
                        self._recalculate_probability(record)
                        self._event(record, "task_rejected", f"{task.title} 执行异常：{exc}", task.id)
                        await self._global_replan(
                            record,
                            task,
                            run_router,
                            reserved_cost=sum(item.estimated_cost for item in active.values()),
                        )
                        continue

                    task.result = result
                    task.finished_at = utc_now()
                    task.validation = self.validator.validate_result(task, result)
                    record.spent += task.estimated_cost

                    if task.validation["passed"]:
                        set_task_status(
                            record,
                            task,
                            TaskStatus.VALIDATED,
                            source="engine.task.validated",
                        )
                        self._recalculate_probability(record)
                        self._event(record, "task_validated", f"{task.title} 通过 {task.validation['level']} 验收", task.id)
                    else:
                        set_task_status(
                            record,
                            task,
                            TaskStatus.REJECTED,
                            source="engine.task.validation_rejected",
                        )
                        self._recalculate_probability(record)
                        self._event(record, "task_rejected", f"{task.title} 未通过验收：{'; '.join(task.validation['findings'])}", task.id)
                        await self._global_replan(
                            record,
                            task,
                            run_router,
                            reserved_cost=sum(item.estimated_cost for item in active.values()),
                        )

                    if record.spent > record.request.budget + 1e-9:
                        raise RuntimeError(f"预算已耗尽：{record.spent:.4f} > {record.request.budget:.4f}")
        finally:
            if active:
                for future in active:
                    future.cancel()
                await asyncio.gather(*active, return_exceptions=True)
                for task in active.values():
                    if task.status == TaskStatus.RUNNING:
                        set_task_status(
                            record,
                            task,
                            TaskStatus.REJECTED,
                            source="engine.task.cancelled",
                        )
                        task.finished_at = utc_now()

    async def _global_replan(
        self,
        record: RunRecord,
        failed: TaskContract,
        run_router: PredictiveRouter,
        reserved_cost: float = 0.0,
    ) -> None:
        if record.replans >= 1:
            raise RuntimeError("全局重规划次数已达到MVP安全上限")
        set_run_status(
            record,
            RunStatus.REPLANNING,
            source="engine.run.replanning",
        )
        record.replans += 1
        descendants = self.validator.descendants(record.plan.tasks, failed.id)
        for task in record.plan.tasks:
            if task.id in descendants and task.status != TaskStatus.VALIDATED:
                set_task_status(
                    record,
                    task,
                    TaskStatus.PENDING,
                    source="engine.task.replan_reset",
                )
                task.result = None
                task.validation = None
        set_task_status(
            record,
            failed,
            TaskStatus.INVALIDATED,
            source="engine.task.invalidated",
        )
        replacement = self.planner.replan(record.plan, failed.id)
        remaining_budget = max(0.0, record.request.budget - record.spent - reserved_cost)
        pending_for_route = [task for task in record.plan.tasks if task.status == TaskStatus.PENDING]
        replacement_summary = run_router.assign(
            pending_for_route, record.request.mode, record.request.quality_floor, remaining_budget
        )
        record.plan.routing_summary = {
            **replacement_summary,
            "replanned": True,
            "reserved_in_flight_usd": round(reserved_cost, 6),
        }
        self._event(record, "replanning", f"高级模型候选重新规划；保留已验收成果，替换 {failed.id}", failed.id, {
            "replacement": replacement.id,
            "affected_descendants": sorted(descendants),
            "selected_model": replacement.selected_worker,
            "routing": replacement_summary,
        })
        await asyncio.sleep(.18)
        issues = self.validator.validate_plan(record.plan)
        if issues:
            raise PlanValidationError("重规划未通过：" + "；".join(issues))
        self._recalculate_probability(record)
        set_run_status(
            record,
            RunStatus.EXECUTING,
            source="engine.run.replan_executing",
        )
        self._event(
            record,
            "replan_validated",
            f"计划v{record.plan.version}通过检查，成功率重估为{record.current_success_probability:.0%}，恢复并行调度",
        )

    @staticmethod
    def _recalculate_probability(record: RunRecord) -> None:
        if not record.plan:
            record.current_success_probability = 0.0
            return
        weighted = 0.0
        total_weight = 0.0
        unresolved_high_risk = 0
        for task in record.plan.tasks:
            if task.status == TaskStatus.INVALIDATED:
                continue
            weight = max(.1, task.criticality)
            prior = task.predicted_success_lower_bound or task.predicted_success or .5
            if task.status == TaskStatus.VALIDATED:
                validation_score = float((task.validation or {}).get("score", prior))
                evidence_score = prior * .45 + validation_score * .55
            elif task.status == TaskStatus.REJECTED:
                evidence_score = min(.25, prior * .3)
            elif task.status == TaskStatus.SUSPECT:
                evidence_score = prior * .58
            else:
                evidence_score = prior
            if task.risk_level == "high" and task.status != TaskStatus.VALIDATED:
                unresolved_high_risk += 1
            weighted += evidence_score * weight
            total_weight += weight
        probability = weighted / total_weight if total_weight else .05
        probability -= min(.12, unresolved_high_risk * .025)
        record.current_success_probability = max(.05, min(.98, probability))

    @staticmethod
    def _event(record: RunRecord, kind: str, message: str, task_id: str | None = None, detail: dict | None = None) -> None:
        with record.lock:
            record.events.append(Event(utc_now(), kind, message, task_id, detail or {}))


class RunManager:
    def __init__(self, catalog_store: CatalogStore | None = None):
        self.engine = OrchestrationEngine(catalog_store)
        self.records: dict[str, RunRecord] = {}
        self.lock = threading.RLock()

    def create(self, request: RunRequest) -> RunRecord:
        request = request.normalized()
        run_id = (
            str(new_run_id())
            if contracts_v4_enabled()
            else uuid.uuid4().hex[:12]
        )
        record = RunRecord(id=run_id, request=request)
        with self.lock:
            self.records[record.id] = record
        thread = threading.Thread(target=lambda: asyncio.run(self.engine.run(record)), daemon=True, name=f"run-{record.id}")
        thread.start()
        return record

    def get(self, run_id: str) -> RunRecord | None:
        with self.lock:
            return self.records.get(run_id)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            records = list(self.records.values())
        return [record.snapshot() for record in reversed(records[-20:])]
