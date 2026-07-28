from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from costweave.cost_model import (
    COST_CATEGORIES,
    CostBreakdown,
    CostForecast,
    ledger_conserves,
)
from costweave.domain import (
    RunMode,
    RunRequest,
    RunStatus,
    TaskContract,
    WorkerProfile,
)
from costweave.engine import RunManager
from costweave.inference_policy import InferencePolicy
from costweave.resource_economics import ResourceEconomics
from costweave.resource_inventory import ResourcePool
from costweave.resource_optimizer import (
    PlacementBudgetError,
    PlacementError,
    ResourcePortfolioOptimizer,
)
from costweave.router import RouteDecision


def worker(
    identifier: str,
    *,
    provider: str = "local",
    local: bool = True,
    reasoning: float = 0.7,
) -> WorkerProfile:
    return WorkerProfile(
        id=identifier,
        model_id=identifier,
        name=identifier,
        specialty="general",
        capabilities={"analysis": 0.9, "validation": 0.9},
        cost_per_task=0.0,
        latency_factor=1.0,
        reliability=0.94,
        local=local,
        provider=provider,
        reasoning=reasoning,
        context_window=128_000,
        max_output_tokens=16_000,
    )


def pool(
    provider: str = "local",
    *,
    local: bool = True,
    identifier: str | None = None,
) -> ResourcePool:
    return ResourcePool(
        pool_id=identifier or f"pool-{provider}",
        provider=provider,
        local=local,
        slots_total=2,
        cpu_cores_total=8,
        gpu_count_total=1 if local else 0,
        vram_mb_total=16_384 if local else 0,
        queue_limit=16,
        token_capacity=1_000_000,
        token_refill_per_second=100_000,
    )


def task(
    identifier: str = "analysis",
    *,
    dependencies: list[str] | None = None,
    task_type: str = "analysis",
) -> TaskContract:
    return TaskContract(
        id=identifier,
        title=identifier,
        objective="produce a verifiable result",
        task_type=task_type,
        dependencies=dependencies or [],
        required_capabilities=["analysis"],
        estimated_input_tokens=4_000,
        estimated_output_tokens=1_000,
        criticality=0.6,
        cpu_cores=2,
    )


def route(
    profile: WorkerProfile,
    *,
    quality: float = 0.9,
    lower: float = 0.86,
    cost: float = 0.01,
    latency_ms: int = 1_000,
) -> RouteDecision:
    return RouteDecision(
        worker=profile,
        predicted_success=quality,
        success_lower_bound=lower,
        prediction_uncertainty=0.03,
        utility=0.8,
        rationale="fixture",
        estimated_cost=cost,
        estimated_latency_ms=latency_ms,
        capability_match=0.9,
        score_components={},
    )


class CostContractTests(unittest.TestCase):
    def test_breakdown_total_contains_all_categories(self):
        values = {
            category: (index + 1) / 1000
            for index, category in enumerate(COST_CATEGORIES)
        }
        breakdown = CostBreakdown(**values)
        self.assertAlmostEqual(sum(values.values()), breakdown.total)
        self.assertEqual(set(COST_CATEGORIES) | {"total"}, set(breakdown.to_dict()))

    def test_negative_component_is_rejected(self):
        with self.assertRaises(ValueError):
            CostBreakdown(expected_rework=-0.1)

    def test_forecast_orders_p50_p95_and_ceiling(self):
        p50 = CostBreakdown(provider_inference=0.1)
        forecast = CostForecast(p50=p50, p95_total=0.12, reservation_ceiling=0.13)
        self.assertLessEqual(forecast.p50.total, forecast.p95_total)
        self.assertLessEqual(forecast.p95_total, forecast.reservation_ceiling)

    def test_forecast_rejects_under_reserved_tail(self):
        with self.assertRaises(ValueError):
            CostForecast(
                p50=CostBreakdown(provider_inference=0.1),
                p95_total=0.09,
                reservation_ceiling=0.2,
            )

    def test_ledger_conservation_and_duplicate_detection(self):
        entries = [
            {
                "phase": "forecast",
                "amount": 0.1,
                "attribution_key": "one",
            },
            {
                "phase": "forecast",
                "amount": 0.2,
                "attribution_key": "two",
            },
        ]
        self.assertTrue(
            ledger_conserves(entries, phase="forecast", expected_total=0.3)
        )
        entries[1]["attribution_key"] = "one"
        self.assertFalse(
            ledger_conserves(entries, phase="forecast", expected_total=0.3)
        )


