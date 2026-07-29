from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .compat_v4 import (
    artifact_dag_enabled,
    contracts_v4_enabled,
    evidence_recovery_enabled,
    outcome_forecast_enabled,
    media_workflow_enabled,
    parallel_runtime_enabled,
    placement_v4_enabled,
    runtime_v4_enabled,
    set_run_status,
    set_task_status,
    v3_snapshot_to_v4,
)
from .contracts_v4 import new_run_id
from .domain import (
    Event,
    Plan,
    RunRequest,
    RunStatus,
    TaskContract,
    TaskStatus,
    WorkerProfile,
)
from .catalog_store import CATALOG_STORE, CatalogStore
from .executor import SimulatedExecutor, utc_now
from .graph_compiler import GraphCompiler
from .failure_classifier import FailureClassifier, FailureRecord
from .plan_patch import PlanPatch
from .planner import HeuristicPlanner
from .recovery import (
    RecoveryAction,
    RecoveryContext,
    RecoveryDecision,
    RecoveryPolicy,
)
from .router import PredictiveRouter
from .scheduler import ParallelScheduler
from .validator import ContractValidator, PlanValidationError
from .dag_simulation import DagMonteCarlo
from .deliberation import DeliberationSimulator
from .planning_governor import PlanningGovernor


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
    recovery_count: int = 0
    recovery_history: list[dict[str, Any]] = field(default_factory=list)
    peak_parallelism: int = 0
    error: str | None = None
    runtime_revision: int = 0
    recovered: bool = False
    cancel_requested: bool = False
    retry_of: str | None = None
    planning_decision: dict[str, Any] | None = None
    planning_proposals: list[dict[str, Any]] = field(default_factory=list)
    worker_snapshot: list[dict[str, Any]] = field(default_factory=list, repr=False)
    control_kernel: Any = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self, *, include_internal: bool = False) -> dict[str, Any]:
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
                    "deadline_seconds": self.request.deadline_seconds,
                    "recovery_budget_ratio": (
                        self.request.recovery_budget_ratio
                    ),
                    "value_of_time_per_second": (
                        self.request.value_of_time_per_second
                    ),
                    "max_recoveries": self.request.max_recoveries,
                    "max_task_attempts": self.request.max_task_attempts,
                    "recovery_time_limit_seconds": (
                        self.request.recovery_time_limit_seconds
                    ),
                },
                "status": self.status.value,
                "plan": self.plan.to_dict() if self.plan else None,
                "events": [event.to_dict() for event in self.events],
                "metrics": {
                    "success_probability": round(self.current_success_probability, 4),
                    "spent": round(self.spent, 4),
                    "budget": self.request.budget,
                    "replans": self.replans,
                    "recoveries": self.recovery_count,
                    "peak_parallelism": self.peak_parallelism,
                    "duration_ms": duration_ms,
                    "validated_tasks": sum(task.status == TaskStatus.VALIDATED for task in self.plan.tasks) if self.plan else 0,
                    "total_tasks": len(self.plan.tasks) if self.plan else 0,
                },
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "recovery_history": list(self.recovery_history),
                "planning": {
                    "decision": self.planning_decision,
                    "proposals": list(self.planning_proposals),
                },
                "forecast": (
                    self.control_kernel.forecast(str(self.id))
                    if self.control_kernel is not None
                    else None
                ),
                "media_project": (
                    self.control_kernel.media_project(str(self.id))
                    if self.control_kernel is not None
                    else None
                ),
                "runtime": {
                    "schema_version": "v5.2",
                    "revision": self.runtime_revision,
                    "persistent": self.control_kernel is not None,
                    "artifact_dag": (
                        artifact_dag_enabled()
                        and self.control_kernel is not None
                    ),
                    "parallel_runtime": (
                        parallel_runtime_enabled()
                        and self.control_kernel is not None
                    ),
                    "placement_v4": (
                        placement_v4_enabled()
                        and self.control_kernel is not None
                    ),
                    "recovered": self.recovered,
                    "cancel_requested": self.cancel_requested,
                    "retry_of": self.retry_of,
                },
                "artifacts": (
                    self.control_kernel.artifacts(
                        str(self.id), include_content=False
                    )
                    if artifact_dag_enabled()
                    and self.control_kernel is not None
                    else []
                ),
                "scheduling": (
                    self.control_kernel.scheduling_summary(str(self.id))
                    if parallel_runtime_enabled()
                    and self.control_kernel is not None
                    else {
                        "reservations": 0,
                        "active_leases": 0,
                        "reserved_cost": 0.0,
                        "released_actual_cost": 0.0,
                        "pending_approvals": 0,
                        "resolved_approvals": 0,
                    }
                ),
            }
            if include_internal:
                snapshot["_runtime_internal"] = {
                    "worker_snapshot": self.worker_snapshot,
                }
            return (
                v3_snapshot_to_v4(
                    snapshot,
                    trace_persistent=self.control_kernel is not None,
                )
                if contracts_v4_enabled()
                else snapshot
            )


