from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from costweave.evidence_console import (
    EVIDENCE_CONSOLE_SCHEMA_VERSION,
    EVIDENCE_EXPORT_SCHEMA_VERSION,
    EvidenceConsoleError,
    build_evidence_console,
    build_evidence_export,
    public_evidence_console_policy,
    validate_evidence_export,
)
from costweave.evidence_console_store import (
    EvidenceConsoleStore,
    EvidenceConsoleStoreError,
)
from costweave.event_store import EventStore


NOW = "2026-08-05T00:00:00+00:00"


class Counts:
    def __init__(self, values):
        self.values = values

    def counts(self):
        return dict(self.values)


class FakeRecord:
    def snapshot(self):
        return {
            "id": "run-evidence-1",
            "status": "completed",
            "created_at": "2026-08-05T00:00:00+00:00",
            "started_at": "2026-08-05T00:00:01+00:00",
            "finished_at": "2026-08-05T00:00:02+00:00",
            "request": {"goal": "build a coding and research system"},
            "plan": {
                "analysis": {"task_types": ["coding", "research"]},
                "routing_summary": {
                    "strategy": "balanced",
                    "estimated_total_cost_usd": 0.42,
                    "critical_path_p95_ms": 1234.0,
                },
            },
            "planning": {
                "decision": {
                    "mode": "multi_planner",
                    "panel_policy": {"size": 3},
                },
                "proposals": [],
                "deliberation": None,
            },
            "metrics": {
                "success_probability": 0.84,
                "spent": 0.31,
                "validated_tasks": 2,
                "total_tasks": 2,
                "duration_ms": 1000,
            },
            "runtime": {
                "policy_snapshot": {
                    "selected_policy_id": "stable-policy",
                    "selected_policy_version": "1.0.0",
                }
            },
        }


class FakeManager:
    def __init__(self, *, include_run=True, event_count=2):
        self.record = FakeRecord() if include_run else None
        self.event_count = event_count
        self.last_event_limit = None
        self.control_kernel = SimpleNamespace(
            store=Counts(
                {
                    "commands": 2,
                    "events": event_count,
                    "runs": 1 if include_run else 0,
                    "tasks": 2,
                    "attempts": 2,
                    "projections": 1 if include_run else 0,
                }
            ),
            canary_store=Counts(
                {"canary_assignments": 1, "canary_runtime_rollbacks": 0}
            ),
            policy_registry_store=Counts(
                {
                    "policy_registry_policies": 2,
                    "policy_registry_gate_reports": 1,
                }
            ),
            production_shadow_store=Counts(
                {"production_shadow_comparisons": 1}
            ),
        )

    def health(self):
        return {
            "runtime_schema": "v5.8.1",
            "external_call_attempts": 0,
            "outcomes": 1,
            "outcome_label_events": 0,
        }

    def get(self, run_id):
        return self.record if run_id == "run-evidence-1" else None

    def list(self):
        return [self.record.snapshot()] if self.record else []

    def events(self, run_id, *, after_revision=0, limit=200):
        self.last_event_limit = limit
        return [
            {
                "revision": index + 1,
                "occurred_at": f"2026-08-05T00:00:{index + 1:02d}+00:00",
                "kind": "runtime.test",
            }
            for index in range(min(self.event_count, limit))
        ]

    def ledger(self, *, run_id=None):
        return [
            {"phase": "forecast", "amount": 0.42},
            {"phase": "actual", "amount": 0.31},
        ]

    def resources(self):
        return [
            {"pool_id": "local-cpu", "slots": 2},
            {"pool_id": "cloud", "slots": 4},
        ]

    def placements(self, run_id):
        return [
            {"placement_id": "p1", "status": "selected"},
            {"placement_id": "p2", "status": "backup"},
        ]

    def planning(self, run_id):
        return self.record.snapshot()["planning"] if self.record else None

    def validations(self, run_id):
        return [{"validation_id": "v1", "passed": True}]

    def failures(self, run_id):
        return []

    def artifacts(self, run_id):
        return [{"artifact_id": "a1", "content": None}]

    def forecast(self, run_id):
        return {"run_id": run_id, "probabilities": {"success": 0.84}}