class InferenceAndEconomicsTests(unittest.TestCase):
    def test_policy_exposes_bounded_three_level_frontier(self):
        profiles = InferencePolicy.candidates(worker("local-a"), task())
        self.assertEqual(["low", "medium", "high"], [item.reasoning_effort for item in profiles])
        self.assertLess(profiles[0].token_multiplier, profiles[2].token_multiplier)

    def test_high_risk_task_disables_cache_reuse(self):
        item = task()
        item.risk_level = "high"
        profiles = InferencePolicy.candidates(worker("local-a"), item)
        self.assertTrue(all(item.cache_hit_probability == 0 for item in profiles))

    def test_cache_scope_requires_every_safety_dimension(self):
        with self.assertRaises(ValueError):
            InferencePolicy.cache_key_scope(
                model_revision="m1",
                prompt_revision="p1",
                toolchain_revision="",
                permission_scope="internal",
                input_artifact_hash="abc",
                freshness_epoch="2026-07",
            )

    def test_local_is_not_treated_as_free(self):
        item = task()
        local = worker("local-a")
        forecast = ResourceEconomics().forecast(
            task=item,
            worker=local,
            pool=pool(),
            profile=InferencePolicy.candidates(local, item)[1],
            direct_cost=0,
            success_lower_bound=0.85,
            execution_ms=2_000,
            queue_ms=0,
            available_slots=2,
        )
        self.assertGreater(forecast.p50.local_compute, 0)
        self.assertGreater(forecast.p50.energy, 0)

    def test_queue_and_shadow_prices_increase_cost(self):
        item = task()
        local = worker("local-a")
        profile = InferencePolicy.candidates(local, item)[1]
        economics = ResourceEconomics()
        idle = economics.forecast(
            task=item, worker=local, pool=pool(), profile=profile,
            direct_cost=0, success_lower_bound=.85, execution_ms=1_000,
            queue_ms=0, available_slots=2,
        )
        busy = economics.forecast(
            task=item, worker=local, pool=pool(), profile=profile,
            direct_cost=0, success_lower_bound=.85, execution_ms=1_000,
            queue_ms=2_000, available_slots=0,
        )
        self.assertGreater(busy.p50.queue_opportunity, idle.p50.queue_opportunity)
        self.assertGreater(busy.p50.scarce_capacity, idle.p50.scarce_capacity)

    def test_failure_probability_prices_expected_rework(self):
        item = task()
        cloud = worker("cloud-a", provider="vendor", local=False)
        profile = InferencePolicy.candidates(cloud, item)[1]
        economics = ResourceEconomics()
        strong = economics.forecast(
            task=item, worker=cloud, pool=pool("vendor", local=False),
            profile=profile, direct_cost=.01, success_lower_bound=.95,
            execution_ms=1_000, queue_ms=0, available_slots=2,
        )
        weak = economics.forecast(
            task=item, worker=cloud, pool=pool("vendor", local=False),
            profile=profile, direct_cost=.01, success_lower_bound=.6,
            execution_ms=1_000, queue_ms=0, available_slots=2,
        )
        self.assertGreater(weak.p50.expected_rework, strong.p50.expected_rework)