class RunCancelled(RuntimeError):
    """Raised internally when a persisted cancellation stops execution."""


class OrchestrationEngine:
    def __init__(
        self,
        catalog_store: CatalogStore | None = None,
        *,
        control_kernel: Any = None,
    ):
        self.planner = HeuristicPlanner()
        self.deliberation = DeliberationSimulator()
        self.planning_governor = PlanningGovernor()
        self.router = PredictiveRouter(store=catalog_store or CATALOG_STORE)
        self.validator = ContractValidator()
        self.failure_classifier = FailureClassifier()
        self.recovery_policy = RecoveryPolicy()
        self.forecaster = DagMonteCarlo()
        self.executor = SimulatedExecutor()
        self.control_kernel = control_kernel
        self.graph_compiler = GraphCompiler()
        self.scheduler = (
            ParallelScheduler(control_kernel)
            if control_kernel is not None
            else None
        )

    async def run(self, record: RunRecord) -> None:
        record.started_at = utc_now()
        self._raise_if_cancelled(record)
        set_run_status(
            record,
            RunStatus.PLANNING,
            source="engine.run.planning",
        )
        self._event(record, "planning", "本地总指挥正在判断难度并生成任务图")
        try:
            run_router = self.router.freeze()
            record.worker_snapshot = [worker.to_dict() for worker in run_router.workers]
            if parallel_runtime_enabled() and self.scheduler is not None:
                self.scheduler.configure(
                    run_router.workers,
                    max_concurrency=record.request.max_concurrency,
                )
            analysis_planner = (
                self.planner
                if hasattr(self.planner, "analyze")
                else HeuristicPlanner()
            )
            analysis = analysis_planner.analyze(record.request)
            planning_decision = self.planning_governor.decide(
                record.request,
                analysis,
                run_router.workers,
            )
            planning_proposals = self.deliberation.proposals(
                planning_decision,
                record.request,
                analysis,
            )
            record.planning_decision = planning_decision.to_dict()
            record.planning_proposals = [
                proposal.to_dict() for proposal in planning_proposals
            ]
            if bool(
                planning_decision.pre_execution_escalation.get(
                    "required"
                )
            ):
                analysis.planning_action = "senior-review"
            self._event(
                record,
                "planning_mode_decided",
                (
                    f"Planning Governor 选择 {planning_decision.mode.value}；"
                    f"规划角色 {planning_decision.panel_policy.size} 个，"
                    "管理深度上限 2"
                ),
                detail={
                    "decision_id": planning_decision.decision_id,
                    "mode": planning_decision.mode.value,
                    "fingerprint": planning_decision.fingerprint(),
                    "difficulty_interval": (
                        planning_decision.difficulty_vector.overall.to_dict()
                        if planning_decision.difficulty_vector is not None
                        else None
                    ),
                    "value_of_deliberation": (
                        planning_decision.value_of_deliberation
                    ),
                    "pre_execution_escalation": (
                        planning_decision.pre_execution_escalation
                    ),
                    "simulated": False,
                },
            )
            plan = (
                self.planner.plan(record.request, analysis)
                if isinstance(self.planner, HeuristicPlanner)
                else self.planner.plan(record.request)
            )
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
                max_concurrency=record.request.max_concurrency,
                deadline_seconds=record.request.deadline_seconds,
                recovery_budget_ratio=(
                    record.request.recovery_budget_ratio
                ),
                value_of_time_per_second=(
                    record.request.value_of_time_per_second
                ),
                availability=self._resource_availability(record),
            )
            if artifact_dag_enabled() and record.control_kernel is not None:
                record.control_kernel.compile_graph(record)
            if (
                placement_v4_enabled()
                and record.control_kernel is not None
            ):
                self._checkpoint(record, "engine.plan.placements")
                record.control_kernel.sync_placements(record)
            if (
                outcome_forecast_enabled()
                and record.control_kernel is not None
            ):
                forecast = self.forecaster.simulate(
                    run_id=str(record.id),
                    plan=plan,
                    request=record.request,
                    calibrated_probabilities={
                        task.id: float(
                            record.control_kernel.calibrated_probability(
                                task
                            )["probability"]
                        )
                        for task in plan.tasks
                    },
                )
                record.control_kernel.record_forecast(forecast)
            if (
                media_workflow_enabled()
                and record.control_kernel is not None
            ):
                media_project = (
                    record.control_kernel.ensure_media_project(record)
                )
                if media_project is not None:
                    self._event(
                        record,
                        "media_project_ready",
                        (
                            "媒体制作合同、视觉圣经、镜头 DAG "
                            "和低成本预览已生成"
                        ),
                        detail={
                            "project_id": media_project["project_id"],
                            "workflow_kind": media_project[
                                "project"
                            ]["workflow_kind"],
                            "shot_count": len(
                                media_project["shots"]
                            ),
                            "simulated": True,
                        },
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
            self._raise_if_cancelled(record)
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
        except RunCancelled:
            record.error = "运行已取消"
            self._event(record, "cancelled", "运行已取消；未启动新的任务节点")
        except Exception as exc:  # final safety boundary for background runs
            if record.status != RunStatus.CANCELLED:
                # Publish the diagnostic before the terminal state.  Readers
                # must never observe `failed` with a transiently empty error.
                record.error = str(exc)
                set_run_status(
                    record,
                    RunStatus.FAILED,
                    source="engine.run.failed",
                )
                self._event(record, "failed", f"运行失败：{exc}")
        finally:
            record.finished_at = utc_now()
            if (
                outcome_forecast_enabled()
                and record.control_kernel is not None
                and record.plan is not None
                and record.status in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }
            ):
                record.control_kernel.record_outcome(record)
            self._checkpoint(record, "engine.run.finished")

    async def resume(self, record: RunRecord) -> None:
        """Resume a persisted non-terminal run without redoing validated work."""

        if record.status in {RunStatus.CREATED, RunStatus.PLANNING}:
            await self.run(record)
            return
        if record.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return
        if record.plan is None:
            raise RuntimeError("无法恢复没有持久化计划的运行")
        if artifact_dag_enabled() and record.control_kernel is not None:
            record.control_kernel.compile_graph(record)
            self._materialize_legacy_artifacts(record)
        if record.status == RunStatus.REPLANNING:
            set_run_status(
                record,
                RunStatus.EXECUTING,
                source="engine.recovery.replanning_to_executing",
            )
        record.recovered = True
        record.finished_at = None
        record.cancel_requested = False
        for task in record.plan.tasks:
            if task.status == TaskStatus.RUNNING:
                if artifact_dag_enabled() and record.control_kernel is not None:
                    record.control_kernel.invalidate_tasks(
                        record,
                        {task.id},
                        reason="interrupted producer attempt",
                    )
                set_task_status(
                    record,
                    task,
                    TaskStatus.SUSPECT,
                    source="engine.recovery.interrupted",
                )
                set_task_status(
                    record,
                    task,
                    TaskStatus.PENDING,
                    source="engine.recovery.retry_pending",
                )
                task.started_at = None
                task.finished_at = None
        workers = tuple(WorkerProfile(**item) for item in record.worker_snapshot)
        run_router = (
            PredictiveRouter(workers=workers, catalog_revision=record.plan.model_snapshot)
            if workers
            else self.router.freeze()
        )
        if parallel_runtime_enabled() and self.scheduler is not None:
            self.scheduler.configure(
                run_router.workers,
                max_concurrency=record.request.max_concurrency,
            )
        if (
            placement_v4_enabled()
            and record.control_kernel is not None
        ):
            record.control_kernel.sync_placements(record)
        self._event(record, "recovered", "从持久事件投影恢复运行；保留已验收节点")
        try:
            await self._execute_dag(record, run_router)
            self._raise_if_cancelled(record)
            if all(
                task.status in {TaskStatus.VALIDATED, TaskStatus.INVALIDATED}
                for task in record.plan.tasks
            ):
                set_run_status(
                    record,
                    RunStatus.COMPLETED,
                    source="engine.recovery.completed",
                )
                self._recalculate_probability(record)
                self._event(record, "completed", "恢复后的运行通过全局验收")
            else:
                raise RuntimeError("恢复执行结束时仍有未处理节点")
        except RunCancelled:
            record.error = "运行已取消"
            self._event(record, "cancelled", "恢复运行已取消")
        except Exception as exc:
            if record.status != RunStatus.CANCELLED:
                record.error = str(exc)
                set_run_status(record, RunStatus.FAILED, source="engine.recovery.failed")
                self._event(record, "failed", f"恢复运行失败：{exc}")
        finally:
            record.finished_at = utc_now()
            if (
                outcome_forecast_enabled()
                and record.control_kernel is not None
                and record.plan is not None
                and record.status in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }
            ):
                record.control_kernel.record_outcome(record)
            self._checkpoint(record, "engine.recovery.finished")

    async def _execute_dag(
        self,
        record: RunRecord,
        run_router: PredictiveRouter,
    ) -> None:
        active: dict[asyncio.Task, TaskContract] = {}
        reservations: dict[asyncio.Task, Any] = {}
        injected = False
        try:
            while True:
                self._raise_if_cancelled(record)
                pending = [task for task in record.plan.tasks if task.status == TaskStatus.PENDING]
                if not pending and not active:
                    return

                validated_ids = {
                    task.id
                    for task in record.plan.tasks
                    if task.status == TaskStatus.VALIDATED
                }
                ready = [
                    task
                    for task in pending
                    if {
                        dependency
                        for dependency in task.dependencies
                        if task.dependency_types.get(dependency, "hard") != "soft"
                    }
                    <= validated_ids
                ]
                ordered_ready = (
                    self.scheduler.order_ready(record.plan.tasks, ready)
                    if parallel_runtime_enabled()
                    and self.scheduler is not None
                    else sorted(
                        ready,
                        key=lambda item: item.priority,
                        reverse=True,
                    )
                )
                reserved = sum(item.estimated_cost for item in active.values())
                for task in ordered_ready:
                    if len(active) >= record.request.max_concurrency:
                        break
                    self._raise_if_cancelled(record)
                    worker = run_router.get_worker(task.selected_worker)
                    reservation = None
                    if (
                        parallel_runtime_enabled()
                        and self.scheduler is not None
                    ):
                        decision = self.scheduler.start_decision(
                            record, task, worker
                        )
                        if decision.state != "admitted":
                            if (
                                task.queue_state != decision.state
                                or task.blocked_reason != decision.reason
                            ):
                                task.queue_state = decision.state
                                task.blocked_reason = decision.reason
                                event_kind = (
                                    "approval_required"
                                    if decision.state == "waiting_approval"
                                    else "task_queued"
                                )
                                self._event(
                                    record,
                                    event_kind,
                                    (
                                        f"{task.title} 等待审批"
                                        if decision.state
                                        == "waiting_approval"
                                        else f"{task.title} 进入资源队列"
                                    ),
                                    task.id,
                                    {
                                        "reason": decision.reason,
                                        "retry_after_ms": (
                                            decision.retry_after_ms
                                        ),
                                        "approval_id": task.approval_id,
                                    },
                                )
                            continue
                        reservation = decision.reservation
                    elif (
                        record.spent + reserved + task.estimated_cost
                        > record.request.budget + 1e-9
                    ):
                        raise RuntimeError(
                            f"预算守卫拒绝启动 {task.title}：已用 "
                            f"${record.spent:.4f}，在途 ${reserved:.4f}，"
                            f"节点预计 ${task.estimated_cost:.4f}，"
                            f"预算 ${record.request.budget:.4f}"
                        )
                    try:
                        set_task_status(
                            record,
                            task,
                            TaskStatus.RUNNING,
                            source="engine.task.started",
                        )
                        task.attempt += 1
                        task.started_at = utc_now()
                        task.queue_state = "running"
                        task.blocked_reason = ""
                        task.reservation_id = (
                            reservation.reservation_id
                            if reservation is not None
                            else None
                        )
                        if (
                            artifact_dag_enabled()
                            and record.control_kernel is not None
                        ):
                            dependency_results = (
                                record.control_kernel.prepare_handoff(
                                    record, task
                                )
                            )
                        else:
                            dependency_results = [
                                item.result
                                for item in record.plan.tasks
                                if item.id in task.dependencies
                                and item.result is not None
                            ]
                    except Exception:
                        if reservation is not None:
                            record.control_kernel.reservations.release(
                                reservation.reservation_id,
                                reason="start-failed",
                            )
                        raise
                    should_inject = (
                        record.request.simulate_replan
                        and not injected
                        and task.id.startswith("branch-")
                    )
                    if should_inject:
                        injected = True
                    execute = lambda task=task, worker=worker, dependency_results=dependency_results, should_inject=should_inject: self.executor.execute(
                        task, worker, dependency_results, should_inject
                    )
                    coroutine = (
                        self.scheduler.execute_with_retry(
                            execute,
                            provider=worker.provider,
                            on_retry=lambda attempts, task=task: setattr(
                                task, "transient_retries", attempts
                            ),
                        )
                        if parallel_runtime_enabled()
                        and self.scheduler is not None
                        else execute()
                    )
                    future = asyncio.create_task(coroutine)
                    active[future] = task
                    if reservation is not None:
                        reservations[future] = reservation
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
                    if ready and any(
                        task.queue_state
                        in {"queued", "waiting_approval"}
                        for task in ready
                    ):
                        await asyncio.sleep(0.05)
                        continue
                    blocked = [task.id for task in pending]
                    raise RuntimeError(f"DAG无法继续，阻塞节点：{blocked}")

                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for future in done:
                    self._raise_if_cancelled(record)
                    task = active.pop(future)
                    reservation = reservations.pop(future, None)
                    try:
                        result = future.result()
                    except Exception as exc:
                        if reservation is not None:
                            record.control_kernel.reservations.release(
                                reservation.reservation_id,
                                reason="execution-failed",
                                actual_cost=0.0,
                            )
                        task.reservation_id = None
                        task.queue_state = "rejected"
                        task.blocked_reason = str(exc)
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
                            "contract_revision": (
                                task.success_contract.get(
                                    "revision",
                                    "success-contract-v1",
                                )
                            ),
                            "criterion_results": [],
                            "critical_failures": list(
                                task.critical_criteria
                            ),
                            "evidence_gaps": [str(exc)],
                            "contract_errors": [],
                            "layers_completed": [],
                            "false_positive_risk": 0.01,
                            "false_negative_risk": 0.25,
                        }
                        if (
                            placement_v4_enabled()
                            and record.control_kernel is not None
                        ):
                            record.control_kernel.placement_store.record_actual(
                                run_id=str(record.id),
                                task=task,
                                actual_total=0.0,
                            )
                        self._recalculate_probability(record)
                        self._event(record, "task_rejected", f"{task.title} 执行异常：{exc}", task.id)
                        await self._recover_from_failure(
                            record,
                            task,
                            run_router,
                            reserved_cost=sum(item.estimated_cost for item in active.values()),
                            exception=exc,
                        )
                        continue

                    if reservation is not None:
                        record.control_kernel.reservations.release(
                            reservation.reservation_id,
                            reason="execution-finished",
                            actual_cost=task.estimated_cost,
                        )
                    task.reservation_id = None
                    task.result = result
                    task.finished_at = utc_now()
                    task.validation = self.validator.validate_result(task, result)
                    if (
                        evidence_recovery_enabled()
                        and record.control_kernel is not None
                    ):
                        record.control_kernel.record_validation(
                            record, task
                        )
                    record.spent += task.estimated_cost
                    if (
                        placement_v4_enabled()
                        and record.control_kernel is not None
                    ):
                        record.control_kernel.record_actual_cost(
                            record, task
                        )

                    if artifact_dag_enabled() and record.control_kernel is not None:
                        record.control_kernel.publish_task_artifacts(
                            record,
                            task,
                            result,
                            validated=bool(task.validation["passed"]),
                        )

                    if task.validation["passed"]:
                        task.queue_state = "completed"
                        set_task_status(
                            record,
                            task,
                            TaskStatus.VALIDATED,
                            source="engine.task.validated",
                        )
                        self._recalculate_probability(record)
                        self._event(record, "task_validated", f"{task.title} 通过 {task.validation['level']} 验收", task.id)
                    else:
                        task.queue_state = "rejected"
                        set_task_status(
                            record,
                            task,
                            TaskStatus.REJECTED,
                            source="engine.task.validation_rejected",
                        )
                        self._recalculate_probability(record)
                        self._event(record, "task_rejected", f"{task.title} 未通过验收：{'; '.join(task.validation['findings'])}", task.id)
                        await self._recover_from_failure(
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
                for future, task in active.items():
                    reservation = reservations.get(future)
                    if reservation is not None:
                        record.control_kernel.reservations.release(
                            reservation.reservation_id,
                            reason="runtime-cancelled",
                            actual_cost=0.0,
                        )
                    task.reservation_id = None
                    if task.status == TaskStatus.RUNNING:
                        set_task_status(
                            record,
                            task,
                            TaskStatus.REJECTED,
                            source="engine.task.cancelled",
                        )
                        task.finished_at = utc_now()

    async def _recover_from_failure(
        self,
        record: RunRecord,
        failed: TaskContract,
        run_router: PredictiveRouter,
        reserved_cost: float = 0.0,
        exception: Exception | None = None,
    ) -> None:
        if not evidence_recovery_enabled():
            raise RuntimeError(
                "高级恢复已停用；失败节点保持拒绝状态"
            )
        failure = self.failure_classifier.classify(
            task=failed,
            validation=failed.validation,
            exception=exception,
            result=failed.result,
        )
        remaining_budget = max(
            0.0,
            record.request.budget
            - record.spent
            - reserved_cost,
        )
        estimated_recovery_cost = max(
            0.000001,
            failed.estimated_cost
            * (
                1.0
                if failure.classification == "transient"
                else 1.22
            ),
        )
        elapsed = self._elapsed_seconds(record)
        decision = self.recovery_policy.decide(
            failure,
            RecoveryContext(
                recovery_count=record.recovery_count,
                max_recoveries=record.request.max_recoveries,
                task_attempt=failed.attempt,
                max_task_attempts=record.request.max_task_attempts,
                remaining_budget=remaining_budget,
                estimated_recovery_cost=estimated_recovery_cost,
                elapsed_seconds=elapsed,
                time_limit_seconds=(
                    record.request.recovery_time_limit_seconds
                ),
            ),
        )
        descendants = self.validator.descendants(
            record.plan.tasks, failed.id
        )
        affected = (
            {failed.id}
            if decision.scope == "node"
            else {failed.id, *descendants}
        )
        if decision.scope == "global":
            affected = {task.id for task in record.plan.tasks}
        patch = None
        if decision.action in {
            RecoveryAction.UPGRADE_NODE.value,
            RecoveryAction.REPLAN_SUBGRAPH.value,
            RecoveryAction.REPLAN_GLOBAL.value,
        }:
            patch = PlanPatch.propose(
                base_revision=record.plan.version,
                scope=decision.scope,
                failed_task_id=failed.id,
                affected_task_ids=affected,
                rationale=decision.reason,
            )
            patch.validate()

        entry = {
            "failure": failure.to_dict(),
            "decision": decision.to_dict(),
            "affected_task_ids": sorted(affected),
            "plan_patch": (
                patch.to_dict() if patch is not None else None
            ),
        }
        failed.failure_record = failure.to_dict()
        failed.recovery_history.append(entry)
        record.recovery_history.append(entry)
        if record.control_kernel is not None:
            record.control_kernel.record_failure(
                record,
                failed,
                failure,
                decision,
                sorted(affected),
                patch,
            )
        if not decision.allowed:
            self._event(
                record,
                "recovery_stopped",
                f"{failed.title} 恢复停止：{decision.reason}",
                failed.id,
                entry,
            )
            raise RuntimeError(f"恢复停止：{decision.reason}")

        record.recovery_count += 1
        if decision.action == RecoveryAction.RETRY_NODE.value:
            set_task_status(
                record,
                failed,
                TaskStatus.PENDING,
                source="engine.recovery.retry_node",
            )
            failed.result = None
            failed.validation = None
            failed.queue_state = "pending"
            failed.blocked_reason = ""
            failed.started_at = None
            failed.finished_at = None
            self._event(
                record,
                "recovery_retry_node",
                f"{failed.title} 判定为临时故障，仅重试该节点",
                failed.id,
                entry,
            )
            self._checkpoint(record, "engine.recovery.retry_node")
            return

        await self._global_replan(
            record,
            failed,
            run_router,
            reserved_cost=reserved_cost,
            failure=failure,
            decision=decision,
            patch=patch,
            affected=affected,
        )

    async def _global_replan(
        self,
        record: RunRecord,
        failed: TaskContract,
        run_router: PredictiveRouter,
        reserved_cost: float = 0.0,
        *,
        failure: FailureRecord | None = None,
        decision: RecoveryDecision | None = None,
        patch: PlanPatch | None = None,
        affected: set[str] | None = None,
    ) -> None:
        set_run_status(
            record,
            RunStatus.REPLANNING,
            source="engine.run.replanning",
        )
        record.replans += 1
        descendants = self.validator.descendants(record.plan.tasks, failed.id)
        affected = affected or {failed.id, *descendants}
        if artifact_dag_enabled() and record.control_kernel is not None:
            if (
                decision is None
                or decision.action
                != RecoveryAction.REPLAN_GLOBAL.value
            ):
                affected = self.graph_compiler.affected_subgraph(
                    record.plan, {failed.id}
                )
            invalidation_scope = {
                task.id
                for task in record.plan.tasks
                if (
                    task.id in affected
                    and task.status != TaskStatus.VALIDATED
                )
            }
            record.control_kernel.invalidate_tasks(
                record,
                invalidation_scope,
                reason=f"task {failed.id} failed validation",
            )
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
                task.input_artifact_refs = []
                task.handoff_message_id = None
        set_task_status(
            record,
            failed,
            TaskStatus.INVALIDATED,
            source="engine.task.invalidated",
        )
        replacement = self.planner.replan(record.plan, failed.id)
        if (
            decision is not None
            and decision.action
            == RecoveryAction.REPLAN_GLOBAL.value
        ):
            for task in record.plan.tasks:
                if (
                    task.id != replacement.id
                    and task.status == TaskStatus.PENDING
                    and replacement.id not in task.dependencies
                ):
                    task.dependencies.append(replacement.id)
                    task.dependency_types[
                        replacement.id
                    ] = "hard"
        if artifact_dag_enabled() and record.control_kernel is not None:
            record.control_kernel.compile_graph(record)
        remaining_budget = max(0.0, record.request.budget - record.spent - reserved_cost)
        pending_for_route = [task for task in record.plan.tasks if task.status == TaskStatus.PENDING]
        replacement_summary = run_router.assign(
            pending_for_route,
            record.request.mode,
            record.request.quality_floor,
            remaining_budget,
            max_concurrency=record.request.max_concurrency,
            deadline_seconds=record.request.deadline_seconds,
            recovery_budget_ratio=0.0,
            value_of_time_per_second=(
                record.request.value_of_time_per_second
            ),
            reserve_recovery_budget=False,
            availability=self._resource_availability(record),
        )
        record.plan.routing_summary = {
            **replacement_summary,
            "replanned": True,
            "reserved_in_flight_usd": round(reserved_cost, 6),
        }
        if (
            placement_v4_enabled()
            and record.control_kernel is not None
        ):
            self._checkpoint(record, "engine.replan.placements")
            record.control_kernel.sync_placements(record)
        self._event(record, "replanning", f"高级模型候选重新规划；保留已验收成果，替换 {failed.id}", failed.id, {
            "replacement": replacement.id,
            "affected_descendants": sorted(affected - {failed.id}),
            "selected_model": replacement.selected_worker,
            "routing": replacement_summary,
            "recovery_action": (
                decision.action if decision is not None else "legacy"
            ),
            "failure_class": (
                failure.classification
                if failure is not None else "legacy"
            ),
            "plan_patch": (
                patch.to_dict() if patch is not None else None
            ),
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
    def _elapsed_seconds(record: RunRecord) -> float:
        if not record.started_at:
            return 0.0
        return max(
            0.0,
            (
                datetime.fromisoformat(
                    utc_now().replace("Z", "+00:00")
                )
                - datetime.fromisoformat(
                    record.started_at.replace("Z", "+00:00")
                )
            ).total_seconds(),
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
    def _raise_if_cancelled(record: RunRecord) -> None:
        if record.cancel_requested or record.status == RunStatus.CANCELLED:
            raise RunCancelled("run cancellation requested")

    @staticmethod
    def _checkpoint(record: RunRecord, source: str) -> None:
        kernel = record.control_kernel
        if kernel is not None:
            kernel.checkpoint(record, source=source)

    def _materialize_legacy_artifacts(self, record: RunRecord) -> None:
        """Upgrade validated v0.4.2 results without re-executing their tasks."""

        kernel = record.control_kernel
        if kernel is None or record.plan is None:
            return
        for task in record.plan.tasks:
            if (
                task.status != TaskStatus.VALIDATED
                or task.result is None
                or task.artifact_ids
            ):
                continue
            kernel.prepare_handoff(record, task)
            kernel.publish_task_artifacts(
                record,
                task,
                task.result,
                validated=True,
            )

    @staticmethod
    def _event(record: RunRecord, kind: str, message: str, task_id: str | None = None, detail: dict | None = None) -> None:
        with record.lock:
            event = Event(utc_now(), kind, message, task_id, detail or {})
            record.events.append(event)
            kernel = record.control_kernel
            if kernel is not None:
                kernel.record_runtime_event(record, event, source=f"engine.event.{kind}")

    @staticmethod
    def _resource_availability(
        record: RunRecord,
    ) -> dict[str, dict[str, Any]] | None:
        if record.control_kernel is None:
            return None
        return {
            item["pool_id"]: dict(item["availability"])
            for item in record.control_kernel.resources()
        }


class RunManager:
    def __init__(
        self,
        catalog_store: CatalogStore | None = None,
        *,
        runtime_db_path: str | None = None,
        recover: bool = True,
    ):
        self.control_kernel = None
        if runtime_v4_enabled():
            from .control_kernel import ControlKernel

            self.control_kernel = ControlKernel(runtime_db_path)
        self.engine = OrchestrationEngine(
            catalog_store,
            control_kernel=self.control_kernel,
        )
        self.records: dict[str, RunRecord] = {}
        self.lock = threading.RLock()
        self.threads: dict[str, threading.Thread] = {}
        if self.control_kernel is not None:
            for record in self.control_kernel.load_records():
                self.records[record.id] = record
            if recover:
                for record in list(self.records.values()):
                    if record.status == RunStatus.CANCELLING:
                        self.control_kernel.set_run_status(
                            record,
                            RunStatus.CANCELLED,
                            source="run_manager.startup_cancel_finalize",
                        )
                    elif record.status not in {
                        RunStatus.COMPLETED,
                        RunStatus.FAILED,
                        RunStatus.CANCELLED,
                    }:
                        self._start(record, resume=True)

    def create(
        self,
        request: RunRequest,
        *,
        idempotency_key: str | None = None,
        retry_of: str | None = None,
    ) -> RunRecord:
        request = request.normalized()
        if self.control_kernel is not None:
            key = idempotency_key or f"legacy-create:{uuid.uuid4().hex}"
            record, replayed = self.control_kernel.create_run(
                request,
                idempotency_key=key,
                retry_of=retry_of,
            )
        else:
            run_id = (
                str(new_run_id())
                if contracts_v4_enabled()
                else uuid.uuid4().hex[:12]
            )
            record = RunRecord(id=run_id, request=request, retry_of=retry_of)
            replayed = False
        with self.lock:
            if replayed and record.id in self.records:
                return self.records[record.id]
            self.records[record.id] = record
        if not replayed and record.status == RunStatus.CREATED:
            self._start(record, resume=False)
        return record

    def _start(self, record: RunRecord, *, resume: bool) -> None:
        with self.lock:
            existing = self.threads.get(record.id)
            if existing is not None and existing.is_alive():
                return
            target = self.engine.resume if resume else self.engine.run
            thread = threading.Thread(
                target=lambda: asyncio.run(target(record)),
                daemon=True,
                name=f"run-{record.id}",
            )
            self.threads[record.id] = thread
            thread.start()

    def cancel(
        self,
        run_id: str,
        *,
        idempotency_key: str,
        expected_revision: int,
    ) -> RunRecord:
        record = self.get(run_id)
        if record is None:
            raise KeyError(run_id)
        if self.control_kernel is None:
            raise RuntimeError("persistent runtime is disabled")
        return self.control_kernel.cancel_run(
            record,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
        )

    def retry(
        self,
        run_id: str,
        *,
        idempotency_key: str,
    ) -> RunRecord:
        original = self.get(run_id)
        if original is None:
            raise KeyError(run_id)
        if original.status not in {RunStatus.FAILED, RunStatus.CANCELLED}:
            raise ValueError("only failed or cancelled runs can be retried")
        return self.create(
            original.request,
            idempotency_key=(
                "retry:"
                + hashlib.sha256(
                    f"{run_id}:{idempotency_key}".encode("utf-8")
                ).hexdigest()
            ),
            retry_of=run_id,
        )

    def get(self, run_id: str) -> RunRecord | None:
        with self.lock:
            return self.records.get(run_id)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            records = list(self.records.values())
        return [record.snapshot() for record in reversed(records[-20:])]

    def events(
        self,
        run_id: str,
        *,
        after_revision: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if self.control_kernel is None:
            record = self.get(run_id)
            return [] if record is None else [event.to_dict() for event in record.events]
        return self.control_kernel.events(
            run_id,
            after_revision=after_revision,
            limit=limit,
        )

    def health(self) -> dict[str, Any]:
        if self.control_kernel is None:
            return {"persistent": False, "runtime_schema": None}
        return self.control_kernel.health()

    def task(self, task_id: str) -> dict[str, Any] | None:
        if self.control_kernel is None:
            return None
        return self.control_kernel.task(task_id)

    def artifact(self, artifact_id: str) -> dict[str, Any] | None:
        if self.control_kernel is None:
            return None
        return self.control_kernel.artifact(artifact_id)

    def artifacts(
        self,
        run_id: str,
        *,
        include_content: bool = False,
    ) -> list[dict[str, Any]]:
        if self.control_kernel is None:
            return []
        return self.control_kernel.artifacts(
            run_id, include_content=include_content
        )

    def graph(self, run_id: str) -> dict[str, Any] | None:
        record = self.get(run_id)
        if record is None or self.control_kernel is None:
            return None
        return self.control_kernel.graph(record)

    def resources(self) -> list[dict[str, Any]]:
        if self.control_kernel is None:
            return []
        return self.control_kernel.resources()

    def placements(self, run_id: str) -> list[dict[str, Any]]:
        if self.get(run_id) is None or self.control_kernel is None:
            return []
        return self.control_kernel.placements(run_id)

    def ledger(
        self, *, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        if self.control_kernel is None:
            return []
        return self.control_kernel.ledger(run_id=run_id)

    def validations(self, run_id: str) -> list[dict[str, Any]]:
        if self.get(run_id) is None or self.control_kernel is None:
            return []
        return self.control_kernel.validations(run_id)

    def failures(self, run_id: str) -> list[dict[str, Any]]:
        if self.get(run_id) is None or self.control_kernel is None:
            return []
        return self.control_kernel.failures(run_id)

    def forecast(self, run_id: str) -> dict[str, Any] | None:
        if self.get(run_id) is None or self.control_kernel is None:
            return None
        return self.control_kernel.forecast(run_id)

    def planning(self, run_id: str) -> dict[str, Any] | None:
        record = self.get(run_id)
        if record is None:
            return None
        with record.lock:
            return {
                "run_id": str(record.id),
                "decision": record.planning_decision,
                "proposals": list(record.planning_proposals),
            }

    def evaluations(self) -> list[dict[str, Any]]:
        if self.control_kernel is None:
            return []
        return self.control_kernel.evaluations()

    def media_project(self, run_id: str) -> dict[str, Any] | None:
        if self.get(run_id) is None or self.control_kernel is None:
            return None
        return self.control_kernel.media_project(run_id)

    def media_shots(self, run_id: str) -> list[dict[str, Any]]:
        if self.get(run_id) is None or self.control_kernel is None:
            return []
        return self.control_kernel.media_shots(run_id)

    def approve_media_shot(
        self, shot_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        if self.control_kernel is None:
            raise RuntimeError("persistent runtime is disabled")
        return self.control_kernel.approve_media_shot(
            shot_id, idempotency_key=idempotency_key
        )

    def approve_media_project(
        self, run_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        if self.get(run_id) is None:
            raise KeyError(run_id)
        if self.control_kernel is None:
            raise RuntimeError("persistent runtime is disabled")
        return self.control_kernel.approve_media_project(
            run_id, idempotency_key=idempotency_key
        )

    def approvals(
        self, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        if self.control_kernel is None:
            return []
        return self.control_kernel.approvals(status=status)

    def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        note: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if self.control_kernel is None:
            raise RuntimeError("persistent runtime is disabled")
        approval = self.control_kernel.approval_store.get(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        record = self.get(str(approval["run_id"]))
        if record is None:
            raise KeyError(str(approval["run_id"]))
        return self.control_kernel.resolve_approval(
            record,
            approval_id,
            decision=decision,
            note=note,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    def shutdown(self, *, wait_timeout: float = 10.0) -> None:
        """Let in-flight simulated work reach a persisted checkpoint on shutdown."""

        deadline = time.time() + max(0.0, float(wait_timeout))
        with self.lock:
            threads = list(self.threads.values())
        for thread in threads:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
