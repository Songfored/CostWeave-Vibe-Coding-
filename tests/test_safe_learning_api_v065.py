import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from costweave.catalog_store import CatalogStore
from costweave.intelligence.common import CREDIT_COMPONENTS
from costweave.intelligence.safe_learning import SAFE_LEARNING_CONFIRM_TOKEN, LoggedDecisionV1, TraceCreditInputV1
from costweave.server import build_server


def trace_payload():
    return TraceCreditInputV1(
        project_id="local-default", trace_id="api-trace-065", accepted=True, reward=0.7,
        total_cost_usd=0.01, total_latency_ms=1000,
        component_effects={component: 0.1 for component in CREDIT_COMPONENTS},
        component_confidence={component: 0.8 for component in CREDIT_COMPONENTS},
        evidence_level="E0-simulated", data_origin="test_fixture",
        independently_validated=False, evidence_ids=("api-fixture",),
        created_at="2026-01-01T00:00:00+00:00",
    ).to_dict()


def observation_payload(index):
    return LoggedDecisionV1(
        observation_id=f"api-observation-{index:02d}", project_id="local-default",
        task_family="classification", risk_level="low", logged_action="terra",
        logging_action_probabilities={"luna": 0.25, "terra": 0.75},
        candidate_action_probabilities={"luna": 0.4, "terra": 0.6},
        predicted_rewards={"luna": 0.55, "terra": 0.75},
        observed_reward=0.7, evidence_level="E0-simulated", data_origin="test_fixture",
        independently_validated=False, created_at="2026-01-01T00:00:00+00:00",
    ).to_dict()


class SafeLearningApiTests(unittest.TestCase):
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

    def test_status_is_advisory_only(self):
        status, payload = self.request("/api/v1/safe-learning/status")
        self.assertEqual(200, status)
        self.assertEqual("v0.6.5", payload["schema_version"])
        self.assertFalse(payload["training_enabled"])
        self.assertFalse(payload["route_intervention"])

    def test_credit_idempotency_and_get(self):
        payload = {"confirm": SAFE_LEARNING_CONFIRM_TOKEN, "trace": trace_payload()}
        headers = {"Content-Type": "application/json", "Idempotency-Key": "api-safe-credit-001"}
        status, result = self.request("/api/v1/safe-learning/credit", payload=payload, headers=headers, method="POST")
        self.assertEqual(201, status)
        self.assertFalse(result["credit"]["usable_for_learning"])
        replay_status, replay = self.request("/api/v1/safe-learning/credit", payload=payload, headers=headers, method="POST")
        self.assertEqual(200, replay_status)
        self.assertTrue(replay["replayed"])
        lookup_status, lookup = self.request("/api/v1/safe-learning/" + result["credit"]["credit_id"])
        self.assertEqual(200, lookup_status)
        self.assertEqual("credit", lookup["kind"])

    def test_evaluate_e0_is_rejected_and_missing_confirmation_is_400(self):
        payload = {
            "confirm": SAFE_LEARNING_CONFIRM_TOKEN, "policy_id": "p", "policy_version": "1",
            "candidate_policy_id": "p", "candidate_policy_version": "1",
            "champion_policy_id": "champion", "champion_policy_version": "1",
            "champion_estimate": 0.68, "rollback_policy_id": "rollback",
            "task_family": "classification", "method": "dr", "stage": "shadow",
            "observations": [observation_payload(index) for index in range(12)],
        }
        headers = {"Content-Type": "application/json", "Idempotency-Key": "api-safe-eval-001"}
        status, result = self.request("/api/v1/safe-learning/evaluate", payload=payload, headers=headers, method="POST")
        self.assertEqual(201, status)
        self.assertEqual("rejected", result["assessment"]["state"])
        bad = {key: value for key, value in payload.items() if key != "confirm"}
        status, error = self.request("/api/v1/safe-learning/evaluate", payload=bad, headers={"Content-Type": "application/json", "Idempotency-Key": "api-safe-eval-002"}, method="POST")
        self.assertEqual(400, status)
        self.assertEqual("safe_learning_request_invalid", error["error"])


if __name__ == "__main__":
    unittest.main()
