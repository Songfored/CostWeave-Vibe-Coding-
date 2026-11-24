from __future__ import annotations

import random
import time
import unittest

from costweave.kernel.contracts import Job, Operation
from costweave.kernel.planner import plan
from costweave.kernel.solver import BSAOPlanner


def brute_force_oracle(job: Job, available: frozenset[str], excluded: frozenset[str], budget: int, risk: float) -> float:
    """Independent un-memoized recursive decision-tree oracle."""
    goals = frozenset(job.goals)
    if goals.issubset(available):
        return 0.0
    fail_pen = float(job.failure_penalty_units) if job.failure_penalty_units is not None else float(
        max(1_000_000, (job.budget_units + job.corruption_penalty_units) * 10_000 + 100_000)
    )
    best = fail_pen
    for op in job.operations:
        if (
            op.id not in excluded
            and op.produces not in available
            and available.issuperset(op.requires)
            and op.cost_units <= budget
            and op.false_pass_risk <= risk + 1e-12
        ):
            step = (
                op.effective_expected_units
                + job.latency_weight_units_per_sec * (op.latency_p50_ms / 1000.0)
                + op.false_pass_risk * job.corruption_penalty_units
            )
            nxt_exh = excluded | {op.id}
            nxt_b = budget - op.cost_units
            l_pass = brute_force_oracle(job, available | {op.produces}, nxt_exh, nxt_b, risk - op.false_pass_risk)
            l_fail = brute_force_oracle(job, available, nxt_exh, nxt_b, risk)
            q = step + op.pass_prob * l_pass + (1.0 - op.pass_prob) * l_fail
            if q < best:
                best = q
    return best


def _acc_for(ops: list[Operation]) -> dict[str, dict]:
    return {op.produces: {"validator": "present", "config": {}} for op in ops}


