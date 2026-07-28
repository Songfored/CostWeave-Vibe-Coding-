import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from costweave.server import build_server
from costweave.catalog_store import CatalogStore


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.catalog_store = CatalogStore(f"{cls.temp.name}/catalog.json")
        cls.server = build_server(
            "127.0.0.1",
            0,
            catalog_store=cls.catalog_store,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def fetch_json(self, path, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def request_json(self, path, payload=None, headers=None, method=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers or {"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_health_and_catalog(self):
        status, health = self.fetch_json("/api/health")
        self.assertEqual(200, status)
        self.assertEqual("offline-decision-simulation", health["runtime"])
        self.assertEqual("0.4.7", health["version"])
        self.assertEqual("v4.1", health["contract_schema"])
        self.assertTrue(health["features"]["contracts_v4"])
        self.assertTrue(health["contract_trace_persistent"])
        self.assertTrue(health["features"]["runtime_v4"])
        self.assertEqual("v4.7", health["control_kernel"]["runtime_schema"])
        self.assertTrue(health["features"]["parallel_runtime"])
        self.assertTrue(health["features"]["artifact_dag"])
        self.assertTrue(health["features"]["evidence_recovery"])
        self.assertTrue(health["features"]["outcome_forecast"])
        self.assertTrue(health["control_kernel"]["artifact_dag"])
        self.assertEqual("wal", health["control_kernel"]["journal_mode"])
        _, catalog = self.fetch_json("/api/catalog")
        self.assertGreaterEqual(len(catalog["models"]), 12)
        self.assertIn("snapshot_date", catalog["metadata"])
        self.assertTrue(catalog["metadata"]["editable"])

    def test_catalog_crud_and_revision_conflict(self):
        _, catalog = self.fetch_json("/api/catalog")
        revision = catalog["metadata"]["catalog_revision"]
        model = dict(catalog["models"][0])
        model.update({
            "id": "test-imported-model",
            "model_id": "test-imported-model",
            "name": "测试导入模型",
            "custom": True,
        })
        status, created = self.request_json("/api/catalog/models", {
            "model": model,
            "expected_revision": revision,
        })
        self.assertEqual(201, status, created)
        new_revision = created["metadata"]["catalog_revision"]

        status, conflict = self.request_json(
            "/api/catalog/models/test-imported-model",
            {
                "model": {"input_price_per_mtok": .25},
                "expected_revision": revision,
            },
            method="PUT",
        )
        self.assertEqual(409, status)
        self.assertEqual("catalog_revision_conflict", conflict["error"])

        status, updated = self.request_json(
            "/api/catalog/models/test-imported-model",
            {
                "model": {"input_price_per_mtok": .25},
                "expected_revision": new_revision,
            },
            method="PUT",
        )
        self.assertEqual(200, status, updated)
        self.assertEqual(.25, updated["model"]["input_price_per_mtok"])

    def test_static_paths_cannot_escape_web_root(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as client:
            client.sendall(
                b"GET /x\\..\\..\\..\\README.md HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            response = b""
            while True:
                chunk = client.recv(8192)
                if not chunk:
                    break
                response += chunk
        status_line, _, body = response.partition(b"\r\n\r\n")
        self.assertNotIn(b" 200 ", status_line)
        self.assertNotIn(b"# CostWeave", body)

    def test_request_schema_and_cross_origin_are_rejected(self):
        status, _ = self.request_json("/api/runs", [])
        self.assertEqual(400, status)
        status, _ = self.request_json("/api/runs", {
            "goal": "这是一个足够长的测试目标",
            "simulate_replan": "false",
        })
        self.assertEqual(400, status)
        status, _ = self.request_json(
            "/api/runs",
            {"goal": "这是一个足够长的测试目标"},
            {"Content-Type": "text/plain", "Origin": "https://evil.example"},
        )
        self.assertIn(status, {403, 415})

    def test_unknown_api_is_json_404(self):
        status, body = self.request_json("/api/does-not-exist")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"])

    def test_public_bind_is_refused_without_authentication(self):
        with self.assertRaises(ValueError):
            build_server("0.0.0.0", 0)

    def test_create_and_poll_run(self):
        status, run = self.fetch_json("/api/runs", {
            "goal": "开发一个数据分析应用并形成报告",
            "budget": 2,
            "max_concurrency": 4,
        })
        self.assertEqual(202, status)
        for _ in range(50):
            _, current = self.fetch_json(f"/api/runs/{run['id']}")
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(.1)
        self.assertEqual("completed", current["status"], current.get("error"))
        self.assertIsNotNone(current["plan"])

    def test_v1_create_is_idempotent_and_events_are_contiguous(self):
        payload = {
            "goal": "分析一个可持久化的产品需求并形成报告",
            "budget": 2,
            "max_concurrency": 3,
        }
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": "server-create-key-0001",
        }
        status, first = self.request_json(
            "/api/v1/runs", payload, headers=headers, method="POST"
        )
        self.assertEqual(202, status, first)
        status, second = self.request_json(
            "/api/v1/runs", payload, headers=headers, method="POST"
        )
        self.assertEqual(202, status, second)
        self.assertEqual(first["id"], second["id"])
        for _ in range(80):
            _, current = self.fetch_json(f"/api/v1/runs/{first['id']}")
            if current["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(.05)
        _, timeline = self.fetch_json(
            f"/api/v1/runs/{first['id']}/events?after_revision=0&limit=1000"
        )
        revisions = [event["revision"] for event in timeline["events"]]
        self.assertEqual(list(range(1, len(revisions) + 1)), revisions)
        self.assertGreater(len(revisions), 3)

    def test_v1_requires_idempotency_and_rejects_stale_revision(self):
        status, missing = self.request_json(
            "/api/v1/runs",
            {"goal": "分析一个缺少幂等键的产品任务"},
            method="POST",
        )
        self.assertEqual(400, status)
        self.assertEqual("idempotency_key_required", missing["error"])

        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": "server-stale-create-0001",
        }
        _, run = self.request_json(
            "/api/v1/runs",
            {"goal": "分析一个并发版本冲突任务", "budget": 2},
            headers=headers,
            method="POST",
        )
        cancel_headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": "server-stale-cancel-0001",
        }
        status, conflict = self.request_json(
            f"/api/v1/runs/{run['id']}/cancel",
            {"expected_revision": 0},
            headers=cancel_headers,
            method="POST",
        )
        self.assertEqual(409, status, conflict)
        self.assertEqual("runtime_revision_conflict", conflict["error"])

    def test_v1_artifact_task_and_graph_apis_are_traceable(self):
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": "server-artifact-create-0001",
        }
        status, run = self.request_json(
            "/api/v1/runs",
            {
                "goal": "分析一个需要完整成果血缘的产品方案",
                "budget": 2,
                "max_concurrency": 3,
            },
            headers=headers,
            method="POST",
        )
        self.assertEqual(202, status)
        for _ in range(100):
            _, current = self.fetch_json(f"/api/v1/runs/{run['id']}")
            if current["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(.05)
        self.assertEqual("completed", current["status"], current.get("error"))

        _, artifact_list = self.fetch_json(
            f"/api/v1/runs/{run['id']}/artifacts"
        )
        self.assertEqual(
            len(current["plan"]["tasks"]),
            len(artifact_list["artifacts"]),
        )
        artifact_id = artifact_list["artifacts"][0]["artifact_id"]
        _, artifact = self.fetch_json(f"/api/v1/artifacts/{artifact_id}")
        self.assertNotIn("content", artifact)
        self.assertEqual(64, len(artifact["content_hash"]))

        _, task = self.fetch_json(
            f"/api/v1/tasks/{artifact['producer_task_id']}"
        )
        self.assertEqual(run["id"], task["run_id"])
        _, graph = self.fetch_json(f"/api/v1/runs/{run['id']}/graph")
        self.assertEqual(
            len(current["plan"]["tasks"]), len(graph["tasks"])
        )
        self.assertGreater(len(graph["artifact_links"]), 0)
        _, validations = self.fetch_json(
            f"/api/v1/runs/{run['id']}/validations"
        )
        self.assertEqual(
            len(current["plan"]["tasks"]),
            len(validations["validations"]),
        )
        self.assertEqual(len(validations["validations"]), validations["passed"])
        _, failures = self.fetch_json(
            f"/api/v1/runs/{run['id']}/failures"
        )
        self.assertEqual([], failures["failures"])
        _, forecast = self.fetch_json(
            f"/api/v1/runs/{run['id']}/forecast"
        )
        self.assertEqual(1200, forecast["sample_size"])
        self.assertAlmostEqual(
            1.0, sum(forecast["probabilities"].values())
        )
        _, evaluations = self.fetch_json("/api/v1/evaluations")
        self.assertTrue(evaluations["evaluations"][0]["passed"])

        _, resources = self.fetch_json("/api/v1/resources")
        self.assertGreater(len(resources["resources"]), 0)
        self.assertIn("availability", resources["resources"][0])

        _, placements = self.fetch_json(
            f"/api/v1/runs/{run['id']}/placements"
        )
        self.assertEqual(
            len(current["plan"]["tasks"]), placements["selected"]
        )
        self.assertGreater(placements["backups"], 0)
        _, ledger = self.fetch_json(
            f"/api/v1/resources/ledger?run_id={run['id']}"
        )
        self.assertEqual(run["id"], ledger["run_id"])
        self.assertGreater(len(ledger["entries"]), 0)
        self.assertGreater(ledger["total"], 0)

        approval, _ = self.server.manager.control_kernel.approval_store.ensure(
            run_id=run["id"],
            task_id=current["plan"]["tasks"][0]["id"],
            kind="release-review",
            reason="verify approval API",
        )
        _, pending = self.fetch_json(
            "/api/v1/approvals?status=pending"
        )
        self.assertIn(
            approval["approval_id"],
            [item["approval_id"] for item in pending["approvals"]],
        )
        status, resolved = self.request_json(
            f"/api/v1/approvals/{approval['approval_id']}/resolve",
            {
                "decision": "approved",
                "note": "API acceptance passed",
                "expected_revision": approval["revision"],
            },
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "server-approval-resolve-0001",
            },
            method="POST",
        )
        self.assertEqual(200, status, resolved)
        self.assertEqual("approved", resolved["status"])


if __name__ == "__main__":
    unittest.main()
