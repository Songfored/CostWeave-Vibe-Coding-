from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from costweave.domain import (
    RunRequest,
    RunStatus,
    TaskContract,
)
from costweave.engine import RunManager
from costweave.failure_classifier import (
    FailureClass,
    FailureClassifier,
    FailureRecord,
)
from costweave.plan_patch import PlanPatch
from costweave.recovery import (
    RecoveryAction,
    RecoveryContext,
    RecoveryPolicy,
)
from costweave.success_contract import (
    SuccessContract,
    is_self_attestation,
)
from costweave.validator import ContractValidator


def make_task(
    *,
    task_type: str = "analysis",
    risk: str = "normal",
    criteria: list[str] | None = None,
    critical: list[str] | None = None,
) -> TaskContract:
    criteria = criteria or ["关键正确性", "表达清晰", "范围完整"]
    return TaskContract(
        id="task-a",
        title="Task A",
        objective="produce a verifiable result",
        task_type=task_type,
        acceptance_criteria=criteria,
        critical_criteria=critical or [],
        output_schema=["deliverable"],
        risk_level=risk,
        uncertainty=.2,
        criticality=.7,
    )


def valid_result(task: TaskContract) -> dict:
    evidence = ["test-suite:passed", "artifact:verified"]
    result = {
        "summary": "complete",
        "deliverable": {"status": "ready"},
        "evidence": evidence,
        "confidence": .9,
    }
    if task.task_type in {"planning", "validation", "risk", "safety"}:
        result["decision"] = "pass"
        result["criteria_results"] = [
            {
                "criterion": item,
                "passed": True,
                "evidence_refs": [evidence[0]],
            }
            for item in task.acceptance_criteria
        ]
    return result


def failure(
    category: FailureClass,
    *,
    attempt: int = 1,
) -> FailureRecord:
    return FailureRecord(
        failure_id=f"failure-{category.value}",
        task_id="task-a",
        attempt=attempt,
        classification=category.value,
        confidence=.9,
        signals=("fixture",),
        source="test",
        created_at="2026-07-28T00:00:00Z",
    )


def context(**changes) -> RecoveryContext:
    values = {
        "recovery_count": 0,
        "max_recoveries": 2,
        "task_attempt": 1,
        "max_task_attempts": 2,
        "remaining_budget": 1.0,
        "estimated_recovery_cost": .1,
        "elapsed_seconds": 1.0,
        "time_limit_seconds": 30.0,
    }
    values.update(changes)
    return RecoveryContext(**values)


class SuccessContractTests(unittest.TestCase):
    def test_first_criterion_is_critical_by_default(self):
        contract = SuccessContract.from_task(make_task())
        self.assertTrue(contract.criteria[0].critical)
        self.assertFalse(contract.criteria[1].critical)

    def test_explicit_critical_criterion_is_respected(self):
        task = make_task(critical=["范围完整"])
        contract = SuccessContract.from_task(task)
        critical = [item.text for item in contract.criteria if item.critical]
        self.assertEqual(["范围完整"], critical)

    def test_high_risk_makes_all_criteria_critical_and_independent(self):
        contract = SuccessContract.from_task(make_task(risk="high"))
        self.assertTrue(all(item.critical for item in contract.criteria))
        self.assertTrue(
            all(
                item.independent_evidence_required
                for item in contract.criteria
            )
        )

    def test_contract_round_trip_is_lossless(self):
        contract = SuccessContract.from_task(make_task())
        self.assertEqual(
            contract,
            SuccessContract.from_dict(contract.to_dict()),
        )

    def test_contract_ids_are_deterministic(self):
        first = SuccessContract.from_task(make_task())
        second = SuccessContract.from_task(make_task())
        self.assertEqual(
            [item.criterion_id for item in first.criteria],
            [item.criterion_id for item in second.criteria],
        )

    def test_self_attestation_prefixes_are_detected(self):
        self.assertTrue(is_self_attestation("self:looks good"))
        self.assertTrue(is_self_attestation("model_claim:pass"))
        self.assertFalse(is_self_attestation("test-suite:passed"))


