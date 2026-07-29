import tempfile
import unittest
from pathlib import Path

from costweave.capability_store import CapabilityStore
from costweave.cross_provider import (
    CROSS_PROVIDER_POLICY_REVISION,
    CrossProviderPolicy,
)
from costweave.domain import (
    RunMode,
    RunRequest,
    RunStatus,
    TaskContract,
    WorkerProfile,
)
from costweave.engine import RunManager
from costweave.event_store import EventStore
from costweave.inference_policy import InferencePolicy
from costweave.resource_inventory import ResourcePool
from costweave.resource_optimizer import ResourcePortfolioOptimizer
from costweave.router import RouteDecision


def worker(
    identifier: str,
    *,
    provider: str,
    local: bool,
    reasoning: float = 0.75,
    verified_at: str = "2026-07-01",
) -> WorkerProfile:
    return WorkerProfile(
        id=identifier,
        model_id=identifier,
        name=identifier,
        specialty="general",
        capabilities={"analysis": 0.92},
        cost_per_task=0.0,
        latency_factor=1.0,
        reliability=0.96,
        local=local,
        provider=provider,
        reasoning=reasoning,
        context_window=128_000,
        max_output_tokens=16_000,
        verified_at=verified_at,
    )


def task() -> TaskContract:
    return TaskContract(
        id="analysis",
        title="analysis",
        objective="produce evidence",
        task_type="analysis",
        required_capabilities=["analysis"],
        estimated_input_tokens=4_000,
        estimated_output_tokens=1_000,
        cpu_cores=2,
        criticality=0.7,
    )


def pool(
    provider: str,
    *,
    local: bool,
    identifier: str,
) -> ResourcePool:
    return ResourcePool(
        pool_id=identifier,
        provider=provider,
        local=local,
        slots_total=2,
        cpu_cores_total=8 if local else 0,
        gpu_count_total=1 if local else 0,
        vram_mb_total=16_384 if local else 0,
        queue_limit=8,
        token_capacity=1_000_000,
        token_refill_per_second=100_000,
    )


def route(
    profile: WorkerProfile,
    *,
    mean: float = 0.93,
    lower: float = 0.88,
    cost: float = 0.01,
    latency_ms: int = 500,
) -> RouteDecision:
    return RouteDecision(
        worker=profile,
        predicted_success=mean,
        success_lower_bound=lower,
        prediction_uncertainty=0.03,
        utility=0.8,
        rationale="fixture",
        estimated_cost=cost,
        estimated_latency_ms=latency_ms,
        capability_match=0.9,
        score_components={},
    )