class KernelBSAOPlannerTests(unittest.TestCase):
    def test_new_contract_defaults_preserve_legacy_serialization(self) -> None:
        legacy_op = {
            "id": "read", "requires": [], "produces": "goal",
            "executor": "literal", "validator": "present", "cost_units": 2, "config": {"value": 1},
        }
        legacy_job = {
            "goals": ["goal"], "inputs": {}, "budget_units": 5, "operations": [legacy_op],
            "acceptance": {"goal": {"validator": "present", "config": {}}},
        }
        explicit_op = {
            **legacy_op, "expected_units": None, "pass_prob": 1.0, "false_pass_risk": 0.0,
            "latency_p50_ms": 0, "latency_p95_ms": 0, "provider_family": "local", "max_attempts": 1,
        }
        explicit_job = {
            **legacy_job, "operations": [explicit_op], "max_risk_alpha": 1.0,
            "failure_penalty_units": None, "corruption_penalty_units": 0,
            "deadline_ms": None, "latency_weight_units_per_sec": 0.0, "max_parallel": 1,
        }
        self.assertEqual(Job.from_dict(legacy_job).to_dict(), Job.from_dict(explicit_job).to_dict())

    def test_exact_agreement_with_brute_force_oracle_on_100_random_hypergraphs(self) -> None:
        rng = random.Random(20261124)
        for trial in range(100):
            n_ops = rng.randint(2, 6)
            ops = []
            produced = {"seed"}
            for i in range(n_ops):
                target = "goal" if i == n_ops - 1 else rng.choice(["a", "b", "goal"])
                pool = sorted(r for r in produced if r != target)
                reqs = [r for r in pool if rng.random() < 0.45]
                produced.add(target)
                cost = rng.randint(1, 5)
                ops.append(Operation(
                    f"op{i}", reqs, target, "literal", "present", cost, {"value": i},
                    pass_prob=round(rng.uniform(0.3, 0.99), 2),
                    false_pass_risk=round(rng.uniform(0.0, 0.04), 3),
                ))
            acc = _acc_for(ops)
            acc["goal"] = {"validator": "present", "config": {}}
            job = Job(
                ["goal"], {"seed": 1}, rng.randint(3, 15), ops, acceptance=acc,
                planning_policy="bsao", failure_penalty_units=rng.randint(15, 60),
                corruption_penalty_units=80, max_risk_alpha=1.0,
            )
            oracle_loss = brute_force_oracle(job, frozenset({"seed"}), frozenset(), job.budget_units, job.max_risk_alpha)
            res = BSAOPlanner(job).plan(frozenset({"seed"}), frozenset(), job.budget_units)
            with self.subTest(trial=trial):
                self.assertAlmostEqual(oracle_loss, res.expected_loss, places=7)

    def test_five_canonical_economic_regimes(self) -> None:
        # 1. Tight budget fallback protection WITHOUT route_role tags (B=5)
        ops1 = [
            Operation("spec", ["doc"], "report", "literal", "present", 1, {"value": 1}, pass_prob=0.55),
            Operation("fb1", ["doc"], "parts", "literal", "present", 2, {"value": 1}, pass_prob=0.98),
            Operation("fb2", ["parts"], "report", "literal", "present", 3, {"value": 1}, pass_prob=0.99),
        ]
        job1 = Job(["report"], {"doc": 1}, 5, ops1, acceptance=_acc_for(ops1),
                   planning_policy="bsao", failure_penalty_units=30)
        p1 = plan(job1, ["doc"], [], 5)
        self.assertEqual("ready", p1.status)
        self.assertEqual("fb1", p1.operations[0])

        # 2. Ample budget skips low-win-rate c/p cheap trap (B=12)
        ops2 = [
            Operation("trap", ["doc"], "report", "literal", "present", 3, {"value": 1}, pass_prob=0.15),
            Operation("safe", ["doc"], "report", "literal", "present", 5, {"value": 1}, pass_prob=0.96),
        ]
        job2 = Job(["report"], {"doc": 1}, 12, ops2, acceptance=_acc_for(ops2),
                   planning_policy="bsao", failure_penalty_units=40)
        p2 = plan(job2, ["doc"], [], 12)
        self.assertEqual("safe", p2.operations[0])

        # 3. Rejects 3-step 0.62^3 compounding decomposition trap in favor of frontier monolith
        ops3 = [
            Operation("monolith", ["doc"], "report", "literal", "present", 10, {"value": 1}, pass_prob=0.96),
            Operation("step1", ["doc"], "s1", "literal", "present", 3, {"value": 1}, pass_prob=0.62),
            Operation("step2", ["s1"], "s2", "literal", "present", 3, {"value": 1}, pass_prob=0.62),
            Operation("step3", ["s2"], "report", "literal", "present", 3, {"value": 1}, pass_prob=0.62),
        ]
        job3 = Job(["report"], {"doc": 1}, 16, ops3, acceptance=_acc_for(ops3),
                   planning_policy="bsao", failure_penalty_units=60)
        p3 = plan(job3, ["doc"], [], 16)
        self.assertEqual(("monolith",), p3.operations)

        # 4. Blocks cheap noisy-judge route when false_pass_risk > max_risk_alpha
        ops4 = [
            Operation("noisy", ["doc"], "report", "literal", "present", 2, {"value": 1}, pass_prob=0.70, false_pass_risk=0.14),
            Operation("opus", ["doc"], "report", "literal", "present", 20, {"value": 1}, pass_prob=0.99, false_pass_risk=0.015),
        ]
        job4 = Job(["report"], {"doc": 1}, 25, ops4, acceptance=_acc_for(ops4),
                   planning_policy="bsao", max_risk_alpha=0.04, failure_penalty_units=60)
        p4 = plan(job4, ["doc"], [], 25)
        self.assertEqual(("opus",), p4.operations)
        reasons = {alt["op_id"]: alt["reason"] for alt in p4.audit["alternatives"]}
        self.assertEqual("risk_exceeded", reasons["noisy"])

        # 5. ABSTAIN when failure_penalty_units < expected cost
        ops5 = [Operation("expensive", ["doc"], "report", "literal", "present", 20, {"value": 1}, pass_prob=0.80)]
        job5 = Job(["report"], {"doc": 1}, 25, ops5, acceptance=_acc_for(ops5),
                   planning_policy="bsao", failure_penalty_units=10)
        p5 = plan(job5, ["doc"], [], 25)
        self.assertEqual("abstain", p5.status)
        self.assertEqual((), p5.operations)

    def test_20_operation_hypergraph_solves_within_50ms(self) -> None:
        ops = [Operation("monolith", ["spec"], "app", "literal", "present", 45, {"value": 1}, pass_prob=0.95)]
        mod_names = []
        for idx in range(6):
            name = f"m{idx}"
            mod_names.append(name)
            ops.append(Operation(f"{name}_c", ["spec"], name, "literal", "present", 2, {"value": idx}, pass_prob=0.70))
            ops.append(Operation(f"{name}_m", ["spec"], name, "literal", "present", 5, {"value": idx}, pass_prob=0.96))
            ops.append(Operation(f"{name}_s", ["spec"], name, "literal", "present", 9, {"value": idx}, pass_prob=0.99))
        ops.append(Operation("integrate", mod_names, "app", "literal", "present", 2, {"value": 99}, pass_prob=0.99))
        self.assertEqual(20, len(ops))

        job = Job(["app"], {"spec": 1}, 60, ops, acceptance=_acc_for(ops), planning_policy="bsao", failure_penalty_units=120)
        t0 = time.perf_counter()
        result = plan(job, ["spec"], [], 60)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertEqual("ready", result.status)
        self.assertLess(elapsed_ms, 50.0, f"solver took {elapsed_ms:.2f} ms")


if __name__ == "__main__":
    unittest.main()