class EvidenceValidatorTests(unittest.TestCase):
    def setUp(self):
        self.validator = ContractValidator()

    def test_critical_failure_cannot_be_averaged_away(self):
        task = make_task(task_type="validation")
        result = valid_result(task)
        result["criteria_results"][0]["passed"] = False
        result["criteria_results"][0]["evidence_refs"] = ["test:failed"]
        result["confidence"] = .99
        validation = self.validator.validate_result(task, result)
        self.assertFalse(validation["passed"])
        self.assertEqual(["关键正确性"], validation["critical_failures"])

    def test_executor_self_attestation_cannot_unlock_critical_criterion(self):
        task = make_task(task_type="validation")
        result = valid_result(task)
        result["criteria_results"][0]["evidence_refs"] = ["self:pass"]
        validation = self.validator.validate_result(task, result)
        self.assertFalse(validation["passed"])
        self.assertIn("关键正确性", validation["critical_failures"])

    def test_independent_evidence_allows_critical_criterion(self):
        task = make_task(task_type="validation")
        validation = self.validator.validate_result(
            task, valid_result(task)
        )
        self.assertTrue(validation["passed"], validation["findings"])
        self.assertTrue(
            validation["criterion_results"][0][
                "independent_evidence"
            ]
        )

    def test_gate_requires_explicit_criterion_results(self):
        task = make_task(task_type="validation")
        result = valid_result(task)
        result.pop("criteria_results")
        validation = self.validator.validate_result(task, result)
        self.assertFalse(validation["passed"])
        self.assertTrue(validation["contract_errors"])

    def test_normal_task_gets_deterministic_criterion_results(self):
        task = make_task()
        validation = self.validator.validate_result(
            task, valid_result(task)
        )
        self.assertEqual(
            len(task.acceptance_criteria),
            len(validation["criterion_results"]),
        )
        self.assertTrue(validation["passed"])

    def test_high_risk_task_uses_senior_policy_layer(self):
        task = make_task(risk="high")
        validation = self.validator.validate_result(
            task, valid_result(task)
        )
        self.assertEqual("L3-senior", validation["level"])
        self.assertIn("L3-senior-policy", validation["layers_completed"])

    def test_missing_schema_field_is_a_contract_error(self):
        task = make_task()
        result = valid_result(task)
        result.pop("deliverable")
        validation = self.validator.validate_result(task, result)
        self.assertFalse(validation["passed"])
        self.assertTrue(validation["contract_errors"])

    def test_missing_evidence_is_recorded_as_gap(self):
        task = make_task()
        result = valid_result(task)
        result["evidence"] = []
        validation = self.validator.validate_result(task, result)
        self.assertFalse(validation["passed"])
        self.assertTrue(validation["evidence_gaps"])

    def test_false_positive_risk_is_bounded(self):
        task = make_task()
        validation = self.validator.validate_result(
            task, valid_result(task)
        )
        self.assertGreaterEqual(validation["false_positive_risk"], 0)
        self.assertLessEqual(validation["false_positive_risk"], 1)

    def test_invalid_non_object_has_complete_validation_contract(self):
        validation = self.validator.validate_result(make_task(), [])
        for field in (
            "contract_revision",
            "critical_failures",
            "evidence_gaps",
            "contract_errors",
            "false_positive_risk",
            "false_negative_risk",
        ):
            self.assertIn(field, validation)


class FailureClassifierTests(unittest.TestCase):
    def setUp(self):
        self.classifier = FailureClassifier()
        self.task = make_task()
        self.task.attempt = 1

    def classify(self, **values):
        return self.classifier.classify(task=self.task, **values)

    def test_timeout_exception_is_transient(self):
        record = self.classify(
            exception=RuntimeError("temporary provider timeout")
        )
        self.assertEqual(FailureClass.TRANSIENT, record.classification)

    def test_unknown_adapter_exception_is_contract_boundary(self):
        record = self.classify(
            exception=RuntimeError("adapter output protocol broke")
        )
        self.assertEqual(FailureClass.CONTRACT, record.classification)

    def test_root_assumption_signal_is_global_class(self):
        record = self.classify(
            result={"fatal_error": "核心假设失效，需要全局重新规划"},
            validation={"findings": []},
        )
        self.assertEqual(
            FailureClass.ROOT_ASSUMPTION,
            record.classification,
        )

    def test_contract_error_is_classified(self):
        record = self.classify(
            validation={
                "contract_errors": ["缺少必要字段"],
                "findings": [],
            }
        )
        self.assertEqual(FailureClass.CONTRACT, record.classification)

    def test_evidence_gap_is_classified(self):
        record = self.classify(
            validation={
                "evidence_gaps": ["missing"],
                "critical_failures": ["criterion"],
                "findings": [],
            }
        )
        self.assertEqual(FailureClass.EVIDENCE, record.classification)

    def test_high_risk_critical_failure_is_policy_class(self):
        self.task.risk_level = "high"
        record = self.classify(
            validation={
                "critical_failures": ["safety"],
                "findings": [],
            }
        )
        self.assertEqual(FailureClass.POLICY, record.classification)

    def test_low_score_is_capability_class(self):
        record = self.classify(
            validation={"score": .2, "findings": []}
        )
        self.assertEqual(FailureClass.CAPABILITY, record.classification)

    def test_failure_identity_is_deterministic(self):
        values = {
            "validation": {"score": .2, "findings": []}
        }
        self.assertEqual(
            self.classify(**values).failure_id,
            self.classify(**values).failure_id,
        )


class RecoveryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = RecoveryPolicy()

    def test_transient_failure_retries_only_node(self):
        decision = self.policy.decide(
            failure(FailureClass.TRANSIENT), context()
        )
        self.assertEqual(RecoveryAction.RETRY_NODE, decision.action)
        self.assertEqual("node", decision.scope)

    def test_capability_failure_upgrades_node(self):
        decision = self.policy.decide(
            failure(FailureClass.CAPABILITY), context()
        )
        self.assertEqual(RecoveryAction.UPGRADE_NODE, decision.action)

    def test_contract_failure_replans_subgraph(self):
        decision = self.policy.decide(
            failure(FailureClass.CONTRACT), context()
        )
        self.assertEqual(
            RecoveryAction.REPLAN_SUBGRAPH, decision.action
        )

    def test_root_assumption_replans_global(self):
        decision = self.policy.decide(
            failure(FailureClass.ROOT_ASSUMPTION), context()
        )
        self.assertEqual(RecoveryAction.REPLAN_GLOBAL, decision.action)

    def test_recovery_count_limit_stops(self):
        decision = self.policy.decide(
            failure(FailureClass.CAPABILITY),
            context(recovery_count=2),
        )
        self.assertEqual(RecoveryAction.STOP, decision.action)
        self.assertFalse(decision.allowed)

    def test_task_attempt_limit_stops_transient_loop(self):
        decision = self.policy.decide(
            failure(FailureClass.TRANSIENT),
            context(task_attempt=2),
        )
        self.assertEqual(RecoveryAction.STOP, decision.action)

    def test_budget_limit_stops(self):
        decision = self.policy.decide(
            failure(FailureClass.CONTRACT),
            context(remaining_budget=.01),
        )
        self.assertEqual(RecoveryAction.STOP, decision.action)

    def test_time_limit_stops(self):
        decision = self.policy.decide(
            failure(FailureClass.CONTRACT),
            context(elapsed_seconds=30),
        )
        self.assertEqual(RecoveryAction.STOP, decision.action)


class PlanPatchTests(unittest.TestCase):
    def test_patch_advances_exactly_one_revision(self):
        patch = PlanPatch.propose(
            base_revision=3,
            scope="subgraph",
            failed_task_id="a",
            affected_task_ids={"a", "b"},
            rationale="contract error",
        )
        patch.validate()
        self.assertEqual(4, patch.target_revision)

    def test_patch_requires_failed_task_in_scope(self):
        patch = PlanPatch.propose(
            base_revision=3,
            scope="node",
            failed_task_id="a",
            affected_task_ids={"b"},
            rationale="invalid",
        )
        with self.assertRaises(ValueError):
            patch.validate()

    def test_patch_rejects_unknown_scope(self):
        with self.assertRaises(ValueError):
            PlanPatch.propose(
                base_revision=1,
                scope="recursive-management",
                failed_task_id="a",
                affected_task_ids={"a"},
                rationale="invalid",
            )

    def test_patch_identity_is_deterministic(self):
        kwargs = {
            "base_revision": 3,
            "scope": "global",
            "failed_task_id": "a",
            "affected_task_ids": {"a", "b"},
            "rationale": "root assumption",
        }
        self.assertEqual(
            PlanPatch.propose(**kwargs).patch_id,
            PlanPatch.propose(**kwargs).patch_id,
        )


class EvidenceRecoveryIntegrationTests(unittest.TestCase):
    def test_normal_run_persists_one_validation_per_task(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = RunManager(
                runtime_db_path=os.path.join(temp, "runtime.sqlite3"),
                recover=False,
            )
            record = manager.create(
                RunRequest(
                    "分析一个产品方案并形成证据报告",
                    budget=3,
                ),
                idempotency_key="evidence-normal-run-0001",
            )
            for thread in manager.threads.values():
                thread.join(timeout=20)
            self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
            validations = manager.validations(record.id)
            self.assertEqual(len(record.plan.tasks), len(validations))
            self.assertTrue(all(item["passed"] for item in validations))
            manager.shutdown()

    def test_root_failure_persists_global_plan_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = RunManager(
                runtime_db_path=os.path.join(temp, "runtime.sqlite3"),
                recover=False,
            )
            record = manager.create(
                RunRequest(
                    "研究并开发一个数据产品并形成报告",
                    budget=3,
                    simulate_replan=True,
                ),
                idempotency_key="evidence-global-run-0001",
            )
            for thread in manager.threads.values():
                thread.join(timeout=20)
            self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
            failures = manager.failures(record.id)
            self.assertEqual(1, len(failures))
            self.assertEqual(
                "replan_global", failures[0]["recovery_action"]
            )
            self.assertEqual(1, record.recovery_count)
            manager.shutdown()

    def test_recovery_feature_flag_can_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"COSTWEAVE_EVIDENCE_RECOVERY": "0"}
        ):
            manager = RunManager(
                runtime_db_path=os.path.join(temp, "runtime.sqlite3"),
                recover=False,
            )
            record = manager.create(
                RunRequest(
                    "研究一个会触发证据失败的产品方案",
                    budget=3,
                    simulate_replan=True,
                ),
                idempotency_key="evidence-disabled-run-0001",
            )
            for thread in manager.threads.values():
                thread.join(timeout=20)
            self.assertEqual(RunStatus.FAILED, record.status)
            self.assertIn("高级恢复已停用", record.error)
            self.assertEqual(0, record.recovery_count)
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
