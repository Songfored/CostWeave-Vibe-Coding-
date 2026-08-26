import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from costweave.catalog_store import CatalogStore
from costweave.meta_controller_cli import _demo
from costweave.server import build_server
from costweave.intelligence.meta_controller import BudgetAmountV1
from costweave.meta_controller_service import CONFIRM_TOKEN


class MetaControllerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.catalog = CatalogStore(Path(cls.temp.name) / "catalog.json")
        cls.server = build_server("127.0.0.1", 0, catalog_store=cls.catalog)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    @staticmethod
    def _url(path):
        return f"http://127.0.0.1:{MetaControllerApiTests.port}{path}"

    def _request(self, path, *, payload=None, headers=None, method=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = {
            "Content-Type": "application/json",
            "X-CostWeave-Session": self.server.session_token,
        }
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(self._url(path), data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def _payload(self):
        task, system, candidates = _demo()
        return {
            "confirm": CONFIRM_TOKEN,
            "task_belief": task.to_dict(),
            "system_belief": system.to_dict(),
            "candidates": [candidate.to_dict() for candidate in candidates],
            "available_budget": BudgetAmountV1(money_usd=1, planner_calls=2, wall_clock_ms=1000, probe_calls=1).to_dict(),
            "now": "2026-08-09T00:00:00Z",
        }

    def test_status_is_advisory_and_offline(self):
        status, payload = self._request("/api/v1/meta-controller/status")
        self.assertEqual(200, status)
        self.assertEqual("v0.6.1", payload["schema_version"])
        self.assertEqual(2, payload["management_depth_limit"])
        self.assertEqual(0, payload["provider_calls"])
        self.assertEqual(0, payload["network_calls"])
        self.assertIn("meta_decisions_v061", payload["counts"])

    def test_post_requires_confirmation_and_idempotency(self):
        status, payload = self._request("/api/v1/meta-controller/decide", payload={}, headers={"Idempotency-Key": "api-key-v061"}, method="POST")
        self.assertEqual(400, status)
        self.assertEqual("meta_controller_request_invalid", payload["error"])
        status, payload = self._request("/api/v1/meta-controller/decide", payload=self._payload(), headers={"Idempotency-Key": "api-key-v061"}, method="POST")
        self.assertEqual(201, status)
        decision_id = payload["decision"]["decision_id"]
        self.assertEqual("fast", payload["decision"]["action"])
        self.assertFalse(payload["replayed"])
        status, replay = self._request("/api/v1/meta-controller/decide", payload=self._payload(), headers={"Idempotency-Key": "api-key-v061"}, method="POST")
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        self.assertEqual(decision_id, replay["decision"]["decision_id"])
        status, found = self._request(f"/api/v1/meta-controller/{decision_id}")
        self.assertEqual(200, status)
        self.assertEqual(decision_id, found["decision"]["decision_id"])

    def test_unknown_fields_and_project_scope_are_rejected_or_isolated(self):
        payload = self._payload()
        payload["unexpected"] = True
        status, result = self._request("/api/v1/meta-controller/decide", payload=payload, headers={"Idempotency-Key": "api-key-v061-2"}, method="POST")
        self.assertEqual(400, status)
        self.assertEqual("meta_controller_request_invalid", result["error"])
        status, _ = self._request("/api/v1/meta-controller/missing-decision-id")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