class EvidenceConsoleContractTests(unittest.TestCase):
    def test_public_policy_requires_provenance_and_real_claim_evidence(self):
        policy = public_evidence_console_policy()
        self.assertEqual(EVIDENCE_CONSOLE_SCHEMA_VERSION, policy["schema_version"])
        self.assertEqual(
            ["actual", "predicted", "simulated", "missing"],
            policy["evidence_kinds"],
        )
        self.assertTrue(policy["source_trace_required"])
        self.assertTrue(policy["confidence_interval_required_for_business_claims"])
        self.assertFalse(policy["fixture_values_count_as_real"])
        self.assertFalse(policy["commercial_claims_allowed_without_e2"])

    def test_workspace_snapshot_never_promotes_fixture_values_to_real(self):
        console = build_evidence_console(FakeManager(), generated_at=NOW)
        self.assertEqual(EVIDENCE_CONSOLE_SCHEMA_VERSION, console["schema_version"])
        self.assertEqual("workspace", console["scope"]["kind"])
        self.assertEqual("E0", console["summary"]["evidence_level"])
        self.assertFalse(console["summary"]["commercial_claims_allowed"])
        self.assertEqual(0, console["summary"]["real_effect_sample_size"])
        fixture = next(
            item for item in console["metrics"]
            if item["metric_id"] == "fixture_evaluation_cases"
        )
        self.assertEqual("simulated", fixture["evidence_kind"])
        self.assertEqual("E0", fixture["evidence_level"])

    def test_every_metric_traces_to_a_versioned_source(self):
        console = build_evidence_console(FakeManager(), generated_at=NOW)
        self.assertGreaterEqual(len(console["sources"]), 10)
        sources = {item["source_id"]: item for item in console["sources"]}
        for metric in console["metrics"]:
            self.assertIn(metric["source_id"], sources)
            source = sources[metric["source_id"]]
            self.assertTrue(source["data_version"])
            self.assertTrue(source["trace"]["api_path"].startswith("/api/"))
            self.assertEqual(64, len(source["source_hash"]))

    def test_run_view_separates_actual_predictions_simulations_and_missing(self):
        console = build_evidence_console(
            FakeManager(), run_id="run-evidence-1", generated_at=NOW
        )
        metrics = {item["metric_id"]: item for item in console["metrics"]}
        self.assertEqual("actual", metrics["planning_mode"]["evidence_kind"])
        self.assertEqual("actual", metrics["routing_strategy"]["evidence_kind"])
        self.assertEqual("predicted", metrics["estimated_total_cost_usd"]["evidence_kind"])
        self.assertEqual("predicted", metrics["success_probability"]["evidence_kind"])
        self.assertEqual("simulated", metrics["simulated_spend"]["evidence_kind"])
        self.assertEqual("missing", metrics["verified_success_rate"]["evidence_kind"])
        self.assertEqual(["coding", "research"], metrics["planning_mode"]["applicable_task_families"])

    def test_business_claims_show_sample_ci_and_applicable_families(self):
        claims = build_evidence_console(FakeManager(), generated_at=NOW)[
            "business_claims"
        ]
        self.assertEqual(3, len(claims))
        for claim in claims:
            self.assertEqual("not_supported", claim["status"])
            self.assertEqual(0, claim["sample_size"])
            self.assertIsNone(claim["confidence_interval"])
            self.assertEqual([], claim["applicable_task_families"])
            self.assertTrue(claim["reason"])

    def test_export_is_bounded_redacted_and_hash_verifiable(self):
        manager = FakeManager(event_count=1_500)
        package = build_evidence_export(
            manager,
            run_id="run-evidence-1",
            catalog_snapshot={
                "api_key": "must-not-leak",
                "metadata": {"max_output_tokens": 4_096},
            },
            generated_at=NOW,
        )
        validate_evidence_export(package)
        self.assertEqual(EVIDENCE_EXPORT_SCHEMA_VERSION, package["schema_version"])
        self.assertEqual(1_000, manager.last_event_limit)
        self.assertEqual(1_000, len(package["records"]["events"]))
        self.assertTrue(package["manifest"]["events_truncated"])
        self.assertEqual(
            "[REDACTED]",
            package["records"]["catalog_snapshot"]["api_key"],
        )
        self.assertEqual(
            4_096,
            package["records"]["catalog_snapshot"]["metadata"]["max_output_tokens"],
        )
        self.assertFalse(package["manifest"]["raw_artifact_content_included"])

    def test_export_tampering_is_rejected(self):
        package = build_evidence_export(FakeManager(), generated_at=NOW)
        package["console"]["summary"]["commercial_claims_allowed"] = True
        with self.assertRaises(EvidenceConsoleError):
            validate_evidence_export(package)

    def test_missing_run_is_not_silently_replaced_by_workspace(self):
        with self.assertRaises(KeyError):
            build_evidence_console(
                FakeManager(include_run=False),
                run_id="missing-run",
                generated_at=NOW,
            )


class EvidenceConsoleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.health = EventStore(self.path).health()
        self.store = EvidenceConsoleStore(self.path)
        self.package = build_evidence_export(FakeManager(), generated_at=NOW)

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_26_and_empty_receipt_store(self):
        self.assertEqual(27, self.health["migrations"])
        self.assertEqual("v5.8.2", self.health["runtime_schema"])
        self.assertEqual(0, self.store.counts()["evidence_export_receipts"])

    def test_export_receipt_is_idempotent_and_integrity_checked(self):
        first, replayed = self.store.record_export(self.package)
        second, replayed_again = self.store.record_export(self.package)
        self.assertFalse(replayed)
        self.assertTrue(replayed_again)
        self.assertEqual(first, second)
        self.assertEqual([first], self.store.list_exports())
        self.assertFalse(first["commercial_claims_allowed"])

    def test_store_rejects_forged_package_hash(self):
        forged = copy.deepcopy(self.package)
        forged["integrity"]["package_hash"] = "0" * 64
        with self.assertRaises(EvidenceConsoleError):
            self.store.record_export(forged)

    def test_receipts_are_database_immutable(self):
        receipt, _ = self.store.record_export(self.package)
        with closing(sqlite3.connect(self.path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE evidence_export_receipts SET record_count=0 "
                    "WHERE export_id=?",
                    (receipt["export_id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM evidence_export_receipts WHERE export_id=?",
                    (receipt["export_id"],),
                )

    def test_invalid_list_limit_is_rejected(self):
        with self.assertRaises(EvidenceConsoleStoreError):
            self.store.list_exports(limit=0)


if __name__ == "__main__":
    unittest.main()
