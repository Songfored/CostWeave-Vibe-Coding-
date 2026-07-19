from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from costweave.catalog_store import (
    CatalogConflictError,
    CatalogError,
    CatalogStore,
)
from costweave.domain import RunMode, TaskContract, WorkerProfile
from costweave.model_catalog import MODELS
from costweave.router import PredictiveRouter, RoutingError


def worker(
    model_id: str,
    *,
    analysis: float,
    price: float,
    tools: list[str] | None = None,
) -> WorkerProfile:
    return WorkerProfile(
        id=model_id,
        model_id=model_id,
        name=model_id,
        provider="test",
        specialty="测试模型",
        capabilities={
            "analysis": analysis,
            "structure": analysis,
            "validation": analysis,
            "risk": analysis,
            "safety": analysis,
        },
        cost_per_task=0,
        latency_factor=.8,
        reliability=analysis,
        reasoning=analysis,
        speed=.8,
        context_window=128_000,
        max_output_tokens=16_000,
        input_price_per_mtok=price,
        output_price_per_mtok=price,
        pricing_currency="USD",
        modalities=["text"],
        tools=tools or ["structured_output"],
        source_url="https://example.test/model",
        verified_at="2026-07-18",
        data_confidence=.9,
        routable=True,
    )


class CatalogStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name, "catalog.json")
        self.defaults = (
            worker("economy", analysis=.82, price=.1),
            worker("premium", analysis=.96, price=5),
        )
        self.store = CatalogStore(self.path, defaults=self.defaults)

    def tearDown(self):
        self.temp.cleanup()

    def test_json_and_csv_roundtrip_preserve_models(self):
        json_export = self.store.export_json()
        json_store = CatalogStore(Path(self.temp.name, "json.json"), defaults=self.defaults)
        result = json_store.import_data(json_export, mode="replace")
        self.assertEqual(2, result["total"])
        self.assertEqual(
            {item.id for item in self.store.snapshot()},
            {item.id for item in json_store.snapshot()},
        )

        csv_export = self.store.export_csv()
        csv_store = CatalogStore(Path(self.temp.name, "csv.json"), defaults=self.defaults)
        result = csv_store.import_data(csv_export, format="csv", mode="replace")
        self.assertEqual(2, result["total"])
        self.assertEqual("测试模型", csv_store.get("economy").specialty)

    def test_csv_text_with_bom_roundtrips(self):
        csv_export = "\ufeff" + self.store.export_csv()
        csv_store = CatalogStore(Path(self.temp.name, "bom.json"), defaults=self.defaults)
        result = csv_store.import_data(csv_export, format="csv", mode="replace")
        self.assertEqual(2, result["total"])
        self.assertEqual(
            {"economy", "premium"},
            {item.id for item in csv_store.snapshot()},
        )

    def test_invalid_import_is_atomic(self):
        before_revision = self.store.revision
        before_ids = [item.id for item in self.store.snapshot()]
        document = json.loads(self.store.export_json())
        document["models"][0]["reasoning"] = 1.5
        with self.assertRaises(CatalogError):
            self.store.import_data(document, mode="replace")
        self.assertEqual(before_revision, self.store.revision)
        self.assertEqual(before_ids, [item.id for item in self.store.snapshot()])

    def test_revision_conflict_prevents_lost_update(self):
        revision = self.store.revision
        self.store.update("economy", {"input_price_per_mtok": .2}, expected_revision=revision)
        with self.assertRaises(CatalogConflictError):
            self.store.update("economy", {"input_price_per_mtok": .3}, expected_revision=revision)

    def test_frozen_router_is_not_changed_by_catalog_edit(self):
        frozen = PredictiveRouter(store=self.store).freeze()
        self.store.update("economy", {"routable": False})
        current = PredictiveRouter(store=self.store).freeze()
        task = TaskContract(
            id="analysis",
            title="分析",
            objective="分析",
            task_type="analysis",
            required_capabilities=["analysis", "structure"],
            acceptance_criteria=["完整"],
            output_schema=["summary", "evidence", "confidence"],
            difficulty=3,
            estimated_input_tokens=2_000,
            estimated_output_tokens=800,
        )
        self.assertEqual("economy", frozen.route(task, RunMode.ECONOMY, .7).worker.id)
        self.assertEqual("premium", current.route(task, RunMode.ECONOMY, .7).worker.id)

    def test_function_calling_does_not_imply_code_execution(self):
        only_function_calling = worker(
            "function-only",
            analysis=.95,
            price=.1,
            tools=["function_calling", "structured_output"],
        )
        router = PredictiveRouter(workers=(only_function_calling,))
        task = TaskContract(
            id="code",
            title="运行代码",
            objective="运行测试",
            task_type="coding",
            required_capabilities=["analysis"],
            requires_tools=["code_execution"],
            acceptance_criteria=["通过"],
            output_schema=["summary", "evidence", "confidence"],
            difficulty=4,
        )
        with self.assertRaises(RoutingError):
            router.route(task, RunMode.BALANCED, .6)

    def test_single_catalog_list_value_is_not_split_into_characters(self):
        luna = next(model for model in MODELS if model.id == "gpt-5.6-luna")
        sonnet = next(model for model in MODELS if model.id == "claude-sonnet-5")
        self.assertEqual(["复杂任务能力低于 Sol"], luna.limitations)
        self.assertEqual(["官方曾有阶段性优惠，目录使用长期标价"], sonnet.limitations)

    def test_corrupt_persistent_catalog_falls_back_and_can_reset(self):
        self.path.write_text("{broken json", encoding="utf-8")
        recovered = CatalogStore(self.path, defaults=self.defaults)
        self.assertEqual(
            {"economy", "premium"},
            {item.id for item in recovered.snapshot()},
        )
        self.assertTrue(recovered.payload()["metadata"]["load_warning"])

        recovered.reset()
        reloaded = CatalogStore(self.path, defaults=self.defaults)
        self.assertIsNone(reloaded.payload()["metadata"]["load_warning"])
        self.assertEqual(
            {"economy", "premium"},
            {item.id for item in reloaded.snapshot()},
        )


if __name__ == "__main__":
    unittest.main()
