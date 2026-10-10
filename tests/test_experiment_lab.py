import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from costweave.adapters import HTTPResponse
from costweave.experiment_lab import (ExperimentLab, TASKS, endpoint, exact_result,
                                       protect, build_lab_server)


class Transport:
    evidence_level = "E0-transport-fixture"

    def __init__(self, *, failure=False, usage=True):
        self.calls = []
        self.failure = failure
        self.usage = usage

    def post(self, **kwargs):
        self.calls.append(json.loads(kwargs["body"]))
        if self.failure:
            raise ValueError("provider echoed PRIVATE_TEST_CREDENTIAL")
        body = {"id": "fixture-response", "model": "fixture-model",
                "choices": [{"message": {"content": json.dumps(TASKS["arithmetic"]["expected"])},
                             "finish_reason": "stop"}]}
        if self.usage:
            body["usage"] = {"prompt_tokens": 100, "completion_tokens": 20}
        return HTTPResponse(status_code=200, body=json.dumps(body).encode(), headers={}, elapsed_ms=10)


class LabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lab = ExperimentLab(self.root)

    def tearDown(self):
        deadline = time.monotonic() + 5
        while self.lab.busy and time.monotonic() < deadline:
            time.sleep(.01)
        self.temp.cleanup()

    def save(self, **kwargs):
        return self.lab.save({"label": "fixture", "model": "fixture-model", "profile": "compatible",
                              "endpoint": "https://example.com/v1", "api_key": "PRIVATE_TEST_CREDENTIAL",
                              "input_price": 1, "output_price": 2, **kwargs})["id"]

    def run_job(self, **kwargs):
        result = self.lab.start(kwargs)
        deadline = time.monotonic() + 10
        while self.lab.busy and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.lab.busy)
        return self.lab.jobs[result["id"]]

    def test_secret_encrypted_and_not_returned_or_persisted_plaintext(self):
        identity = self.save()
        self.assertNotIn("PRIVATE_TEST_CREDENTIAL", self.lab.config_file.read_text())
        self.assertNotIn("credential", json.dumps(self.lab.status()))
        recovered = ExperimentLab(self.root)
        self.assertIn(identity, recovered.connections)
        self.assertEqual(b"example-password", protect(protect(b"example-password"), decrypt=True))

    def test_changing_endpoint_requires_reentry(self):
        identity = self.save()
        with self.assertRaises(ValueError):
            self.save(id=identity, endpoint="https://other.example/v1", api_key="")
        self.save(id=identity, api_key="")

    def test_endpoint_normalization_and_ssrf_rejection(self):
        self.assertEqual("https://example.com/v1/chat/completions", endpoint("https://example.com/v1/"))
        for value in ("http://example.com", "https://127.0.0.1", "https://example.com?key=secret", "https://user:pass@example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                endpoint(value)

    def test_invalid_config_never_persists(self):
        for update in ({"input_price": "NaN"}, {"output_price": -1}, {"profile": "unsupported"}, {"model": ""}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.save(**update)
        self.assertFalse(self.lab.config_file.exists())

    def test_strict_validation(self):
        expected = TASKS["empty"]["expected"]
        self.assertTrue(exact_result(json.dumps(expected), expected)["accepted"])
        for value in ('{"ids":[],"quantity":false,"revenue":0}', '{"ids":[],"quantity":0,"quantity":0,"revenue":0}',
                      '```json\n{"ids":[],"quantity":0,"revenue":0}\n```', '{"ids":[],"quantity":NaN,"revenue":0}'):
            self.assertFalse(exact_result(value, expected)["accepted"])

    def test_offline_never_constructs_transport(self):
        with patch("costweave.adapters.http.UrllibHTTPTransport", side_effect=AssertionError("network")):
            row = self.run_job(mode="offline", task="orders")
        self.assertEqual("completed", row["status"])
        self.assertEqual(0, row["external_call_attempts"])
        self.assertTrue(row["validation"]["accepted"])
        self.assertNotIn("telemetry", row)

    def test_manual_has_no_fabricated_usage(self):
        result = self.lab.manual({"task": "empty", "output": '{"ids":[],"quantity":0,"revenue":0}'})
        row = self.lab.jobs[result["id"]]
        self.assertTrue(row["validation"]["accepted"])
        self.assertEqual("manual", row["mode"])
        self.assertNotIn("telemetry", row)

    def test_real_requires_authorization_and_known_prices(self):
        identity = self.save(input_price="", output_price="")
        for authorize in (False, True):
            with self.assertRaises(ValueError):
                self.lab.start({"mode": "real", "connection": identity, "authorize": authorize, "budget_usd": .05})
        self.assertFalse(self.lab.jobs)

    def test_transport_call_uses_shared_ledger_and_no_secret_leak(self):
        identity = self.save()
        transport = Transport()
        with patch("costweave.adapters.http.UrllibHTTPTransport", return_value=transport):
            row = self.run_job(mode="real", connection=identity, authorize=True, budget_usd=.05)
        self.assertEqual("completed", row["status"], row)
        self.assertTrue(row["validation"]["accepted"])
        self.assertEqual(1, len(transport.calls))
        self.assertNotIn("reasoning_effort", transport.calls[0])
        self.assertEqual(512, transport.calls[0]["max_tokens"])
        self.assertTrue(row["external_call_id"])
        self.assertEqual(100, row["telemetry"]["input_tokens"])
        self.assertNotIn("PRIVATE_TEST_CREDENTIAL", json.dumps(row))
        self.assertFalse(any(k.startswith("COSTWEAVE_LAB_") for k in os.environ))

    def test_openai_profile_uses_completion_token_bound(self):
        identity = self.save(profile="openai")
        transport = Transport()
        with patch("costweave.adapters.http.UrllibHTTPTransport", return_value=transport):
            row = self.run_job(mode="real", connection=identity, authorize=True, budget_usd=.05)
        self.assertEqual("completed", row["status"], row)
        self.assertEqual(512, transport.calls[0]["max_completion_tokens"])
        self.assertNotIn("max_tokens", transport.calls[0])

    def test_too_small_budget_blocks_before_transport(self):
        identity = self.save()
        transport = Transport()
        with patch("costweave.adapters.http.UrllibHTTPTransport", return_value=transport):
            row = self.run_job(mode="real", connection=identity, authorize=True, budget_usd=.000001)
        self.assertEqual("failed", row["status"])
        self.assertFalse(transport.calls)

    def test_missing_usage_and_failed_call_never_retry(self):
        identity = self.save()
        for transport in (Transport(usage=False), Transport(failure=True)):
            with patch("costweave.adapters.http.UrllibHTTPTransport", return_value=transport):
                row = self.run_job(mode="real", connection=identity, authorize=True, budget_usd=.05)
            self.assertEqual("failed", row["status"], row)
            self.assertEqual(1, len(transport.calls))
            self.assertNotIn("PRIVATE_TEST_CREDENTIAL", json.dumps(row))
            self.assertNotIn("telemetry", row)

    def test_restart_marks_interrupted_without_call(self):
        self.lab._persist({"id": "lab_interrupted", "status": "running"})
        lab = ExperimentLab(self.root)
        self.assertEqual("interrupted", lab.jobs["lab_interrupted"]["status"])

    def test_busy_rejects_duplicate(self):
        self.lab.busy = True
        try:
            with self.assertRaises(ValueError):
                self.lab.start({"mode": "offline"})
        finally:
            self.lab.busy = False


class LabHTTPTests(unittest.TestCase):
    def test_session_required_static_assets_and_paid_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            server = build_lab_server(Path(directory))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(base + "/api/v1/lab")
                self.assertEqual(401, error.exception.code)
                error.exception.close()
                for path, marker in (("/", b"auto.js"), ("/lab.js", b"use strict"), ("/lab.css", b":root")):
                    with urllib.request.urlopen(base + path) as response:
                        self.assertIn(marker, response.read())
                request = urllib.request.Request(base + "/api/v1/lab", headers={"X-CostWeave-Session": server.session_token})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual([], json.load(response)["connections"])
                request = urllib.request.Request(base + "/api/v1/lab/run", data=b'{"mode":"real"}',
                    headers={"X-CostWeave-Session": server.session_token, "Content-Type":"application/json"})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(400, error.exception.code)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
