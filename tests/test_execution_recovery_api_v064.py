import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from costweave.catalog_store import CatalogStore
from costweave.execution_recovery_service import CONFIRM_TOKEN
from costweave.intelligence.execution_recovery import build_demo_snapshot
from costweave.server import build_server


class ExecutionRecoveryApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = build_server("127.0.0.1", 0, catalog_store=CatalogStore(f"{cls.temp.name}/catalog.json"))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    @classmethod
    def request(cls, path, *, payload=None, headers=None, method=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(cls.base + path, data=data, headers=headers or {"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_status_is_advisory_and_v064(self):
        status, payload = self.request("/api/v1/execution-recovery/status")
        self.assertEqual(200, status)
        self.assertEqual("v0.6.4", payload["schema_version"])
        self.assertIn("SWITCH_MODEL", payload["available_actions"])
        self.assertTrue(payload["sunk_cost_excluded"])
        self.assertFalse(payload["production_route_intervention"])

    def test_decide_and_idempotent_replay(self):
        payload = {"confirm": CONFIRM_TOKEN, "snapshot": build_demo_snapshot().to_dict()}
        headers = {"Content-Type": "application/json", "Idempotency-Key": "api-recovery-001"}
        status, result = self.request("/api/v1/execution-recovery/decide", payload=payload, headers=headers, method="POST")
        self.assertEqual(201, status)
        self.assertEqual("SWITCH_MODEL", result["decision"]["action"])
        replay_status, replay = self.request("/api/v1/execution-recovery/decide", payload=payload, headers=headers, method="POST")
        self.assertEqual(200, replay_status)
        self.assertTrue(replay["replayed"])
        self.assertEqual(result["decision"]["decision_id"], replay["decision"]["decision_id"])

    def test_missing_confirmation_and_unknown_decision(self):
        payload = {"snapshot": build_demo_snapshot().to_dict()}
        status, error = self.request(
            "/api/v1/execution-recovery/decide",
            payload=payload,
            headers={"Content-Type": "application/json", "Idempotency-Key": "api-recovery-002"},
            method="POST",
        )
        self.assertEqual(400, status)
        self.assertEqual("execution_recovery_request_invalid", error["error"])
        status, error = self.request("/api/v1/execution-recovery/not-found-v064")
        self.assertEqual(404, status)
        self.assertEqual("execution_recovery_decision_not_found", error["error"])


if __name__ == "__main__":
    unittest.main()
