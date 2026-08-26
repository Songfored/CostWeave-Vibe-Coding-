import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from costweave.capability_graph_service import CONFIRM_TOKEN
from costweave.capability_router_cli import demo_evidence, demo_payload
from costweave.catalog_store import CatalogStore
from costweave.server import build_server


class CapabilityRouterApiTests(unittest.TestCase):
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

    def _request(self, path, *, payload=None, headers=None, method=None, project="local-default"):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = {
            "Content-Type": "application/json",
            "X-CostWeave-Project": project,
            "X-CostWeave-Session": self.server.session_token,
        }
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_status_is_offline_and_advisory(self):
        status, payload = self._request("/api/v1/capability-router/status")
        self.assertEqual(200, status)
        self.assertEqual("v0.6.2", payload["schema_version"])
        self.assertTrue(payload["router_intervention_enabled"] is False)
        self.assertEqual(0, payload["provider_calls"])
        self.assertIn("capability_evidence_v062", payload["counts"])

    def test_evidence_and_decision_are_replay_safe(self):
        evidence_payload = {"confirm": CONFIRM_TOKEN, "evidence": demo_evidence().to_dict()}
        status, result = self._request("/api/v1/capability-router/evidence", payload=evidence_payload, headers={"Idempotency-Key": "api-evidence-v062"}, method="POST")
        self.assertEqual(201, status)
        self.assertFalse(result["replayed"])
        status, replay = self._request("/api/v1/capability-router/evidence", payload=evidence_payload, headers={"Idempotency-Key": "api-evidence-v062"}, method="POST")
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        status, result = self._request("/api/v1/capability-router/decide", payload=demo_payload(), headers={"Idempotency-Key": "api-route-v062"}, method="POST")
        self.assertEqual(201, status)
        decision_id = result["decision"]["decision_id"]
        self.assertIn(result["decision"]["action"], {"ROUTE", "ABSTAIN", "ESCALATE"})
        status, replay = self._request("/api/v1/capability-router/decide", payload=demo_payload(), headers={"Idempotency-Key": "api-route-v062"}, method="POST")
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        status, found = self._request(f"/api/v1/capability-router/{decision_id}")
        self.assertEqual(200, status)
        self.assertEqual(decision_id, found["decision"]["decision_id"])

    def test_confirmation_and_project_scope_are_enforced(self):
        status, payload = self._request("/api/v1/capability-router/decide", payload={}, headers={"Idempotency-Key": "api-bad-v062"}, method="POST")
        self.assertEqual(400, status)
        self.assertEqual("capability_router_request_invalid", payload["error"])
        status, _ = self._request("/api/v1/capability-router/not-found-v062")
        self.assertEqual(404, status)
        status, _ = self._request("/api/v1/capability-router/status", project="another-project")
        self.assertEqual(200, status)


if __name__ == "__main__":
    unittest.main()
