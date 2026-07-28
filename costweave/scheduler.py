"""Critical-path scheduler and v0.4.4 runtime admission facade."""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .admission import (
    AdmissionCode,
    AdmissionController,
    CircuitBreaker,
    ResourceRequest,
    TokenBucket,
)
from .domain import Plan, TaskContract, TaskStatus, WorkerProfile
from .reservations import CapacityUnavailable, Reservation
from .resource_inventory import ResourceInventory, ResourcePool, pool_id


SCHEDULER_POLICY_ENV = "COSTWEAVE_SCHEDULER_POLICY"


class SchedulerError(RuntimeError):
    pass


class ImpossibleAdmission(SchedulerError):
    pass


class ApprovalRejected(SchedulerError):
    pass


class TransientExecutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StartDecision:
    state: str
    reason: str
    reservation: Reservation | None = None
    retry_after_ms: int = 0


class ParallelScheduler:
    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        self.pools: dict[tuple[str, bool], ResourcePool] = {}
        self.buckets: dict[str, TokenBucket] = {}
        self.circuits: dict[str, CircuitBreaker] = {}
        self.policy = os.environ.get(
            SCHEDULER_POLICY_ENV, "critical-path"
        ).strip().lower()

    def configure(
        self,
        workers: tuple[WorkerProfile, ...] | list[WorkerProfile],
        *,
        max_concurrency: int,
    ) -> None:
        for pool in ResourceInventory.from_workers(
            workers, max_concurrency=max_concurrency
        ):
            self.pools[(pool.provider, pool.local)] = pool
            self.kernel.reservations.register_pool(pool)
            self.buckets.setdefault(
                pool.pool_id,
                TokenBucket(
                    pool.token_capacity, pool.token_refill_per_second
                ),
            )
            if pool.provider not in self.circuits:
                circuit = CircuitBreaker(clock=time.time)
                stored = self.kernel.reservations.load_circuit(
                    pool.provider
                )
                if stored is not None:
                    circuit.restore(stored)
                self.circuits[pool.provider] = circuit

    def order_ready(
        self,
        tasks: list[TaskContract],
        ready: list[TaskContract],
    ) -> list[TaskContract]:
        if self.policy == "fifo":
            return [task for task in tasks if task in ready]
        scores = self.critical_path_scores(tasks)
        return sorted(
            ready,
            key=lambda task: (
                scores.get(task.id, 0),
                task.priority,
                task.criticality,
            ),
            reverse=True,
        )

    @staticmethod
    def critical_path_scores(
        tasks: list[TaskContract],
    ) -> dict[str, int]:
        children: dict[str, list[str]] = defaultdict(list)
        indegree = {task.id: len(task.dependencies) for task in tasks}
        by_id = {task.id: task for task in tasks}
        for task in tasks:
            for dependency in task.dependencies:
                children[dependency].append(task.id)
        queue = deque(
            task_id for task_id, count in indegree.items() if count == 0
        )
        order: list[str] = []
        while queue:
            current = queue.popleft()
            order.append(current)
            for child in children[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(order) != len(tasks):
            raise SchedulerError("scheduler received a cyclic DAG")
        scores: dict[str, int] = {}
        for task_id in reversed(order):
            own = max(1, int(by_id[task_id].estimated_latency_ms))
            scores[task_id] = own + max(
                (scores[child] for child in children[task_id]),
                default=0,
            )
        return scores

    def start_decision(
        self,
        record: Any,
        task: TaskContract,
        worker: WorkerProfile,
    ) -> StartDecision:
        if task.requires_approval:
            approval, _ = self.kernel.approval_store.ensure(
                run_id=str(record.id),
                task_id=str(task.id),
                kind=task.approval_kind,
                reason=task.approval_reason
                or "task contract requires explicit approval",
            )
            task.approval_id = approval["approval_id"]
            if approval["status"] == "pending":
                return StartDecision(
                    "waiting_approval", approval["reason"], None, 50
                )
            if approval["status"] == "rejected":
                raise ApprovalRejected(
                    f"approval rejected for {task.id}: "
                    f"{approval.get('resolution_note') or ''}"
                )
        pool = self.pools.get((worker.provider, worker.local))
        if pool is None:
            raise ImpossibleAdmission(
                f"no resource pool for {worker.provider}"
            )
        request = ResourceRequest(
            slots=max(1, task.resource_slots),
            cpu_cores=max(0.0, task.cpu_cores),
            gpu_count=max(0, task.gpu_count),
            vram_mb=max(0, task.vram_mb),
            tokens=max(
                0,
                task.estimated_input_tokens
                + task.estimated_output_tokens,
            ),
        )
        available = self.kernel.reservations.availability(pool.pool_id)
        deadline_ms = self._deadline_remaining_ms(record)
        decision = AdmissionController.evaluate(
            pool,
            request,
            available_slots=available["slots"],
            available_cpu=available["cpu_cores"],
            available_gpu=available["gpu_count"],
            available_vram=available["vram_mb"],
            queue_depth=available["queue_depth"],
            deadline_remaining_ms=deadline_ms,
            estimated_latency_ms=task.estimated_latency_ms,
            bucket=self.buckets[pool.pool_id],
            circuit=self.circuits[worker.provider],
        )
        self.kernel.reservations.save_circuit(
            worker.provider,
            self.circuits[worker.provider].snapshot(),
        )
        if decision.code in {
            AdmissionCode.IMPOSSIBLE_CAPACITY,
            AdmissionCode.DEADLINE_IMPOSSIBLE,
        }:
            raise ImpossibleAdmission(
                f"{decision.code.value}: {decision.reason}"
            )
        if not decision.accepted:
            return StartDecision(
                "queued",
                decision.reason,
                None,
                decision.retry_after_ms,
            )
        try:
            reservation = self.kernel.reservations.reserve(
                run_id=str(record.id),
                task_id=str(task.id),
                attempt=task.attempt + 1,
                pool=pool,
                request=request,
                estimated_cost=task.estimated_cost,
                budget=record.request.budget,
                spent=record.spent,
            )
        except CapacityUnavailable:
            return StartDecision(
                "queued", "capacity changed during reservation", None, 25
            )
        return StartDecision("admitted", "resources reserved", reservation)

    async def execute_with_retry(
        self,
        execute,
        *,
        provider: str,
        max_retries: int = 2,
        on_retry=None,
    ):
        attempts = 0
        while True:
            try:
                result = await execute()
                self.circuits[provider].success()
                self.kernel.reservations.save_circuit(
                    provider, self.circuits[provider].snapshot()
                )
                return result
            except TransientExecutionError:
                attempts += 1
                self.circuits[provider].failure()
                self.kernel.reservations.save_circuit(
                    provider, self.circuits[provider].snapshot()
                )
                if attempts > max_retries:
                    raise
                if on_retry is not None:
                    on_retry(attempts)
                await asyncio.sleep(0.02 * attempts)

    @staticmethod
    def _deadline_remaining_ms(record: Any) -> int | None:
        if record.request.deadline_seconds is None or not record.started_at:
            return None
        started = datetime.fromisoformat(
            record.started_at.replace("Z", "+00:00")
        )
        elapsed = (
            datetime.now(timezone.utc) - started
        ).total_seconds()
        return max(
            0,
            round((record.request.deadline_seconds - elapsed) * 1000),
        )
