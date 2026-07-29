from __future__ import annotations

import tempfile
import sqlite3
import unittest
from pathlib import Path

from costweave.control_kernel import ControlKernel
from costweave.domain import RunRequest
from costweave.engine import RunRecord
from costweave.projections import record_from_snapshot, record_to_projection


class V051ProjectionTests(unittest.TestCase):
    def test_planning_receipt_round_trips_in_authoritative_projection(self):
        record = RunRecord(
            id="run_1234567890abcdef1234567890abcdef",
            request=RunRequest(goal="验证规划合同能够持久恢复"),
            planning_decision={
                "decision_id": "planning-test",
                "mode": "SINGLE_PLANNER",
                "fingerprint": "abc",
            },
            planning_proposals=[{
                "proposal_id": "proposal-test",
                "planner_role": "domain-planner",
            }],
            planning_deliberation={
                "proposal_count": 1,
                "parallel": False,
                "fingerprint": "receipt-abc",
            },
        )
        snapshot = record_to_projection(record, revision=1)
        restored = record_from_snapshot(snapshot)
        self.assertEqual("v5.5", snapshot["projection_schema_version"])
        self.assertEqual(
            "SINGLE_PLANNER",
            restored.planning_decision["mode"],
        )
        self.assertEqual(1, len(restored.planning_proposals))
        self.assertEqual(
            "receipt-abc",
            restored.planning_deliberation["fingerprint"],
        )

    def test_legacy_v49_projection_hydrates_without_planning_receipt(self):
        record = RunRecord(
            id="run_abcdef1234567890abcdef1234567890",
            request=RunRequest(goal="兼容旧投影"),
        )
        snapshot = record_to_projection(record, revision=1)
        snapshot["projection_schema_version"] = "v4.9"
        snapshot["runtime"]["schema_version"] = "v4.9"
        snapshot.pop("planning")
        restored = record_from_snapshot(snapshot)
        self.assertIsNone(restored.planning_decision)
        self.assertEqual([], restored.planning_proposals)

    def test_v51_projection_hydrates_with_legacy_planning_receipt(self):
        record = RunRecord(
            id="run_51abcdef1234567890abcdef12345678",
            request=RunRequest(goal="restore a v5.1 planning receipt"),
            planning_decision={
                "decision_id": "planning-v51",
                "mode": "DIRECT_DELEGATION",
                "simulated": True,
            },
            planning_proposals=[],
        )
        snapshot = record_to_projection(record, revision=1)
        snapshot["projection_schema_version"] = "v5.1"
        snapshot["runtime"]["schema_version"] = "v5.1"
        restored = record_from_snapshot(snapshot)
        self.assertEqual(
            "DIRECT_DELEGATION",
            restored.planning_decision["mode"],
        )
        self.assertTrue(restored.planning_decision["simulated"])

    def test_migration_ten_is_applied_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            first = ControlKernel(path)
            self.assertEqual(14, first.health()["migrations"])
            connection = sqlite3.connect(path)
            try:
                indexes = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA index_list(events)"
                    ).fetchall()
                }
            finally:
                connection.close()
            self.assertIn("idx_events_run_kind_revision", indexes)
            self.assertIn("idx_events_kind_run_revision", indexes)
            second = ControlKernel(path)
            self.assertEqual(14, second.health()["migrations"])
            self.assertEqual("v5.6.1", second.health()["runtime_schema"])


if __name__ == "__main__":
    unittest.main()
