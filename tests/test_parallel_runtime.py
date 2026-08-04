import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from costweave.admission import (
    AdmissionCode,
    AdmissionController,
    CircuitBreaker,
    CircuitState,
    ResourceRequest,
    TokenBucket,
)
from costweave.approvals import (
    ApprovalConflictError,
    ApprovalStore,
)
from costweave.contracts_v4 import canonical_identifier, utc_now
from costweave.domain import (
    RunRequest,
    RunStatus,
    TaskContract,
    TaskStatus,
)
from costweave.engine import RunManager
from costweave.event_store import EventStore
from costweave.executor import SimulatedExecutor
from costweave.model_catalog import MODELS
from costweave.reservations import (
    BudgetUnavailable,
    CapacityUnavailable,
    ReservationManager,
)
from costweave.resource_inventory import (
    ResourceInventory,
    ResourcePool,
    pool_id,
)
from costweave.scheduler import (
    ParallelScheduler,
    SchedulerError,
    TransientExecutionError,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_pool(
    *,
    slots: int = 2,
    cpu: float = 4.0,
    gpu: int = 0,
    vram: int = 0,
    queue_limit: int = 8,
) -> ResourcePool:
    return ResourcePool(
        pool_id="pool_test",
        provider="test-provider",
        local=True,
        slots_total=slots,
        cpu_cores_total=cpu,
        gpu_count_total=gpu,
        vram_mb_total=vram,
        queue_limit=queue_limit,
        token_capacity=1_000,
        token_refill_per_second=100,
    )


def make_task(
    task_id: str,
    dependencies: list[str] | None = None,
    latency: int = 100,
) -> TaskContract:
    return TaskContract(
        id=task_id,
        title=task_id,
        objective=f"complete {task_id}",
        task_type="analysis",
        dependencies=list(dependencies or []),
        estimated_latency_ms=latency,
    )


class AdmissionTests(unittest.TestCase):
    def evaluate(self, request=None, **overrides):
        pool = overrides.pop("pool", make_pool())
        values = {
            "available_slots": pool.slots_total,
            "available_cpu": pool.cpu_cores_total,
            "available_gpu": pool.gpu_count_total,
            "available_vram": pool.vram_mb_total,
            "queue_depth": 0,
            "deadline_remaining_ms": None,
            "estimated_latency_ms": 100,
            "bucket": TokenBucket(1_000, 100),
            "circuit": CircuitBreaker(),
        }
        values.update(overrides)
        return AdmissionController.evaluate(
            pool, request or ResourceRequest(), **values
        )

    def test_token_bucket_consumes_and_refills(self):
        clock = FakeClock()
        bucket = TokenBucket(10, 2, clock=clock)
        self.assertTrue(bucket.consume(9))
        self.assertFalse(bucket.consume(2))
        clock.advance(1)
        self.assertTrue(bucket.consume(2))

    def test_impossible_capacity_is_rejected(self):
        decision = self.evaluate(ResourceRequest(slots=3))
        self.assertEqual(AdmissionCode.IMPOSSIBLE_CAPACITY, decision.code)

    def test_temporarily_occupied_capacity_is_queued(self):
        decision = self.evaluate(
            ResourceRequest(slots=1), available_slots=0
        )
        self.assertEqual(AdmissionCode.QUEUED, decision.code)
        self.assertGreater(decision.retry_after_ms, 0)

    def test_impossible_deadline_is_rejected(self):
        decision = self.evaluate(
            deadline_remaining_ms=99, estimated_latency_ms=100
        )
        self.assertEqual(AdmissionCode.DEADLINE_IMPOSSIBLE, decision.code)

    def test_queue_limit_applies_backpressure(self):
        pool = make_pool(queue_limit=2)
        decision = self.evaluate(pool=pool, queue_depth=2)
        self.assertEqual(AdmissionCode.BACKPRESSURE, decision.code)

    def test_depleted_token_bucket_applies_backpressure(self):
        decision = self.evaluate(
            ResourceRequest(tokens=11),
            bucket=TokenBucket(10, 0),
        )
        self.assertEqual(AdmissionCode.BACKPRESSURE, decision.code)

    def test_admitted_request_consumes_all_gates(self):
        decision = self.evaluate(ResourceRequest(slots=1, tokens=10))
        self.assertTrue(decision.accepted)
        self.assertEqual(AdmissionCode.ADMITTED, decision.code)

    def test_circuit_closed_to_open_transition(self):
        breaker = CircuitBreaker(failure_threshold=2)
        breaker.failure()
        self.assertEqual(CircuitState.CLOSED, breaker.state)
        breaker.failure()
        self.assertEqual(CircuitState.OPEN, breaker.state)

    def test_open_circuit_denies_before_timeout(self):
        clock = FakeClock()
        breaker = CircuitBreaker(
            failure_threshold=1, reset_timeout=5, clock=clock
        )
        breaker.failure()
        self.assertFalse(breaker.allow())

    def test_open_circuit_allows_one_half_open_probe(self):
        clock = FakeClock()
        breaker = CircuitBreaker(
            failure_threshold=1, reset_timeout=5, clock=clock
        )
        breaker.failure()
        clock.advance(5)
        self.assertTrue(breaker.allow())
        self.assertEqual(CircuitState.HALF_OPEN, breaker.state)
        self.assertFalse(breaker.allow())

    def test_half_open_success_closes_circuit(self):
        clock = FakeClock()
        breaker = CircuitBreaker(
            failure_threshold=1, reset_timeout=1, clock=clock
        )
        breaker.failure()
        clock.advance(1)
        breaker.allow()
        breaker.success()
        self.assertEqual(CircuitState.CLOSED, breaker.state)
        self.assertEqual(0, breaker.consecutive_failures)

    def test_half_open_failure_reopens_circuit(self):
        clock = FakeClock()
        breaker = CircuitBreaker(
            failure_threshold=1, reset_timeout=1, clock=clock
        )
        breaker.failure()
        clock.advance(1)
        breaker.allow()
        breaker.failure()
        self.assertEqual(CircuitState.OPEN, breaker.state)
        self.assertEqual(2, breaker.consecutive_failures)

    def test_circuit_snapshot_restores_without_stale_probe(self):
        breaker = CircuitBreaker(failure_threshold=1)
        breaker.failure()
        snapshot = breaker.snapshot()
        snapshot["probe_in_flight"] = True
        restored = CircuitBreaker()
        restored.restore(snapshot)
        self.assertEqual(CircuitState.OPEN, restored.state)
        self.assertFalse(restored.probe_in_flight)


class SchedulerAndInventoryTests(unittest.TestCase):
    class Kernel:
        pass

    def scheduler(self):
        return ParallelScheduler(self.Kernel())

    def test_critical_path_prioritizes_longer_branch(self):
        root = make_task("root")
        short = make_task("short", ["root"], 10)
        long = make_task("long", ["root"], 100)
        long_tail = make_task("long-tail", ["long"], 100)
        scheduler = self.scheduler()
        ordered = scheduler.order_ready(
            [root, short, long, long_tail], [short, long]
        )
        self.assertEqual(["long", "short"], [task.id for task in ordered])

    def test_fifo_compatibility_policy_preserves_plan_order(self):
        first = make_task("first")
        second = make_task("second")
        with patch.dict(
            os.environ, {"COSTWEAVE_SCHEDULER_POLICY": "fifo"}
        ):
            scheduler = self.scheduler()
        ordered = scheduler.order_ready(
            [first, second], [second, first]
        )
        self.assertEqual(["first", "second"], [task.id for task in ordered])

    def test_thousand_node_dag_scores_without_deadlock(self):
        tasks = [
            make_task(
                f"node-{index}",
                [f"node-{index - 1}"] if index else [],
                1,
            )
            for index in range(1_000)
        ]
        scores = self.scheduler().critical_path_scores(tasks)
        self.assertEqual(1_000, scores["node-0"])
        self.assertEqual(1, scores["node-999"])

    def test_cyclic_dag_is_rejected(self):
        tasks = [
            make_task("a", ["b"]),
            make_task("b", ["a"]),
        ]
        with self.assertRaises(SchedulerError):
            self.scheduler().critical_path_scores(tasks)

    def test_inventory_groups_workers_by_provider_and_locality(self):
        pools = ResourceInventory.from_workers(MODELS, max_concurrency=4)
        expected = {(worker.provider, worker.local) for worker in MODELS}
        self.assertEqual(
            expected, {(item.provider, item.local) for item in pools}
        )

    def test_local_inventory_capacity_is_bounded(self):
        pools = ResourceInventory.from_workers(MODELS, max_concurrency=12)
        local = [item for item in pools if item.local]
        self.assertTrue(local)
        self.assertTrue(all(1 <= item.slots_total <= 4 for item in local))

    def test_pool_id_is_stable_and_separates_locality(self):
        self.assertEqual(pool_id("x", True), pool_id("x", True))
        self.assertNotEqual(pool_id("x", True), pool_id("x", False))


class PersistentRuntimeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        EventStore(self.path)
        self.run_id = "run_parallel_runtime_test"
        self.task_ids = {}
        now = utc_now()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "INSERT INTO runs(run_id,revision,status,request_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (
                    self.run_id,
                    1,
                    "executing",
                    json.dumps({"goal": "test"}),
                    now,
                    now,
                ),
            )
            for legacy in ("task-a", "task-b"):
                task_id = canonical_identifier(
                    "task", f"{self.run_id}:{legacy}"
                )
                self.task_ids[legacy] = task_id
                connection.execute(
                    "INSERT INTO tasks(run_id,task_id,legacy_task_id,"
                    "revision,status,task_json) VALUES(?,?,?,?,?,?)",
                    (
                        self.run_id,
                        task_id,
                        legacy,
                        1,
                        "pending",
                        "{}",
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        self.reservations = ReservationManager(self.path)
        self.approvals = ApprovalStore(self.path)
        self.pool = make_pool(slots=1, cpu=2)
        self.reservations.register_pool(self.pool)

    def tearDown(self):
        self.temp.cleanup()

    def reserve(self, task="task-a", attempt=1, **overrides):
        values = {
            "run_id": self.run_id,
            "task_id": task,
            "attempt": attempt,
            "pool": self.pool,
            "request": ResourceRequest(slots=1, cpu_cores=1),
            "estimated_cost": 0.2,
            "budget": 1.0,
            "spent": 0.0,
        }
        values.update(overrides)
        return self.reservations.reserve(**values)

    def test_reserve_and_release_updates_availability(self):
        reservation = self.reserve()
        self.assertEqual(0, self.reservations.availability("pool_test")["slots"])
        self.assertTrue(
            self.reservations.release(
                reservation.reservation_id,
                reason="completed",
                actual_cost=0.1,
            )
        )
        self.assertEqual(1, self.reservations.availability("pool_test")["slots"])

    def test_duplicate_attempt_replays_one_reservation(self):
        first = self.reserve()
        second = self.reserve()
        self.assertEqual(first.reservation_id, second.reservation_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(1, self.reservations.counts()["resource_reservations"])

    def test_concurrent_duplicate_reservations_are_atomic(self):
        results = []
        errors = []

        def reserve():
            try:
                results.append(self.reserve())
            except Exception as exc:  # pragma: no cover - assertion reports
                errors.append(exc)

        threads = [threading.Thread(target=reserve) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual(1, len({item.reservation_id for item in results}))
        self.assertEqual(1, sum(not item.replayed for item in results))

    def test_capacity_reservation_rejects_second_task(self):
        self.reserve("task-a")
        with self.assertRaises(CapacityUnavailable):
            self.reserve("task-b")

    def test_cost_reservation_rejects_budget_overrun(self):
        with self.assertRaises(BudgetUnavailable):
            self.reserve(estimated_cost=0.6, budget=1.0, spent=0.5)

    def test_release_is_idempotent(self):
        reservation = self.reserve()
        self.assertTrue(
            self.reservations.release(
                reservation.reservation_id, reason="done"
            )
        )
        self.assertFalse(
            self.reservations.release(
                reservation.reservation_id, reason="done-again"
            )
        )

    def test_startup_recovers_orphan_lease(self):
        self.reserve()
        from costweave.control_kernel import ControlKernel

        kernel = ControlKernel(self.path)
        self.assertEqual(1, kernel.recovered_orphan_leases)
        self.assertEqual(0, self.reservations.summary(self.run_id)["active_leases"])

    def test_resources_include_live_availability(self):
        resources = self.reservations.resources()
        self.assertEqual(1, len(resources))
        self.assertEqual(1, resources[0]["availability"]["slots"])
        self.assertTrue(resources[0]["local"])

    def test_approval_creation_is_idempotent(self):
        first, replayed_first = self.approvals.ensure(
            run_id=self.run_id,
            task_id="task-a",
            kind="deployment",
            reason="production change",
        )
        second, replayed_second = self.approvals.ensure(
            run_id=self.run_id,
            task_id="task-a",
            kind="deployment",
            reason="production change",
        )
        self.assertFalse(replayed_first)
        self.assertTrue(replayed_second)
        self.assertEqual(first["approval_id"], second["approval_id"])

    def test_approval_resolution_is_idempotent(self):
        approval, _ = self.approvals.ensure(
            run_id=self.run_id,
            task_id="task-a",
            kind="deployment",
            reason="production change",
        )
        first, replayed_first = self.approvals.resolve(
            approval["approval_id"],
            decision="approved",
            note="safe",
            expected_revision=1,
            idempotency_key="approval-resolution-key-0001",
        )
        second, replayed_second = self.approvals.resolve(
            approval["approval_id"],
            decision="approved",
            note="safe",
            expected_revision=1,
            idempotency_key="approval-resolution-key-0001",
        )
        self.assertFalse(replayed_first)
        self.assertTrue(replayed_second)
        self.assertEqual(2, first["revision"])
        self.assertEqual(first, second)

    def test_approval_stale_revision_is_rejected(self):
        approval, _ = self.approvals.ensure(
            run_id=self.run_id,
            task_id="task-a",
            kind="deployment",
            reason="production change",
        )
        with self.assertRaises(ApprovalConflictError):
            self.approvals.resolve(
                approval["approval_id"],
                decision="approved",
                note="safe",
                expected_revision=0,
                idempotency_key="approval-stale-key-0001",
            )

    def test_approval_key_cannot_change_decision(self):
        approval, _ = self.approvals.ensure(
            run_id=self.run_id,
            task_id="task-a",
            kind="deployment",
            reason="production change",
        )
        self.approvals.resolve(
            approval["approval_id"],
            decision="approved",
            note="safe",
            expected_revision=1,
            idempotency_key="approval-conflict-key-0001",
        )
        with self.assertRaises(ApprovalConflictError):
            self.approvals.resolve(
                approval["approval_id"],
                decision="rejected",
                note="changed",
                expected_revision=2,
                idempotency_key="approval-conflict-key-0001",
            )

    def test_approval_list_filters_status_and_run(self):
        approval, _ = self.approvals.ensure(
            run_id=self.run_id,
            task_id="task-a",
            kind="deployment",
            reason="production change",
        )
        self.assertEqual(
            [approval["approval_id"]],
            [
                item["approval_id"]
                for item in self.approvals.list(
                    status="pending", run_id=self.run_id
                )
            ],
        )
        self.assertEqual([], self.approvals.list(status="approved"))


class PlannerDecorator:
    def __init__(self, delegate, decorate):
        self.delegate = delegate
        self.decorate = decorate

    def plan(self, request):
        plan = self.delegate.plan(request)
        self.decorate(plan)
        return plan

    def replan(self, plan, failed_task_id):
        return self.delegate.replan(plan, failed_task_id)


class RetryTwiceExecutor(SimulatedExecutor):
    def __init__(self):
        self.failures = 0

    async def execute(
        self,
        task,
        worker,
        dependency_results,
        inject_fatal_error=False,
    ):
        if self.failures < 2:
            self.failures += 1
            raise TransientExecutionError("temporary provider timeout")
        return await super().execute(
            task, worker, dependency_results, inject_fatal_error
        )


class ParallelRuntimeIntegrationTests(unittest.TestCase):
    @staticmethod
    def wait(record, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if record.status in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                return
            time.sleep(0.025)
        raise AssertionError(f"run did not finish: {record.status}")

    def manager(self):
        temp = tempfile.TemporaryDirectory()
        manager = RunManager(
            runtime_db_path=str(Path(temp.name) / "runtime.sqlite3"),
            recover=False,
        )
        return temp, manager

    def test_impossible_capacity_fails_before_first_attempt(self):
        temp, manager = self.manager()
        try:
            def decorate(plan):
                plan.tasks[0].gpu_count = 99
                plan.tasks[0].vram_mb = 1_000_000

            manager.engine.planner = PlannerDecorator(
                manager.engine.planner, decorate
            )
            record = manager.create(
                RunRequest("分析产品需求", budget=2),
                idempotency_key="capacity-precheck-run-0001",
            )
            self.wait(record)
            self.assertEqual(RunStatus.FAILED, record.status)
            self.assertIn("impossible_capacity", record.error)
            self.assertEqual(0, record.plan.tasks[0].attempt)
            self.assertEqual(
                0,
                manager.control_kernel.reservations.summary(
                    record.id
                )["reservations"],
            )
        finally:
            manager.shutdown()
            temp.cleanup()

    def test_transient_execution_retries_then_completes(self):
        temp, manager = self.manager()
        try:
            executor = RetryTwiceExecutor()
            manager.engine.executor = executor
            record = manager.create(
                RunRequest("分析产品需求并形成报告", budget=2),
                idempotency_key="transient-retry-run-0001",
            )
            self.wait(record)
            self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
            self.assertEqual(2, executor.failures)
            self.assertEqual(
                2,
                max(
                    task.transient_retries
                    for task in record.plan.tasks
                ),
            )
            circuits = manager.control_kernel.reservations.counts()[
                "provider_circuits"
            ]
            self.assertGreater(circuits, 0)
        finally:
            manager.shutdown()
            temp.cleanup()

    def test_approval_blocks_only_its_own_parallel_branch(self):
        temp, manager = self.manager()
        try:
            blocked_id = []

            def decorate(plan):
                branch = next(
                    task
                    for task in plan.tasks
                    if task.id.startswith("branch-")
                )
                branch.requires_approval = True
                branch.approval_kind = "high-impact-execution"
                branch.approval_reason = "test nonblocking approval"
                blocked_id.append(branch.id)

            manager.engine.planner = PlannerDecorator(
                manager.engine.planner, decorate
            )
            record = manager.create(
                RunRequest(
                    "研究并开发一个数据产品，最终撰写报告",
                    budget=3,
                    max_concurrency=4,
                ),
                idempotency_key="nonblocking-approval-run-0001",
            )
            deadline = time.time() + 18
            approval = None
            unrelated_finished = False
            while time.time() < deadline:
                pending = manager.approvals(status="pending")
                if pending:
                    approval = pending[0]
                    unrelated_finished = any(
                        task.id != blocked_id[0]
                        and task.id.startswith("branch-")
                        and task.status == TaskStatus.VALIDATED
                        for task in record.plan.tasks
                    )
                    if unrelated_finished:
                        break
                time.sleep(0.025)
            self.assertIsNotNone(approval)
            self.assertTrue(unrelated_finished)
            blocked = next(
                task for task in record.plan.tasks
                if task.id == blocked_id[0]
            )
            self.assertEqual(TaskStatus.PENDING, blocked.status)
            self.assertEqual("waiting_approval", blocked.queue_state)
            manager.resolve_approval(
                approval["approval_id"],
                decision="approved",
                note="approved by test",
                expected_revision=approval["revision"],
                idempotency_key="nonblocking-approval-resolve-0001",
            )
            self.wait(record)
            self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
        finally:
            manager.shutdown()
            temp.cleanup()

    def test_feature_flag_preserves_fifo_compatible_runtime(self):
        temp = tempfile.TemporaryDirectory()
        try:
            with patch.dict(
                os.environ, {"COSTWEAVE_PARALLEL_RUNTIME": "0"}
            ):
                manager = RunManager(
                    runtime_db_path=str(
                        Path(temp.name) / "runtime.sqlite3"
                    ),
                    recover=False,
                )
                try:
                    record = manager.create(
                        RunRequest("分析产品需求", budget=2),
                        idempotency_key="parallel-rollback-run-0001",
                    )
                    self.wait(record)
                    self.assertEqual(
                        RunStatus.COMPLETED, record.status, record.error
                    )
                    self.assertEqual(
                        0,
                        manager.control_kernel.reservations.counts()[
                            "resource_reservations"
                        ],
                    )
                finally:
                    manager.shutdown()
        finally:
            temp.cleanup()


class AsyncRetryTests(unittest.TestCase):
    def test_retry_exhaustion_raises_transient_error(self):
        class Reservations:
            def save_circuit(self, *args):
                return None

        class Kernel:
            reservations = Reservations()

        scheduler = ParallelScheduler(Kernel())
        scheduler.circuits["provider"] = CircuitBreaker(
            failure_threshold=10
        )
        calls = 0

        async def failing():
            nonlocal calls
            calls += 1
            raise TransientExecutionError("still unavailable")

        with self.assertRaises(TransientExecutionError):
            asyncio.run(
                scheduler.execute_with_retry(
                    failing, provider="provider", max_retries=2
                )
            )
        self.assertEqual(3, calls)


if __name__ == "__main__":
    unittest.main()
