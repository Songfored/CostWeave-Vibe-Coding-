import unittest

from costweave.domain import RunMode, TaskContract
from costweave.model_catalog import MODELS, SNAPSHOT_DATE
from costweave.router import PredictiveRouter, RoutingError


class ModelCatalogTests(unittest.TestCase):
    def test_catalog_has_traceable_vendor_facts(self):
        providers = {model.provider for model in MODELS}
        self.assertTrue({"openai", "anthropic", "google", "deepseek", "xai", "mistral"} <= providers)
        self.assertTrue(all(model.verified_at == SNAPSHOT_DATE for model in MODELS))
        cloud = [model for model in MODELS if not model.local]
        self.assertTrue(all(model.source_url for model in cloud))
        self.assertTrue(all(model.context_window > 0 for model in MODELS))

    def test_router_uses_local_model_for_easy_low_cost_work(self):
        task = TaskContract(
            id="easy", title="结构化摘要", objective="整理已有内容", task_type="analysis",
            required_capabilities=["analysis", "structure"], acceptance_criteria=["完整"],
            output_schema=["summary", "evidence", "confidence"], difficulty=2,
            estimated_input_tokens=2_000, estimated_output_tokens=800,
        )
        decision = PredictiveRouter().route(task, RunMode.ECONOMY, .72)
        self.assertTrue(decision.worker.local)

    def test_high_quality_floor_forces_capable_model_or_explicit_escalation(self):
        task = TaskContract(
            id="hard", title="复杂系统重构", objective="解决跨域高风险问题", task_type="coding",
            required_capabilities=["coding", "planning", "risk", "validation"],
            acceptance_criteria=["可靠"], output_schema=["summary", "evidence", "confidence"],
            difficulty=10, risk_level="high", estimated_input_tokens=80_000,
            estimated_output_tokens=20_000,
        )
        decision = PredictiveRouter().route(task, RunMode.BALANCED, .90)
        self.assertIn(decision.worker.tier, {"frontier", "premium"})
        self.assertGreaterEqual(decision.success_lower_bound, .90)
        with self.assertRaises(RoutingError):
            PredictiveRouter().route(task, RunMode.BALANCED, .98)


if __name__ == "__main__":
    unittest.main()
