"""Exercise the artifact runtime over the public local HTTP server."""

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from costweave.catalog_store import CatalogStore
from costweave.kernel.ports import default_registry
from costweave.server import build_server


RUNS = "/api/v2/runs"


def job(value="approved", executor="literal"):
    return {
        "goals": ["verdict"], "inputs": {}, "budget_units": 3,
        "acceptance": {"verdict": {"validator": "equals", "config": {"expected": value}}},
        "operations": [{"id": "produce", "executor": executor, "requires": [],
                        "produces": "verdict", "cost_units": 3, "validator": "present",
                        "config": {"value": value}}],
    }


class KernelHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.server = build_server("127.0.0.1", 0, catalog_store=CatalogStore(Path(cls.temp.name) / "catalog.json"))
        cls.addClassCleanup(cls.server.manager.shutdown, wait_timeout=2)
        cls.addClassCleanup(cls.server.server_close)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.thread.join, 2)
        cls.addClassCleanup(cls.server.shutdown)

    def request(self, method, path, body=None, *, key=None, project="kernel-http-a", residency="local", session=True):
        headers = {"X-CostWeave-Project": project, "X-CostWeave-Data-Residency": residency}
        if session:
            headers["X-CostWeave-Session"] = self.server.session_token
        if key is not None:
            headers["Idempotency-Key"] = key
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection = HTTPConnection(*self.server.server_address, timeout=10)
        try:
            connection.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_submit_resume_and_replay_do_not_repeat_settled_work(self):
        key = "single-run"
        path = f"{RUNS}/{key}"
        status, state = self.request("POST", RUNS, job(), key=key)
        self.assertEqual(200, status)
        self.assertEqual(("complete", {"verdict": "approved"}, 3, 0),
                         (state["status"], state["outputs"], state["spent_units"], state["held_units"]))
        status, events = self.request("GET", path + "/events")
        self.assertEqual(200, status)
        self.assertEqual(1, sum(e["type"] == "attempt_claimed" for e in events["events"]))
        self.assertEqual((200, state), self.request("POST", RUNS, job(), key=key))
        self.assertEqual((200, state), self.request("POST", path + "/resume", {}))
        self.assertEqual((200, state), self.request("GET", path))
        self.assertEqual((200, events), self.request("GET", path + "/events"))
        status, conflict = self.request("POST", RUNS, job("changed"), key=key)
        self.assertEqual((409, "run_id_conflict"), (status, conflict["error"]))
        self.assertEqual((200, state), self.request("GET", path))

    def test_local_session_and_residency_apply_to_every_member_route(self):
        key = "local-scope"
        path = f"{RUNS}/{key}"
        requests = [
            ("POST", RUNS, job()), ("GET", path, None), ("GET", path + "/events", None),
            ("POST", path + "/resume", {}), ("POST", path + "/reconcile", {}),
        ]
        for method, url, body in requests:
            with self.subTest(method=method, path=url):
                status, error = self.request(method, url, body, key=key, session=False)
                self.assertEqual((401, "session_auth_required"), (status, error["error"]))
                status, error = self.request(method, url, body, key=key, residency="cn-north")
                self.assertEqual((422, "data_residency_unsupported"), (status, error["error"]))
        self.assertEqual(404, self.request("GET", path)[0])

    def test_same_id_in_distinct_projects_has_independent_values_and_events(self):
        key = "project-scoped"
        path = f"{RUNS}/{key}"
        self.assertEqual(200, self.request("POST", RUNS, job("A"), key=key)[0])
        second = "kernel-http-b"
        for method, suffix, body in (("GET", "", None), ("GET", "/events", None), ("POST", "/resume", {})):
            with self.subTest(suffix=suffix):
                self.assertEqual(404, self.request(method, path + suffix, body, project=second)[0])
        self.assertEqual(200, self.request("POST", RUNS, job("B"), key=key, project=second)[0])
        one = self.request("GET", path)[1]
        two = self.request("GET", path, project=second)[1]
        self.assertEqual(("A", "B"), (one["outputs"]["verdict"], two["outputs"]["verdict"]))
        claims = [next(e["attempt_id"] for e in self.request("GET", path + "/events", project=p)[1]["events"]
                       if e["type"] == "attempt_claimed") for p in ("kernel-http-a", second)]
        self.assertNotEqual(*claims)
        status, _ = self.request("POST", path + "/reconcile", {
            "attempt_id": claims[0], "receipt": {"outcome": "confirmed_failed"}}, project=second)
        self.assertEqual(404, status)

    def test_unknown_call_is_reconciled_via_same_verifier_without_reexecution(self):
        calls = []
        registry = default_registry()

        def interrupted(op, inputs, *, attempt_id):
            calls.append(attempt_id)
            raise RuntimeError("fixture: lost return")

        registry.register_executor("measured", interrupted)
        with patch("costweave.kernel.runtime.default_registry", return_value=registry):
            for key, value, terminal in (("lost-valid", "approved", "complete"), ("lost-invalid", "wrong", "blocked")):
                with self.subTest(key=key):
                    path = f"{RUNS}/{key}"
                    status, pending = self.request("POST", RUNS, job(executor="measured"), key=key)
                    self.assertEqual((200, "needs_reconciliation", {}, 3),
                                     (status, pending["status"], pending["outputs"], pending["held_units"]))
                    attempt = pending["active_attempt"]
                    self.assertNotIn("value", attempt)
                    self.assertEqual((200, pending), self.request("POST", path + "/resume", {}))
                    receipt = {"attempt_id": attempt["id"], "receipt": {"outcome": "returned", "value": value,
                                                                         "actual_units": 1}}
                    status, state = self.request("POST", path + "/reconcile", receipt)
                    self.assertEqual((200, terminal, 1, 0), (status, state["status"], state["spent_units"], state["held_units"]))
                    self.assertEqual({"verdict": value} if terminal == "complete" else {}, state["outputs"])
                    self.assertEqual((200, state), self.request("POST", path + "/reconcile", receipt))
                    self.assertEqual(1, calls.count(attempt["id"]))
        self.assertEqual(2, len(calls))

    def test_invalid_new_job_does_not_change_v1_api(self):
        self.assertEqual(400, self.request("POST", RUNS, job())[0])
        broken = job()
        broken["acceptance"] = {}
        self.assertEqual(400, self.request("POST", RUNS, broken, key="bad-job")[0])
        self.assertEqual(404, self.request("GET", RUNS + "/bad-job")[0])
        status, legacy = self.request("GET", "/api/v1/joint-search/status")
        self.assertEqual((200, "v0.6.3", "advisory-only"),
                         (status, legacy["schema_version"], legacy["runtime_authority"]))