class CrossProviderContractTests(unittest.TestCase):
    def test_deployment_identity_includes_profile_and_provider(self):
        item = task()
        local = worker("same", provider="local", local=True)
        cloud = worker("same", provider="cloud", local=False)
        profiles = InferencePolicy.candidates(local, item)
        local_low = CrossProviderPolicy.deployment_key(
            local, profiles[0], item
        )
        local_high = CrossProviderPolicy.deployment_key(
            local, profiles[2], item
        )
        cloud_low = CrossProviderPolicy.deployment_key(
            cloud,
            InferencePolicy.candidates(cloud, item)[0],
            item,
        )
        self.assertEqual(3, len({
            local_low.value, local_high.value, cloud_low.value
        }))

    def test_local_saturation_diverts_before_execution(self):
        item = task()
        local = worker("local-model", provider="local", local=True)
        cloud = worker("cloud-model", provider="cloud", local=False)
        result = ResourcePortfolioOptimizer().optimize(
            tasks=[item],
            route_pools={
                item.id: [
                    route(local, cost=0.0),
                    route(cloud, cost=0.02),
                ]
            },
            pools=[
                pool("local", local=True, identifier="local-pool"),
                pool("cloud", local=False, identifier="cloud-pool"),
            ],
            quality_floor=0.75,
            mode=RunMode.ECONOMY,
            budget=None,
            availability={
                "local-pool": {
                    "slots": 0,
                    "queue_depth": 2,
                    "bulkhead_active": 2,
                    "bulkhead_limit": 2,
                },
                "cloud-pool": {"slots": 2, "queue_depth": 0},
            },
        )
        self.assertEqual("cloud", result.selected[item.id].provider)
        receipt = result.summary["cross_provider"]
        self.assertGreater(receipt["diverted_or_rejected"], 0)
        self.assertTrue(any(
            "local_capacity_saturated" in rejection[
                "admission_reasons"
            ]
            for rejection in receipt["rejections"]
        ))

    def test_open_provider_circuit_is_rejected(self):
        item = task()
        open_worker = worker("open", provider="open-v", local=False)
        healthy = worker("healthy", provider="healthy-v", local=False)
        result = ResourcePortfolioOptimizer().optimize(
            tasks=[item],
            route_pools={
                item.id: [
                    route(open_worker, cost=0.001),
                    route(healthy, cost=0.02),
                ]
            },
            pools=[
                pool("open-v", local=False, identifier="open-pool"),
                pool(
                    "healthy-v",
                    local=False,
                    identifier="healthy-pool",
                ),
            ],
            quality_floor=0.75,
            mode=RunMode.ECONOMY,
            budget=None,
            availability={
                "open-pool": {
                    "slots": 2,
                    "queue_depth": 0,
                    "circuit_state": "open",
                },
                "healthy-pool": {"slots": 2, "queue_depth": 0},
            },
        )
        self.assertEqual("healthy-v", result.selected[item.id].provider)

    def test_unavailable_local_vram_diverts_to_cloud(self):
        item = task()
        item.gpu_count = 1
        item.vram_mb = 12_000
        local = worker("local-video", provider="local", local=True)
        local.modalities = ["text", "video"]
        cloud = worker("cloud-video", provider="cloud", local=False)
        cloud.modalities = ["text", "video"]
        cloud_pool = pool(
            "cloud", local=False, identifier="cloud-pool"
        )
        cloud_pool = ResourcePool(
            **{
                **cloud_pool.to_dict(),
                "gpu_count_total": 1,
                "vram_mb_total": 64_000,
            }
        )
        result = ResourcePortfolioOptimizer().optimize(
            tasks=[item],
            route_pools={
                item.id: [route(local, cost=0), route(cloud, cost=.02)]
            },
            pools=[
                pool("local", local=True, identifier="local-pool"),
                cloud_pool,
            ],
            quality_floor=.75,
            mode=RunMode.ECONOMY,
            budget=None,
            availability={
                "local-pool": {
                    "slots": 1,
                    "queue_depth": 0,
                    "vram_mb": 4_000,
                },
                "cloud-pool": {
                    "slots": 2,
                    "queue_depth": 0,
                    "vram_mb": 64_000,
                },
            },
        )
        self.assertEqual("cloud", result.selected[item.id].provider)

    def test_deadline_p95_diverts_slow_candidate(self):
        item = task()
        slow = worker("slow", provider="slow-v", local=False)
        fast = worker("fast", provider="fast-v", local=False)
        result = ResourcePortfolioOptimizer().optimize(
            tasks=[item],
            route_pools={
                item.id: [
                    route(slow, cost=.001, latency_ms=2_000),
                    route(fast, cost=.02, latency_ms=200),
                ]
            },
            pools=[
                pool("slow-v", local=False, identifier="slow-pool"),
                pool("fast-v", local=False, identifier="fast-pool"),
            ],
            quality_floor=.75,
            mode=RunMode.ECONOMY,
            budget=None,
            deadline_ms=1_000,
        )
        self.assertEqual("fast-v", result.selected[item.id].provider)

    def test_quality_strategy_selects_best_lower_bound(self):
        item = task()
        cheap = worker("cheap", provider="cheap-v", local=False)
        strong = worker("strong", provider="strong-v", local=False)
        optimizer = ResourcePortfolioOptimizer()
        result = optimizer.optimize(
            tasks=[item],
            route_pools={
                item.id: [
                    route(cheap, mean=.86, lower=.82, cost=.001),
                    route(strong, mean=.98, lower=.96, cost=.08),
                ]
            },
            pools=[
                pool("cheap-v", local=False, identifier="cheap-pool"),
                pool("strong-v", local=False, identifier="strong-pool"),
            ],
            quality_floor=.7,
            mode=RunMode.QUALITY,
            budget=None,
        )
        self.assertEqual("strong", result.selected[item.id].model_id)

    def test_quality_strategy_finds_best_feasible_budget_portfolio(self):
        item = task()
        cheap = worker("cheap", provider="cheap-v", local=False)
        strong = worker("strong", provider="strong-v", local=False)
        result = ResourcePortfolioOptimizer().optimize(
            tasks=[item],
            route_pools={
                item.id: [
                    route(cheap, mean=.86, lower=.82, cost=.001),
                    route(strong, mean=.98, lower=.96, cost=.08),
                ]
            },
            pools=[
                pool("cheap-v", local=False, identifier="cheap-pool"),
                pool("strong-v", local=False, identifier="strong-pool"),
            ],
            quality_floor=.7,
            mode=RunMode.QUALITY,
            budget=.02,
        )
        self.assertEqual("cheap", result.selected[item.id].model_id)
        self.assertGreaterEqual(
            result.summary["budget_rebalances"], 1
        )
        self.assertLessEqual(
            result.summary["estimated_total_cost_usd"], .02
        )

    def test_receipt_exposes_cost_latency_posterior_and_admission(self):
        item = task()
        profile = worker("model", provider="vendor", local=False)
        result = ResourcePortfolioOptimizer().optimize(
            tasks=[item],
            route_pools={item.id: [route(profile)]},
            pools=[
                pool("vendor", local=False, identifier="vendor-pool")
            ],
            quality_floor=.75,
            mode=RunMode.BALANCED,
            budget=None,
        )
        receipt = result.summary["cross_provider"]
        selected = receipt["selected_routes"][0]
        self.assertEqual(
            CROSS_PROVIDER_POLICY_REVISION,
            receipt["policy_revision"],
        )
        self.assertIn("expected_rework_usd", selected)
        self.assertIn("finish_p95_ms", selected)
        self.assertIn("posterior", selected)
        self.assertTrue(selected["admission"]["admitted"])


class DeploymentLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.sqlite3"
        EventStore(self.db)
        self.store = CapabilityStore(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_deployment_outcomes_feed_future_calibration(self):
        item = task()
        profile = worker("learned", provider="vendor", local=False)
        inference = InferencePolicy.candidates(profile, item)[1]
        key = CrossProviderPolicy.deployment_key(
            profile, inference, item
        )
        for _ in range(20):
            self.store.update_deployment(
                model_id=key.model_id,
                provider=key.provider,
                endpoint=key.endpoint,
                reasoning_effort=key.reasoning_effort,
                task_type=item.task_type,
                risk_level=item.risk_level,
                success=True,
            )
        policy = CrossProviderPolicy(
            lambda deployment, contract: (
                self.store.deployment_candidates(
                    model_id=deployment.model_id,
                    provider=deployment.provider,
                    endpoint=deployment.endpoint,
                    reasoning_effort=deployment.reasoning_effort,
                    task_type=contract.task_type,
                    risk_level=contract.risk_level,
                )
            )
        )
        assessment = policy.assess(
            task=item,
            worker=profile,
            pool=pool(
                "vendor", local=False, identifier="vendor-pool"
            ),
            profile=inference,
            prior_mean=.65,
            prior_lower=.6,
            prior_uncertainty=.08,
            base_latency_ms=500,
            availability=None,
            deadline_ms=None,
        )
        self.assertGreaterEqual(assessment.posterior_samples, 20)
        self.assertFalse(assessment.cold_start)
        self.assertGreater(assessment.predicted_success, .65)

    def test_placement_api_round_trips_decision_receipt(self):
        manager = RunManager(
            runtime_db_path=str(self.db), recover=False
        )
        record = manager.create(
            RunRequest(
                goal="分析一个产品方案并形成可验证报告",
                budget=3,
            ),
            idempotency_key="v054-placement",
        )
        for thread in list(manager.threads.values()):
            thread.join(timeout=15)
        self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
        persisted = manager.placements(str(record.id))[0]
        self.assertIn("deployment", persisted)
        self.assertIn("posterior", persisted)
        self.assertIn("admission", persisted)
        self.assertIn("latency_forecast", persisted)
        manager.shutdown()


if __name__ == "__main__":
    unittest.main()
