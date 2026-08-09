"""HTTP boundary tests for the offline v0.6.0 tournament service."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from costweave.catalog_store import CatalogStore
from costweave.server import build_server


class TournamentAPIV060Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        catalog = CatalogStore(Path(cls.temp.name, "catalog.json"))
        cls.server = build_server(
            "127.0.0.1",
            0,
            catalog_store=catalog,
            runtime_db_path=str(Path(cls.temp.name, "runtime.sqlite3")),
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def request(self, method: str, path: str, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = dict(headers or {})
        if payload is not None:
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.base + path, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_status_is_local_only_and_seed_is_explicitly_e0(self):
        status, payload = self.request("GET", "/api/v1/tournaments/status")
        self.assertEqual(200, status)
        self.assertEqual("v0.6.0", payload["schema_version"])
        self.assertEqual("E0-simulated", payload["evidence_level"])
        self.assertEqual(0, payload["provider_calls"])
        self.assertEqual(0, payload["network_calls"])
        self.assertEqual(12, payload["case_count"])

    def test_offline_run_idempotency_and_lookup_are_project_scoped(self):
        headers = {"Idempotency-Key": "api-tournament-key-0001"}
        body = {"confirm": "RUN_OFFLINE_TOURNAMENT", "seed": 600, "bootstrap_samples": 100}
        status, first = self.request("POST", "/api/v1/tournaments/offline", body, headers)
        self.assertEqual(201, status)
        self.assertFalse(first["replayed"])
        tournament_id = first["tournament"]["tournament_id"]
        status, replay = self.request("POST", "/api/v1/tournaments/offline", body, headers)
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["report"]["report_hash"], replay["report"]["report_hash"])
        conflict_status, conflict = self.request(
            "POST", "/api/v1/tournaments/offline",
            {"confirm": "RUN_OFFLINE_TOURNAMENT", "seed": 601, "bootstrap_samples": 100}, headers,
        )
        self.assertEqual(409, conflict_status)
        self.assertEqual("tournament_conflict", conflict["error"])
        status, fetched = self.request("GET", f"/api/v1/tournaments/{tournament_id}")
        self.assertEqual(200, status)
        self.assertEqual(tournament_id, fetched["tournament_id"])
        status, hidden = self.request("GET", f"/api/v1/tournaments/{tournament_id}", headers={"X-CostWeave-Project": "other-project"})
        self.assertEqual(404, status)
        self.assertEqual("tournament_not_found", hidden["error"])

    def test_write_requires_confirmation_key_and_rejects_unknown_or_path_input(self):
        status, payload = self.request("POST", "/api/v1/tournaments/offline", {"confirm": "RUN_OFFLINE_TOURNAMENT"})
        self.assertEqual(400, status)
        self.assertEqual("idempotency_key_required", payload["error"])
        status, payload = self.request("POST", "/api/v1/tournaments/offline", {"confirm": "WRONG", "unknown": 1}, {"Idempotency-Key": "api-tournament-key-0002"})
        self.assertEqual(400, status)
        self.assertEqual("tournament_request_invalid", payload["error"])
        status, payload = self.request("POST", "/api/v1/tournaments/offline", {"confirm": "RUN_OFFLINE_TOURNAMENT", "dataset_path": "C:/secret.json"}, {"Idempotency-Key": "api-tournament-key-0003"})
        self.assertEqual(400, status)
        self.assertNotIn("secret.json", json.dumps(payload))

    def test_bootstrap_limit_and_content_type_are_enforced(self):
        headers = {"Idempotency-Key": "api-tournament-key-0004"}
        status, payload = self.request("POST", "/api/v1/tournaments/offline", {"confirm": "RUN_OFFLINE_TOURNAMENT", "bootstrap_samples": 10}, headers)
        self.assertEqual(400, status)
        status, payload = self.request("POST", "/api/v1/tournaments/offline", {"confirm": "RUN_OFFLINE_TOURNAMENT", "bootstrap_samples": 100}, {**headers, "Content-Type": "text/plain"})
        self.assertEqual(415, status)


if __name__ == "__main__":
    unittest.main()
