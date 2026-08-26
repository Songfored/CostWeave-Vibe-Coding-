"""Cross-surface project x data-residency authorization regression matrix."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from costweave.domain import RunRequest
from costweave.engine import RunManager, RunRecord
from costweave.server import build_server


PROJECT = "project-residency-matrix"
LOCAL = "local"
CN = "cn-north"


def add_run(manager: RunManager, run_id: str, residency: str) -> RunRecord:
    assert manager.control_kernel is not None
    record, _ = manager.control_kernel.create_run(
        RunRequest(
            goal=f"Verify {residency} residency isolation",
            project_id=PROJECT,
            data_residency=residency,
        ),
        idempotency_key=f"residency-matrix-create:{run_id}",
    )
    with manager.lock:
        manager.records[record.id] = record
    return record


class ResidencyHTTPMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = RunManager(
            runtime_db_path=str(Path(self.temporary.name) / "runtime.sqlite3"),
            recover=False,
        )
        self.manager._start = lambda record, *, resume: None
        self.local_run = add_run(self.manager, "run_residency_local", LOCAL)
        self.cn_run = add_run(self.manager, "run_residency_cn", CN)
        self.server = build_server("127.0.0.1", 0, manager=self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.manager.shutdown(wait_timeout=2)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        residency: str,
    ) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "X-CostWeave-Project": PROJECT,
            "X-CostWeave-Data-Residency": residency,
            "X-CostWeave-Session": self.server.session_token,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()

    def test_runtime_events_require_both_scope_dimensions(self) -> None:
        status, body = self.request(
            "GET",
            f"/api/v1/runtime/{self.cn_run.id}/events",
            residency=LOCAL,
        )
        self.assertEqual(404, status)
        self.assertEqual("run_not_found", body["error"])

        status, body = self.request(
            "GET",
            f"/api/v1/runtime/{self.cn_run.id}/events",
            residency=CN,
        )
        self.assertEqual(200, status)
        self.assertEqual(self.cn_run.id, body["run_id"])

    def test_evidence_console_and_export_filter_the_full_scope(self) -> None:
        status, body = self.request(
            "GET",
            "/api/v1/evidence/console",
            residency=CN,
        )
        self.assertEqual(200, status)
        self.assertEqual(CN, body["scope"]["data_residency"])
        recorded = next(
            item for item in body["metrics"] if item["metric_id"] == "recorded_runs"
        )
        self.assertEqual(1, recorded["value"])

        status, body = self.request(
            "GET",
            f"/api/v1/evidence/console?run_id={self.local_run.id}",
            residency=CN,
        )
        self.assertEqual(404, status)
        self.assertEqual("run_not_found", body["error"])

        status, body = self.request(
            "POST",
            "/api/v1/evidence/exports",
            {},
            residency=CN,
        )
        self.assertEqual(201, status)
        package = body["package"]
        self.assertEqual(CN, package["scope"]["data_residency"])
        self.assertEqual(
            [self.cn_run.id],
            [item["id"] for item in package["records"]["run_summaries"]],
        )

        status, body = self.request(
            "GET",
            "/api/v1/evidence/exports",
            residency=LOCAL,
        )
        self.assertEqual(200, status)
        self.assertEqual([], body["exports"])
        status, body = self.request(
            "GET",
            "/api/v1/evidence/exports",
            residency=CN,
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(body["exports"]))
        self.assertEqual(CN, body["exports"][0]["data_residency"])


class DerivedReaderMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = RunManager(
            runtime_db_path=str(Path(self.temporary.name) / "runtime.sqlite3"),
            recover=False,
        )
        self.local_run = add_run(self.manager, "run_derived_local", LOCAL)
        self.cn_run = add_run(self.manager, "run_derived_cn", CN)

    def tearDown(self) -> None:
        self.manager.shutdown(wait_timeout=2)
        self.temporary.cleanup()

    def test_ledger_and_approval_views_use_the_same_scope_matrix(self) -> None:
        self.manager.control_kernel.ledger = lambda *, run_id=None: [
            {"run_id": self.local_run.id, "amount": 1.0},
            {"run_id": self.cn_run.id, "amount": 2.0},
        ]
        self.manager.control_kernel.approvals = lambda *, status=None: [
            {"approval_id": "approval_local", "run_id": self.local_run.id},
            {"approval_id": "approval_cn", "run_id": self.cn_run.id},
        ]

        self.assertEqual(
            [self.cn_run.id],
            [
                item["run_id"]
                for item in self.manager.ledger(
                    project_id=PROJECT,
                    data_residency=CN,
                )
            ],
        )
        self.assertEqual(
            ["approval_cn"],
            [
                item["approval_id"]
                for item in self.manager.approvals(
                    project_id=PROJECT,
                    data_residency=CN,
                )
            ],
        )

    def test_approval_mutation_checks_residency_before_delegating(self) -> None:
        calls: list[str] = []
        self.manager.control_kernel.approval_store.get = lambda approval_id: {
            "approval_id": approval_id,
            "run_id": self.cn_run.id,
        }

        def resolve(record, approval_id, **kwargs):
            calls.append(str(record.id))
            return {"approval_id": approval_id, "status": "approved"}

        self.manager.control_kernel.resolve_approval = resolve
        values = {
            "decision": "approved",
            "note": "matrix control",
            "expected_revision": 1,
            "idempotency_key": "residency-matrix-approval-001",
            "project_id": PROJECT,
        }
        with self.assertRaises(KeyError):
            self.manager.resolve_approval(
                "approval_cn",
                data_residency=LOCAL,
                **values,
            )
        self.assertEqual([], calls)

        result = self.manager.resolve_approval(
            "approval_cn",
            data_residency=CN,
            **values,
        )
        self.assertEqual("approved", result["status"])
        self.assertEqual([self.cn_run.id], calls)


if __name__ == "__main__":
    unittest.main()