class ResourcePortfolioTests(unittest.TestCase):
    def test_quality_floor_is_a_hard_constraint(self):
        item = task()
        optimizer = ResourcePortfolioOptimizer()
        with self.assertRaises(PlacementError):
            optimizer.optimize(
                tasks=[item],
                route_pools={item.id: [route(worker("weak"), lower=.6)]},
                pools=[pool()],
                quality_floor=.8,
                mode=RunMode.ECONOMY,
                budget=None,
            )

    def test_budget_is_a_hard_constraint(self):
        item = task()
        with self.assertRaises(PlacementBudgetError):
            ResourcePortfolioOptimizer().optimize(
                tasks=[item],
                route_pools={item.id: [route(worker("cloud", provider="v", local=False), cost=.1)]},
                pools=[pool("v", local=False)],
                quality_floor=.8,
                mode=RunMode.ECONOMY,
                budget=.001,
            )

    def test_impossible_resource_contract_is_rejected_before_execution(self):
        item = task()
        item.resource_slots = 3
        constrained = pool()
        self.assertEqual(2, constrained.slots_total)
        with self.assertRaises(PlacementError):
            ResourcePortfolioOptimizer().optimize(
                tasks=[item],
                route_pools={item.id: [route(worker("local"))]},
                pools=[constrained],
                quality_floor=.8,
                mode=RunMode.ECONOMY,
                budget=None,
            )

    def test_economy_matches_candidate_oracle_with_zero_regret(self):
        item = task()
        optimizer = ResourcePortfolioOptimizer()
        routes = [
            route(worker("a", provider="a", local=False), cost=.01),
            route(worker("b", provider="b", local=False), cost=.03),
        ]
        pools = [pool("a", local=False), pool("b", local=False)]
        candidates = optimizer.candidates(
            task=item, routes=routes, pools=pools, quality_floor=.8
        )
        result = optimizer.optimize(
            tasks=[item], route_pools={item.id: routes}, pools=pools,
            quality_floor=.8, mode=RunMode.ECONOMY, budget=None,
        )
        oracle = min(item.expected_total_cost_usd for item in candidates)
        chosen = result.selected["analysis"].expected_total_cost_usd
        self.assertLessEqual(optimizer.regret(chosen, oracle), .05)

    def test_deadline_eliminates_slow_queued_local_candidate(self):
        item = task()
        local = worker("same-local")
        cloud = worker("same-cloud", provider="vendor", local=False)
        optimizer = ResourcePortfolioOptimizer()
        result = optimizer.optimize(
            tasks=[item],
            route_pools={
                item.id: [
                    route(local, cost=0, latency_ms=1_500),
                    route(cloud, cost=.02, latency_ms=300),
                ]
            },
            pools=[pool(), pool("vendor", local=False)],
            availability={
                "pool-local": {"slots": 0, "queue_depth": 8},
                "pool-vendor": {"slots": 2, "queue_depth": 0},
            },
            quality_floor=.8,
            mode=RunMode.ECONOMY,
            budget=None,
            deadline_ms=2_000,
        )
        self.assertEqual("vendor", result.selected[item.id].provider)

    def test_same_model_across_providers_is_compared_as_distinct_placement(self):
        item = task()
        optimizer = ResourcePortfolioOptimizer()
        routes = [
            route(worker("model-local"), cost=0),
            route(worker("model-cloud", provider="vendor", local=False), cost=.01),
        ]
        candidates = optimizer.candidates(
            task=item,
            routes=routes,
            pools=[pool(), pool("vendor", local=False)],
            quality_floor=.8,
        )
        self.assertEqual({"local", "vendor"}, {item.provider for item in candidates})
        self.assertEqual(len(candidates), len({item.placement_id for item in candidates}))

    def test_stronger_model_wins_when_weak_rework_erases_price_saving(self):
        item = task()
        weak = worker("weak", provider="weak-vendor", local=False)
        strong = worker("strong", provider="strong-vendor", local=False)
        result = ResourcePortfolioOptimizer().optimize(
            tasks=[item],
            route_pools={
                item.id: [
                    route(
                        weak,
                        quality=.7,
                        lower=.6,
                        cost=.0002,
                    ),
                    route(
                        strong,
                        quality=.97,
                        lower=.96,
                        cost=.0003,
                    ),
                ]
            },
            pools=[
                pool("weak-vendor", local=False),
                pool("strong-vendor", local=False),
            ],
            quality_floor=.55,
            mode=RunMode.ECONOMY,
            budget=None,
        )
        self.assertEqual("strong", result.selected[item.id].model_id)

    def test_critical_path_handles_dependencies_already_completed(self):
        pending = task("pending", dependencies=["completed"])
        result = ResourcePortfolioOptimizer().optimize(
            tasks=[pending],
            route_pools={pending.id: [route(worker("a"))]},
            pools=[pool()],
            quality_floor=.8,
            mode=RunMode.ECONOMY,
            budget=None,
        )
        self.assertGreater(result.summary["critical_path_p95_ms"], 0)

    def test_validator_prefers_independent_model(self):
        producer = task("producer")
        validator = task(
            "validator",
            dependencies=["producer"],
            task_type="validation",
        )
        a = worker("a")
        b = worker("b")
        result = ResourcePortfolioOptimizer().optimize(
            tasks=[producer, validator],
            route_pools={
                "producer": [route(a)],
                "validator": [route(a, cost=.001), route(b, cost=.002)],
            },
            pools=[pool()],
            quality_floor=.8,
            mode=RunMode.ECONOMY,
            budget=None,
        )
        self.assertNotEqual(
            result.selected["producer"].model_id,
            result.selected["validator"].model_id,
        )

    def test_hysteresis_rejects_noise_and_accepts_material_saving(self):
        item = task()
        candidates = ResourcePortfolioOptimizer().candidates(
            task=item,
            routes=[
                route(worker("a", provider="a", local=False), cost=.1),
                route(worker("b", provider="b", local=False), cost=.0999),
                route(worker("c", provider="c", local=False), cost=.05),
            ],
            pools=[
                pool("a", local=False),
                pool("b", local=False),
                pool("c", local=False),
            ],
            quality_floor=.8,
        )
        by_model = {item.model_id: item for item in candidates if item.inference_profile["reasoning_effort"] == "medium"}
        optimizer = ResourcePortfolioOptimizer()
        self.assertFalse(optimizer.should_switch(by_model["a"], by_model["b"]))
        self.assertTrue(optimizer.should_switch(by_model["a"], by_model["c"]))

    def test_split_requires_time_value_to_exceed_coordination(self):
        rejected = ResourcePortfolioOptimizer.evaluate_split(
            sequential_execution_ms=2_000,
            parallel_execution_ms=1_000,
            repeated_context_cost=.02,
            handoff_cost=.02,
            merge_cost=.02,
            validation_cost=.02,
            value_of_time_per_second=.01,
        )
        accepted = ResourcePortfolioOptimizer.evaluate_split(
            sequential_execution_ms=10_000,
            parallel_execution_ms=1_000,
            repeated_context_cost=.001,
            handoff_cost=.001,
            merge_cost=.001,
            validation_cost=.001,
            value_of_time_per_second=.01,
        )
        self.assertFalse(rejected["split"])
        self.assertTrue(accepted["split"])


