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
        self.assertEqual("0.5.8.3", health["version"])
        self.assertEqual("v4.1", health["contract_schema"])
        self.assertTrue(health["features"]["contracts_v4"])
        self.assertTrue(health["contract_trace_persistent"])
        self.assertTrue(health["features"]["runtime_v4"])
        self.assertEqual("v5.8.3", health["control_kernel"]["runtime_schema"])
        self.assertTrue(health["features"]["parallel_runtime"])
        self.assertTrue(health["features"]["artifact_dag"])
        self.assertTrue(health["features"]["evidence_recovery"])
        self.assertTrue(health["features"]["outcome_forecast"])
        self.assertTrue(health["features"]["handoff_intelligence"])
        self.assertEqual(
            "v0.5.5",
            health["handoff_contract"]["schema_version"],
        )
        self.assertEqual(
            "v0.5.6.1",
            health["evaluation_protocol"]["schema_version"],
        )
        self.assertEqual(
            5, health["evaluation_protocol"]["baseline_count"]
        )
        self.assertFalse(
            health["evaluation_protocol"][
                "real_provider_calls_allowed"
            ]
        )
        self.assertEqual(
            "v0.5.6.2",
            health["outcome_dataset_governance"]["schema_version"],
        )
        self.assertFalse(
            health["outcome_dataset_governance"]["training_enabled"]
        )
        self.assertEqual(
            "v0.5.6.3",
            health["experimental_adapters"]["schema_version"],
        )
        self.assertFalse(
            health["experimental_adapters"][
                "experimental_calls_enabled"
            ]
        )
        self.assertTrue(health["shadow_router"]["advisory_only"])
        self.assertEqual(0, health["shadow_router"]["intervention_count"])
        self.assertEqual(
            "frozen", health["predictor_admission"]["current_state"]
        )
        self.assertEqual(
            "frozen_no_real_evidence",
            health["predictor_evaluation"]["current_state"],
        )
        self.assertEqual(
            "v0.5.6.4.3", health["portfolio_audit"]["schema_version"]
        )
        self.assertFalse(health["portfolio_audit"]["may_change_actual_route"])
        self.assertEqual(
            "v0.5.6.4.4", health["portfolio_validation"]["schema_version"]
        )
        self.assertFalse(
            health["portfolio_validation"]["may_change_actual_route"]
        )
        self.assertEqual(
            "v0.5.6.5", health["policy_candidates"]["schema_version"]
        )
        self.assertTrue(
            health["policy_candidates"]["common_support_required"]
        )
        self.assertFalse(
            health["policy_candidates"]["automatic_promotion_enabled"]
        )
        self.assertEqual(
            "v0.5.7.1", health["offline_replay"]["schema_version"]
        )
        self.assertFalse(health["offline_replay"]["routing_authority"])
        self.assertEqual(
            "v0.5.7.2", health["production_shadow"]["schema_version"]
        )
        self.assertFalse(health["production_shadow"]["routing_authority"])
        self.assertEqual(
            "v0.5.7.3", health["policy_registry"]["schema_version"]
        )
        self.assertFalse(
            health["policy_registry"]["registry_active_is_runtime_active"]
        )
        self.assertEqual(
            "v0.5.7.4", health["bounded_canary"]["schema_version"]
        )
        self.assertFalse(health["bounded_canary"]["runtime_enabled"])
        self.assertEqual(
            0.0, health["bounded_canary"]["high_risk_exploration_rate"]
        )
        self.assertEqual(
            "v0.5.8.3",
            health["unified_evidence_console"]["schema_version"],
        )
        self.assertTrue(
            health["unified_evidence_console"]["source_trace_required"]
        )
        self.assertFalse(
            health["unified_evidence_console"][
                "commercial_claims_allowed_without_e2"
            ]
        )
        self.assertTrue(health["control_kernel"]["artifact_dag"])
        self.assertEqual("wal", health["control_kernel"]["journal_mode"])
        _, catalog = self.fetch_json("/api/catalog")
        self.assertGreaterEqual(len(catalog["models"]), 12)
        self.assertIn("snapshot_date", catalog["metadata"])
        self.assertTrue(catalog["metadata"]["editable"])

    def test_unified_evidence_console_and_export_receipt(self):
        status, console = self.fetch_json("/api/v1/evidence/console")
        self.assertEqual(200, status)
        self.assertEqual("v0.5.8.3", console["schema_version"])
        self.assertEqual(
            {"actual", "predicted", "simulated", "missing"},
            set(console["coverage"]),
        )
        self.assertFalse(console["summary"]["commercial_claims_allowed"])
        self.assertEqual(0, console["summary"]["real_effect_sample_size"])
        source_ids = {item["source_id"] for item in console["sources"]}
        self.assertTrue(source_ids)
        self.assertTrue(
            all(item["source_id"] in source_ids for item in console["metrics"])
        )

        status, exported = self.request_json(
            "/api/v1/evidence/exports", {"run_id": None}, method="POST"
        )
        self.assertEqual(201, status)
        self.assertEqual("v0.5.8.1-export", exported["package"]["schema_version"])
        self.assertEqual(
            exported["receipt"]["package_hash"],
            exported["package"]["integrity"]["package_hash"],
        )
        self.assertFalse(
            exported["receipt"]["commercial_claims_allowed"]
        )
        status, receipts = self.fetch_json("/api/v1/evidence/exports?limit=10")
        self.assertEqual(200, status)
        self.assertIn(
            exported["receipt"]["export_id"],
            {item["export_id"] for item in receipts["exports"]},
        )

    def test_evaluation_protocol_is_explicitly_simulated(self):
        status, protocol = self.fetch_json(
            "/api/v1/evaluation/protocol"
        )
        self.assertEqual(200, status)
        self.assertEqual("v0.5.6.1", protocol["schema_version"])
        self.assertEqual(35, protocol["task_count"])
        self.assertEqual(5, len(protocol["baselines"]))
        self.assertFalse(protocol["real_provider_calls_allowed"])
        self.assertEqual("E0-simulated", protocol["evidence_level"])
        self.assertFalse(protocol["commercial_claims_allowed"])

    def test_outcome_dataset_governance_is_frozen_by_default(self):
        status, governance = self.fetch_json(
            "/api/v1/outcomes/governance"
        )
        self.assertEqual(200, status)
        self.assertEqual("v0.5.6.2", governance["schema_version"])
        self.assertTrue(governance["project_partition_isolation"])
        self.assertTrue(governance["append_only_label_evidence"])
        self.assertTrue(governance["conflicting_labels_preserved"])
        self.assertEqual(
            "hard-fail-when-frozen", governance["training_gate"]
        )
        self.assertFalse(governance["training_enabled"])
        self.assertFalse(governance["policy_promotion_enabled"])
        self.assertFalse(governance["real_provider_calls_allowed"])

    def test_experimental_adapter_policy_is_default_off_and_shadow_only(self):
        status, policy = self.fetch_json("/api/v1/experiments/policy")
        self.assertEqual(200, status)
        self.assertEqual("v0.5.6.3", policy["schema_version"])
        self.assertFalse(policy["experimental_calls_enabled"])
        self.assertFalse(policy["paid_calls_enabled"])
        self.assertEqual(0, policy["default_external_calls"])
        self.assertTrue(policy["hard_budget_required"])
        self.assertFalse(policy["secrets_persisted"])
        self.assertTrue(policy["shadow"]["advisory_only"])
        self.assertFalse(policy["shadow"]["may_change_actual_route"])
        self.assertFalse(
            policy["evidence_gate"]["fixture_results_count_as_real"]
        )

    def test_predictor_policy_is_frozen_and_non_authoritative(self):
        status, policy = self.fetch_json("/api/v1/predictors/policy")
        self.assertEqual(200, status)
        self.assertEqual("v0.5.6.5", policy["schema_version"])
        self.assertEqual("frozen_no_real_evidence", policy["current_state"])
        self.assertFalse(
            policy["admission"]["training_enabled_by_default"]
        )
        self.assertFalse(policy["policy_promotion_enabled"])
        self.assertFalse(
            policy["offline_evaluation"][
                "offline_fixture_metrics_are_claimable"
            ]
        )
        self.assertTrue(
            policy["safe_policy_candidates"]["common_support_required"]
        )
        self.assertEqual(
            0.0,
            policy["safe_policy_candidates"][
                "high_risk_exploration_rate"
            ],
        )
        self.assertFalse(
            policy["safe_policy_candidates"]["fixture_metrics_are_claimable"]
        )
        self.assertEqual(
            "legacy-resource-optimizer", policy["routing_authority"]
        )
        self.assertEqual(
            0,
            policy["runtime_counts"]["authoritative_learned_predictions"],
        )
        self.assertEqual(
            0, policy["runtime_counts"]["policy_production_promotions"]
        )
        self.assertEqual(
            0,
            policy["runtime_counts"]["policy_candidate_route_interventions"],
        )

    def test_offline_replay_policy_marks_unknowns_and_zero_authority(self):
        status, policy = self.fetch_json("/api/v1/replay/policy")
        self.assertEqual(200, status)
        self.assertEqual("v0.5.7.1", policy["schema_version"])
        self.assertTrue(policy["historical_version_snapshot_required"])
        self.assertTrue(policy["multiple_baselines_required"])
        self.assertEqual(
            "unknown",
            policy["approximate_oracle"]["unobserved_counterfactual_state"],
        )
        self.assertFalse(policy["routing_authority"])
        self.assertFalse(policy["policy_promotion_allowed"])
        self.assertEqual(
            0, policy["runtime_counts"]["offline_replay_external_calls"]
        )
        self.assertEqual(
            0, policy["runtime_counts"]["offline_replay_route_interventions"]
        )

    def test_production_shadow_policy_is_isolated_and_auditable(self):
        status, policy = self.fetch_json("/api/v1/shadow/policy")
        self.assertEqual(200, status)
        self.assertEqual("v0.5.7.2", policy["schema_version"])
        self.assertTrue(policy["same_task_features_required"])
        self.assertTrue(policy["same_catalog_snapshot_required"])
        self.assertTrue(policy["same_capacity_snapshot_required"])
        self.assertTrue(
            policy["failure_isolation"]["baseline_execution_continues"]
        )
        self.assertEqual(
            0.10, policy["overhead"]["maximum_end_to_end_cost_ratio"]
        )
        self.assertEqual("unknown", policy["counterfactual_outcome_default"])
        self.assertFalse(policy["routing_authority"])
        self.assertFalse(policy["policy_promotion_allowed"])
        self.assertEqual(
            0,
            policy["runtime_counts"]["production_shadow_execution_side_effects"],
        )
        self.assertEqual(
            0,
            policy["runtime_counts"]["production_shadow_route_interventions"],
        )

    def test_policy_registry_exposes_hard_gates_without_runtime_authority(self):
        status, policy = self.fetch_json("/api/v1/policies/registry")
        self.assertEqual(200, status)
        self.assertEqual("v0.5.7.3", policy["schema_version"])
        self.assertEqual(
            ["draft", "evaluated", "shadow", "canary", "active", "frozen", "retired"],
            policy["states"],
        )
        self.assertEqual(1_000, policy["minimum_cases"]["activation"])
        self.assertEqual(
            15.0,
            policy["promotion_gates"][
                "cps_improvement_ci95_lower_pct_strictly_greater_than"
            ],
        )
        self.assertFalse(policy["registry_active_is_runtime_active"])
        self.assertFalse(policy["routing_authority"])
        self.assertIsNone(policy["current_registry_pointer"])
        self.assertEqual(
            0,
            policy["runtime_counts"]["policy_registry_runtime_activations"],
        )

    def test_bounded_canary_is_default_off_and_exposes_zero_runtime_activity(self):
        status, policy = self.fetch_json("/api/v1/canary/policy")
        self.assertEqual(200, status)
        self.assertEqual("v0.5.7.4", policy["schema_version"])
        self.assertFalse(policy["runtime_enabled"])
        self.assertEqual([5, 25, 50, 100], policy["stages_pct"])
        self.assertEqual(0.0, policy["high_risk_exploration_rate"])
        self.assertTrue(policy["in_flight_policy_snapshot_immutable"])
        self.assertTrue(policy["new_runs_observe_rollback_immediately"])
        self.assertIsNone(policy["current_runtime_state"])
        self.assertEqual(0, policy["runtime_counts"]["canary_runtime_rollbacks"])
        self.assertEqual(0, policy["runtime_counts"]["canary_provider_calls"])

    def test_portfolio_policy_is_advisory_and_has_zero_interventions(self):
        status, policy = self.fetch_json("/api/v1/portfolio/policy")
        self.assertEqual(200, status)
        self.assertEqual("v0.5.6.4.3", policy["schema_version"])
        self.assertEqual(
            "v0.5.6.4.4", policy["exact_validation"]["schema_version"]
        )
        self.assertTrue(
            policy["exact_validation"]["exact_oracle"][
                "proves_optimality_within_bounds"
            ]
        )
        self.assertEqual(
            "bounded-deterministic-beam-search", policy["solver"]
        )
        self.assertTrue(policy["advisory_only"])
        self.assertFalse(policy["may_change_actual_route"])
        self.assertFalse(policy["external_calls_required"])
        self.assertEqual(
            0, policy["runtime_counts"]["portfolio_route_interventions"]
        )
        self.assertEqual(
            0,
            policy["runtime_counts"][
                "portfolio_validation_route_interventions"
            ],
        )

    def test_planning_policy_and_run_receipt_are_explainable(self):
        status, policy = self.fetch_json("/api/v1/planning/policy")
        self.assertEqual(200, status)
        self.assertEqual("v0.5.3", policy["schema_version"])
        self.assertEqual(
            "bounded-identity-blind-multiplanner",
            policy["scope"],
        )
        self.assertEqual(
            "local-heuristic-difficulty",
            policy["classifier"]["default"],
        )
        self.assertFalse(policy["adapter_policy"]["paid_calls_enabled"])
        self.assertEqual(2, policy["limits"]["max_management_depth"])
        self.assertIn("MULTI_PLANNER", policy["modes"])
        self.assertEqual(3, len(policy["roles"]))
        self.assertTrue(policy["deliberation"]["parallel_proposals"])
        self.assertTrue(policy["deliberation"]["identity_blind_scoring"])
        self.assertFalse(policy["deliberation"]["majority_vote"])

        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": "server-planning-receipt-0001",
        }
        status, run = self.request_json(
            "/api/v1/runs",
            {
                "goal": "设计并验证一个跨领域高可靠成本调度系统",
                "budget": 2,
            },
            headers=headers,
            method="POST",
        )
        self.assertEqual(202, status)
        planning = None
        for _ in range(50):
            status, planning = self.fetch_json(
                f"/api/v1/runs/{run['id']}/planning"
            )
            if planning["decision"] is not None:
                break
            time.sleep(0.03)
        self.assertEqual(200, status)
        self.assertIsNotNone(planning["decision"])
        self.assertTrue(planning["decision"]["fingerprint"])
        self.assertFalse(planning["decision"]["simulated"])
        self.assertIn("difficulty_vector", planning["decision"])
        self.assertEqual(6, len(planning["decision"]["candidates"]))
        self.assertIn("expected_loss", planning["decision"]["candidates"][0])
        self.assertIsNotNone(planning["deliberation"])
        self.assertEqual(
            "v0.5.3",
            planning["deliberation"]["schema_version"],
        )
        self.assertLessEqual(
            planning["deliberation"]["adjudication"]["rounds_used"],
            1,
        )

    def test_high_risk_governor_inserts_pre_execution_review(self):
        status, run = self.request_json(
            "/api/v1/runs",
            {
                "goal": (
                    "在生产环境执行删除并迁移所有用户认证数据库，"
                    "要求安全审计、法律合规和回滚方案。"
                ),
                "budget": 2,
            },
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "server-governor-risk-gate-0001",
            },
            method="POST",
        )
        self.assertEqual(202, status)
        current = None
        for _ in range(80):
            _, current = self.fetch_json(
                f"/api/v1/runs/{run['id']}"
            )
            if current.get("plan") is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(current["plan"])
        self.assertTrue(
            current["planning"]["decision"][
                "pre_execution_escalation"
            ]["required"]
        )
        self.assertIn(
            "planning-escalation",
            [task["id"] for task in current["plan"]["tasks"]],
        )

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
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            _, current = self.fetch_json(f"/api/v1/runs/{run['id']}")
            if current["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(.05)
        self.assertEqual("completed", current["status"], current.get("error"))

        _, economics = self.fetch_json(
            f"/api/v1/runs/{run['id']}/economics"
        )
        self.assertTrue(
            economics["cross_provider"]["pre_execution_admission"]
        )
        self.assertGreater(
            economics["cross_provider"]["assessments"], 0
        )
        self.assertIn("non_model_cost_usd", economics)
        self.assertIn("critical_path_p95_ms", economics)
        self.assertIsInstance(economics["budget_rebalances"], int)
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
        _, handoffs = self.fetch_json(
            f"/api/v1/runs/{run['id']}/handoffs"
        )
        self.assertEqual(
            len(current["plan"]["tasks"]),
            handoffs["admitted"],
        )
        self.assertTrue(
            all(
                item["contract_digest"]
                for item in handoffs["handoffs"]
            )
        )
        _, recovery = self.fetch_json(
            f"/api/v1/runs/{run['id']}/recovery"
        )
        self.assertTrue(recovery["checkpoints"])
        self.assertEqual(1, recovery["global_replans_remaining"])
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

    def test_v1_media_project_preview_gate_and_approval_api(self):
        status, run = self.request_json(
            "/api/v1/runs",
            {
                "goal": "制作30秒AI电影，主角必须保持一致，包含配音",
                "budget": 3,
                "max_concurrency": 4,
            },
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "server-media-create-0001",
            },
            method="POST",
        )
        self.assertEqual(202, status)
        for _ in range(120):
            _, current = self.fetch_json(f"/api/v1/runs/{run['id']}")
            if current.get("media_project") is not None:
                break
            time.sleep(.05)
        self.assertIsNotNone(current.get("media_project"))

        status, media = self.fetch_json(
            f"/api/v1/media/projects/{run['id']}"
        )
        self.assertEqual(200, status)
        self.assertTrue(6 <= len(media["shots"]) <= 10)
        self.assertEqual(
            0, media["render_summary"]["final_render_attempts"]
        )
        _, shot_list = self.fetch_json(
            f"/api/v1/media/projects/{run['id']}/shots"
        )
        self.assertEqual(len(media["shots"]), len(shot_list["shots"]))

        status, missing_key = self.request_json(
            f"/api/v1/media/projects/{run['id']}/approve",
            {},
            method="POST",
        )
        self.assertEqual(400, status)
        self.assertEqual(
            "idempotency_key_required", missing_key["error"]
        )

        status, approved = self.request_json(
            f"/api/v1/media/projects/{run['id']}/approve",
            {},
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "server-media-approve-0001",
            },
            method="POST",
        )
        self.assertEqual(202, status, approved)
        self.assertEqual("completed", approved["status"])
        self.assertEqual(
            len(approved["shots"]),
            approved["render_summary"]["accepted_final_shots"],
        )


if __name__ == "__main__":
    unittest.main()
