from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from costweave.kernel.contracts import Job, Operation
from costweave.kernel.ports import ExecutionResult, KnownFailure, Registry, default_registry
from costweave.kernel.runtime import Runtime
from costweave.kernel.store import KernelStore


class KernelParallelRuntimeTests(unittest.TestCase):
    def test_32_thread_contention_never_exceeds_max_parallel_or_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KernelStore(Path(tmp) / "parallel.sqlite3")
            ops = [
                Operation(f"op{i}", [], f"art{i}", "literal", "present", 3, {"value": i})
                for i in range(10)
            ]
            acc = {f"art{i}": {"validator": "present", "config": {}} for i in range(10)}
            job = Job(
                [f"art{i}" for i in range(10)], {}, 10, ops, acceptance=acc, max_parallel=4,
            ).to_dict()
            store.create("contend", job)
            barrier = Barrier(32)

            def try_claim(idx: int) -> str | None:
                local = KernelStore(Path(tmp) / "parallel.sqlite3")
                barrier.wait(5)
                return local.claim("contend", f"op{idx % 10}")

            with ThreadPoolExecutor(max_workers=32) as pool:
                ids = [aid for aid in pool.map(try_claim, range(32)) if aid]

            snap = store.snapshot("contend")
            self.assertLessEqual(len(ids), 3)
            self.assertEqual(len(ids), len(snap["active_attempts"]))
            self.assertLessEqual(snap["spent_units"] + snap["held_units"], 10)
            self.assertEqual(len(ids) * 3, snap["held_units"])

    def test_option_value_parallel_wave_vs_serialization(self) -> None:
        def make_job(*, latency_weight: float) -> Job:
            ops = [
                Operation("modA", ["spec"], "a", "slow", "present", 4, {}, pass_prob=0.55, latency_p50_ms=200),
                Operation("modB", ["spec"], "b", "slow", "present", 4, {}, pass_prob=0.95, latency_p50_ms=200),
                Operation("join", ["a", "b"], "app", "collect", "present", 1, {}, pass_prob=1.0),
            ]
            acc = {
                "a": {"validator": "present", "config": {}},
                "b": {"validator": "present", "config": {}},
                "app": {"validator": "present", "config": {}},
            }
            return Job(
                ["app"], {"spec": 1}, 20, ops, acceptance=acc,
                planning_policy="bsao", latency_weight_units_per_sec=latency_weight,
                max_parallel=2, failure_penalty_units=100,
            )

        with tempfile.TemporaryDirectory() as tmp:
            registry = default_registry()
            concurrency = {"cur": 0, "peak": 0}
            lock = threading.Lock()

            def slow_exec(op, inputs, *, attempt_id, cancel_token=None):
                with lock:
                    concurrency["cur"] += 1
                    concurrency["peak"] = max(concurrency["peak"], concurrency["cur"])
                try:
                    time.sleep(0.20)
                    return {"ok": op.id}
                finally:
                    with lock:
                        concurrency["cur"] -= 1

            registry.register_executor("slow", slow_exec)
            store = KernelStore(Path(tmp) / "wave.sqlite3")
            rt = Runtime(store, registry)

            # High latency weight: dispatches modA and modB in parallel.
            concurrency.update(cur=0, peak=0)
            t0 = time.perf_counter()
            s_par = rt.submit("run-par", make_job(latency_weight=25.0))
            dur_par = time.perf_counter() - t0
            self.assertEqual("complete", s_par["status"])
            self.assertEqual(2, concurrency["peak"])

            # Zero latency weight + risky modA: serializes to preserve stopping option.
            concurrency.update(cur=0, peak=0)
            t1 = time.perf_counter()
            s_ser = rt.submit("run-ser", make_job(latency_weight=0.0))
            dur_ser = time.perf_counter() - t1
            self.assertEqual("complete", s_ser["status"])
            self.assertEqual(1, concurrency["peak"])
            self.assertLess(dur_par, dur_ser * 0.85)

    def test_sibling_failure_fires_cancellation_token_within_50ms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = default_registry()
            cancel_times: list[float] = []
            fail_time: list[float] = []
            both_started = Barrier(2)

            def mod_a_fail(op, inputs, *, attempt_id, cancel_token=None):
                both_started.wait(3.0)
                time.sleep(0.03)
                fail_time.append(time.perf_counter())
                raise KnownFailure("fatal_mod_a", actual_units=1)

            def mod_b_stream(op, inputs, *, attempt_id, cancel_token=None):
                both_started.wait(3.0)
                for step in range(40):
                    if cancel_token is not None and cancel_token.cancelled:
                        cancel_times.append(time.perf_counter())
                        cancel_token.raise_if_cancelled(actual_units=1)
                    time.sleep(0.01)
                return {"done": True}

            registry.register_executor("fail_a", mod_a_fail)
            registry.register_executor("stream_b", mod_b_stream)

            ops = [
                Operation("modA", ["spec"], "a", "fail_a", "present", 4, {}, pass_prob=0.9, latency_p50_ms=50),
                Operation("modB", ["spec"], "b", "stream_b", "present", 4, {}, pass_prob=0.9, latency_p50_ms=50),
                Operation("join", ["a", "b"], "app", "collect", "present", 1, {}, pass_prob=1.0),
            ]
            acc = {
                "a": {"validator": "present", "config": {}},
                "b": {"validator": "present", "config": {}},
                "app": {"validator": "present", "config": {}},
            }
            job = Job(
                ["app"], {"spec": 1}, 20, ops, acceptance=acc,
                planning_policy="bsao", latency_weight_units_per_sec=100.0, max_parallel=2,
            )
            store = KernelStore(Path(tmp) / "cancel.sqlite3")
            state = Runtime(store, registry).submit("cancel-run", job)
            self.assertEqual("blocked", state["status"])
            self.assertEqual(2, state["spent_units"])
            self.assertTrue(cancel_times, "CancellationToken did not trigger on sibling modB")
            delay_ms = (cancel_times[0] - fail_time[0]) * 1000.0
            self.assertLess(delay_ms, 50.0, f"cancellation took {delay_ms:.1f}ms")

    def test_backup_and_verify_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "main.sqlite3"
            backup_path = Path(tmp) / "backup.sqlite3"
            store = KernelStore(db_path)
            job = Job(
                ["out"], {"in": 1}, 2,
                [Operation("op1", ["in"], "out", "literal", "present", 1, {"value": 99})],
                acceptance={"out": {"validator": "present", "config": {}}},
            )
            Runtime(store).submit("bkp-run", job)
            report = store.backup_and_verify(backup_path)
            self.assertTrue(report["healthy"])
            self.assertTrue(report["logical_hash_match"])
            restored = KernelStore(backup_path)
            self.assertEqual(store.snapshot("bkp-run"), restored.snapshot("bkp-run"))
            self.assertEqual(store.events("bkp-run"), restored.events("bkp-run"))

    def test_inflight_parallel_siblings_do_not_trigger_premature_expensive_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = default_registry()
            called: list[str] = []

            def fast_exec(op, inp, *, attempt_id):
                called.append("fast")
                time.sleep(0.01)
                return {"left": 1}

            def slow_exec(op, inp, *, attempt_id):
                called.append("slow")
                time.sleep(0.08)
                return {"right": 2}

            def expensive_exec(op, inp, *, attempt_id):
                called.append("expensive")
                return {"direct": True}

            reg.register_executor("fast_op", fast_exec)
            reg.register_executor("slow_op", slow_exec)
            reg.register_executor("exp_op", expensive_exec)

            ops = [
                Operation("branch_a", ["spec"], "a", "fast_op", "present", 2, {}, pass_prob=0.95, latency_p50_ms=300),
                Operation("branch_b", ["spec"], "b", "slow_op", "present", 2, {}, pass_prob=0.95, latency_p50_ms=300),
                Operation("join", ["a", "b"], "goal", "collect", "present", 0, {}, pass_prob=1.0),
                Operation("direct_fallback", ["spec"], "goal", "exp_op", "present", 100, {}, pass_prob=0.99, latency_p50_ms=1000, route_role="fallback"),
            ]
            job = Job(
                ["goal"], {"spec": 1}, 200, ops,
                acceptance={"goal": {"validator": "present", "config": {}}},
                planning_policy="bsao",
                latency_weight_units_per_sec=500.0,
                max_parallel=4,
            )
            store = KernelStore(Path(tmp) / "no-premature.sqlite3")
            self.addCleanup(store.close)
            state = Runtime(store, reg).submit("no-premature", job)
            self.assertEqual("complete", state["status"])
            self.assertEqual(["fast", "slow"], sorted(called))
            self.assertEqual(4, state["spent_units"])


if __name__ == "__main__":
    unittest.main()
