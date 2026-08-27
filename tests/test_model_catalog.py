import json
import tempfile
import unittest
from pathlib import Path

from costweave.catalog_store import CatalogStore
from costweave.domain import RunMode, TaskContract
from costweave.model_catalog import EVIDENCE_REGISTRY, MODELS, SNAPSHOT_DATE
from costweave.router import PredictiveRouter, RoutingError


class ModelCatalogTests(unittest.TestCase):
    def test_catalog_has_traceable_vendor_facts(self):
        providers = {model.provider for model in MODELS}
        self.assertTrue({"openai", "anthropic", "google", "deepseek", "xai", "mistral"} <= providers)
        self.assertTrue(all(model.verified_at == SNAPSHOT_DATE for model in MODELS))
        cloud = [model for model in MODELS if not model.local]
        self.assertTrue(all(model.source_url for model in cloud))
        self.assertTrue(all(model.context_window > 0 for model in MODELS))

    def test_2026_08_catalog_prices_and_representative_providers(self):
        by_id = {model.id: model for model in MODELS}
        self.assertEqual("2026-08-27", SNAPSHOT_DATE)
        self.assertTrue(
            {"alibaba", "moonshot", "minimax"}
            <= {model.provider for model in MODELS}
        )
        self.assertEqual((.2, .02, 1.2), (
            by_id["gpt-5.6-luna"].input_price_per_mtok,
            by_id["gpt-5.6-luna"].cached_input_price_per_mtok,
            by_id["gpt-5.6-luna"].output_price_per_mtok,
        ))
        self.assertEqual((4, .4, 20), (
            by_id["gpt-5.6-sol"].input_price_per_mtok,
            by_id["gpt-5.6-sol"].cached_input_price_per_mtok,
            by_id["gpt-5.6-sol"].output_price_per_mtok,
        ))
        self.assertFalse(by_id["kimi-k3"].routable)
        self.assertEqual("CNY", by_id["kimi-k3"].pricing_currency)

    def test_evidence_registry_covers_catalog_and_separates_source_types(self):
        path = Path(__file__).parents[1] / "costweave" / EVIDENCE_REGISTRY
        evidence = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(SNAPSHOT_DATE, evidence["snapshot_date"])
        self.assertEqual({model.id for model in MODELS}, set(evidence["models"]))
        self.assertTrue(evidence["official_sources"])
        self.assertTrue(evidence["independent_sources"])
        self.assertTrue(evidence["benchmark_snapshots"]["vendor_reported"])
        for row in evidence["benchmark_snapshots"]["lmarena"]:
            self.assertIn("model_variant", row)
            self.assertIn("uncertainty", row)
        by_id = {model.id: model for model in MODELS}
        for model_id, facts in evidence["models"].items():
            if "price" not in facts:
                continue
            price = facts["price"]
            model = by_id[model_id]
            self.assertEqual(price["currency"], model.pricing_currency)
            self.assertEqual(price["input"], model.input_price_per_mtok)
            self.assertEqual(price["cached_input"], model.cached_input_price_per_mtok)
            self.assertEqual(price["output"], model.output_price_per_mtok)

    def test_catalog_hash_is_stable_after_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            store = CatalogStore(path)
            store.reset()
            written_hash = store.payload()["metadata"]["catalog_hash"]
            loaded_hash = CatalogStore(path).payload()["metadata"]["catalog_hash"]
            self.assertEqual(written_hash, loaded_hash)

    def test_router_uses_local_model_for_easy_low_cost_work(self):
        task = TaskContract(
            id="easy", title="结构化摘要", objective="整理已有内容", task_type="analysis",
            required_capabilities=["analysis", "structure"], acceptance_criteria=["完整"],
            output_schema=["summary", "evidence", "confidence"], difficulty=2,
            estimated_input_tokens=2_000, estimated_output_tokens=800,
        )
        decision = PredictiveRouter(workers=MODELS).route(task, RunMode.ECONOMY, .72)
        self.assertTrue(decision.worker.local)

    def test_high_quality_floor_forces_capable_model_or_explicit_escalation(self):
        task = TaskContract(
            id="hard", title="复杂系统重构", objective="解决跨域高风险问题", task_type="coding",
            required_capabilities=["coding", "planning", "risk", "validation"],
            acceptance_criteria=["可靠"], output_schema=["summary", "evidence", "confidence"],
            difficulty=10, risk_level="high", estimated_input_tokens=80_000,
            estimated_output_tokens=20_000,
        )
        decision = PredictiveRouter(workers=MODELS).route(task, RunMode.BALANCED, .90)
        self.assertIn(decision.worker.tier, {"frontier", "premium"})
        self.assertGreaterEqual(decision.success_lower_bound, .90)
        with self.assertRaises(RoutingError):
            PredictiveRouter(workers=MODELS).route(task, RunMode.BALANCED, .98)


if __name__ == "__main__":
    unittest.main()
