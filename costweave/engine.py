from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .domain import Event, Plan, RunRequest, RunStatus, TaskContract, TaskStatus
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
            return {
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


class OrchestrationEngine:
    def __init__(self):
        self.planner = HeuristicPlanner()
        self.router = PredictiveRouter()
        self.validator = ContractValidator()
        self.executor = SimulatedExecutor()

    async def run(self, record: RunRecord) -> None:
        record.started_at = utc_now()
        record.status = RunStatus.PLANNING
        self._event(record, "planning", "本地总指挥正在判断难度并生成任务图")
        try:
            plan = self.planner.plan(record.request)
            issues = self.validator.validate_plan(plan)
            if issues:
                raise PlanValidationError("；".join(issues))
            self.router.assign(plan.tasks, record.request.mode, record.request.quality_floor)
            record.plan = plan
            record.current_success_probability = plan.predicted_success
            self._event(record, "plan_ready", f"生成计划v{plan.version}：难度L{plan.difficulty}，共{len(plan.tasks)}个节点")
            self._event(record, "plan_validated", "分工合同、依赖关系与DAG循环检查通过")
            record.status = RunStatus.EXECUTING
            await self._execute_dag(record)
            if all(task.status in {TaskStatus.VALIDATED, TaskStatus.INVALIDATED} for task in record.plan.tasks):
                record.status = RunStatus.COMPLETED
                record.current_success_probability = min(.98, record.current_success_probability + .04)
                self._event(record, "completed", "最终成果通过全局验收，运行结束")
            else:
                raise RuntimeError("执行结束时仍有未处理节点")
        except Exception as exc:  # final safety boundary for background runs
            record.status = RunStatus.FAILED
            record.error = str(exc)
            self._event(record, "failed", f"运行失败：{exc}")
        finally:
            record.finished_at = utc_now()

    async def _execute_dag(self, record: RunRecord) -> None:
        active: dict[asyncio.Task, TaskContract] = {}
        injected = False
        while True:
            pending = [task for task in record.plan.tasks if task.status == TaskStatus.PENDING]
            if not pending and not active:
                return

            validated_ids = {task.id for task in record.plan.tasks if task.status == TaskStatus.VALIDATED}
            ready = [task for task in pending if set(task.dependencies) <= validated_ids]
            slots = record.request.max_concurrency - len(active)
            for task in sorted(ready, key=lambda item: item.priority, reverse=True)[:max(0, slots)]:
                task.status = TaskStatus.RUNNING
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
                worker = self.router.get_worker(task.selected_worker)
                future = asyncio.create_task(self.executor.execute(task, worker, dependency_results, should_inject))
                active[future] = task
                self._event(record, "task_started", f"{task.title} → {worker.name}", task.id)

            record.peak_parallelism = max(record.peak_parallelism, len(active))
            if not active:
                blocked = [task.id for task in pending]
                raise RuntimeError(f"DAG无法继续，阻塞节点：{blocked}")

            done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            for future in done:
                task = active.pop(future)
                result = future.result()
                task.result = result
                task.finished_at = utc_now()
                task.validation = self.validator.validate_result(task, result)
                record.spent += task.estimated_cost

                if task.validation["passed"]:
                    task.status = TaskStatus.VALIDATED
                    self._update_probability(record, positive=True, score=task.validation["score"])
                    self._event(record, "task_validated", f"{task.title} 通过 {task.validation['level']} 验收", task.id)
                else:
                    task.status = TaskStatus.REJECTED
                    self._update_probability(record, positive=False, score=task.validation["score"])
                    self._event(record, "task_rejected", f"{task.title} 未通过验收：{'; '.join(task.validation['findings'])}", task.id)
                    await self._global_replan(record, task)

                if record.spent > record.request.budget:
                    raise RuntimeError(f"预算已耗尽：{record.spent:.3f} > {record.request.budget:.3f}")

    async def _global_replan(self, record: RunRecord, failed: TaskContract) -> None:
        if record.replans >= 1:
            raise RuntimeError("全局重规划次数已达到MVP安全上限")
        record.status = RunStatus.REPLANNING
        record.replans += 1
        descendants = self.validator.descendants(record.plan.tasks, failed.id)
        for task in record.plan.tasks:
            if task.id in descendants and task.status != TaskStatus.VALIDATED:
                task.status = TaskStatus.PENDING
                task.result = None
                task.validation = None
        failed.status = TaskStatus.INVALIDATED
        replacement = self.planner.replan(record.plan, failed.id)
        self._event(record, "replanning", f"高级规划顾问模拟接管；保留已验收成果，替换 {failed.id}", failed.id, {
            "replacement": replacement.id,
            "affected_descendants": sorted(descendants),
        })
        await asyncio.sleep(.18)
        issues = self.validator.validate_plan(record.plan)
        if issues:
            raise PlanValidationError("重规划未通过：" + "；".join(issues))
        # A validated replacement plan is new positive evidence. Recover the
        # estimate without erasing the earlier failure signal completely.
        record.current_success_probability = max(
            record.current_success_probability,
            record.plan.predicted_success,
            min(.92, record.request.quality_floor + .03),
        )
        record.status = RunStatus.EXECUTING
        self._event(
            record,
            "replan_validated",
            f"计划v{record.plan.version}通过检查，成功率重估为{record.current_success_probability:.0%}，恢复并行调度",
        )

    @staticmethod
    def _update_probability(record: RunRecord, positive: bool, score: float) -> None:
        if positive:
            record.current_success_probability += (1 - record.current_success_probability) * (.06 + score * .025)
        else:
            record.current_success_probability *= max(.25, .68 - score * .2)
        record.current_success_probability = max(.05, min(.99, record.current_success_probability))

    @staticmethod
    def _event(record: RunRecord, kind: str, message: str, task_id: str | None = None, detail: dict | None = None) -> None:
        with record.lock:
            record.events.append(Event(utc_now(), kind, message, task_id, detail or {}))


class RunManager:
    def __init__(self):
        self.engine = OrchestrationEngine()
        self.records: dict[str, RunRecord] = {}
        self.lock = threading.RLock()

    def create(self, request: RunRequest) -> RunRecord:
        request = request.normalized()
        record = RunRecord(id=uuid.uuid4().hex[:12], request=request)
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