class PlacementIntegrationTests(unittest.TestCase):
    def test_run_persists_selected_backups_and_conserving_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "runtime.sqlite3")
            manager = RunManager(runtime_db_path=path, recover=False)
            record = manager.create(
                RunRequest(
                    "分析一个产品方案并形成可验证报告",
                    budget=3,
                    max_concurrency=3,
                ),
                idempotency_key="economics-integration-0001",
            )
            for thread in list(manager.threads.values()):
                thread.join(timeout=15)
            self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
            placements = manager.placements(record.id)
            self.assertEqual(len(record.plan.tasks), sum(item["status"] == "selected" for item in placements))
            self.assertGreater(sum(item["status"] == "backup" for item in placements), 0)
            entries = manager.ledger(run_id=record.id)
            forecast = sum(
                task.placement["forecast_cost"]["p50"]["total"]
                for task in record.plan.tasks
            )
            self.assertTrue(
                ledger_conserves(
                    entries,
                    phase="forecast",
                    expected_total=forecast,
                )
            )
            manager.shutdown()

    def test_feature_flag_rolls_back_to_v3_router_without_placements(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"COSTWEAVE_PLACEMENT_V4": "0"}
        ):
            manager = RunManager(
                runtime_db_path=os.path.join(temp, "runtime.sqlite3"),
                recover=False,
            )
            record = manager.create(
                RunRequest("分析一个兼容模式下的产品方案", budget=3),
                idempotency_key="economics-rollback-0001",
            )
            for thread in list(manager.threads.values()):
                thread.join(timeout=15)
            self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
            self.assertEqual([], manager.placements(record.id))
            self.assertTrue(
                all(not item.placement for item in record.plan.tasks)
            )
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
