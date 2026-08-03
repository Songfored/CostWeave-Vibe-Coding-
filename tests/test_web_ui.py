from __future__ import annotations

import unittest
from html.parser import HTMLParser
from importlib.resources import files


class _MarkupInventory(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.labels: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if tag == "label" and attributes.get("for"):
            self.labels.add(attributes["for"])


class WebUiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        web = files("costweave").joinpath("web")
        cls.html = web.joinpath("index.html").read_text(encoding="utf-8")
        cls.css = web.joinpath("styles.css").read_text(encoding="utf-8")
        cls.js = web.joinpath("app.js").read_text(encoding="utf-8")

    def test_markup_has_unique_ids_and_core_surfaces(self):
        inventory = _MarkupInventory()
        inventory.feed(self.html)
        self.assertEqual(len(inventory.ids), len(set(inventory.ids)))
        required = {
            "workspace",
            "run-form",
            "goal",
            "submit-button",
            "recent-runs",
            "run-panel",
            "run-status",
            "run-error-panel",
            "planning-card",
            "planning-mode",
            "planning-metrics",
            "planning-difficulty",
            "planning-escalation",
            "planning-candidates",
            "planning-roles",
            "planning-deliberation",
            "planning-proposals",
            "planning-conflicts",
            "planning-adjudication",
            "outcome-card",
            "copy-result",
            "download-result",
            "dag",
            "artifact-graph",
            "artifact-count",
            "timeline",
            "contract",
            "analysis-signals",
            "routing-summary",
            "scheduler-summary",
            "media-project-card",
            "media-shot-list",
            "approve-media-project",
            "model-catalog",
            "catalog-stats",
            "catalog-search",
            "add-model",
            "import-models",
            "export-models",
            "model-editor",
            "model-form",
            "import-dialog",
        }
        self.assertTrue(required <= set(inventory.ids), required - set(inventory.ids))

    def test_primary_controls_are_labelled(self):
        inventory = _MarkupInventory()
        inventory.feed(self.html)
        self.assertIn("goal", inventory.labels)
        self.assertIn("quality", inventory.labels)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('aria-label="页面导航"', self.html)
        self.assertNotIn('id="model-catalog" class="model-catalog" aria-live', self.html)

    def test_responsive_and_accessibility_guards_exist(self):
        self.assertIn("@media (max-width: 980px)", self.css)
        self.assertIn("@media (max-width: 700px)", self.css)
        self.assertIn("@media (max-width: 480px)", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertIn(":focus-visible", self.css)
        self.assertIn("max-height: calc(100vh - 36px)", self.css)
        self.assertIn("overflow-y: auto", self.css)

    def test_result_history_and_failure_interactions_are_wired(self):
        for function_name in (
            "renderOutcome",
            "renderArtifacts",
            "renderRunError",
            "renderPlanning",
            "renderMediaProject",
            "renderHistory",
            "copyResult",
            "downloadResult",
            "revealTerminalState",
            "loadCatalog",
            "renderCatalog",
            "openModelEditor",
            "saveModel",
            "importCatalog",
            "resetCatalog",
        ):
            self.assertIn(f"function {function_name}", self.js)
        self.assertIn("analysis.classifications", self.js)
        self.assertIn("metadata.load_warning", self.js)
        self.assertIn("run.scheduling", self.js)
        self.assertIn('data-queue-state=', self.js)
        self.assertIn("v0.5.6.4.2 Offline Evaluation Gate", self.html)
        self.assertIn("标签治理", self.html)
        self.assertIn("端到端成本", self.js)
        self.assertIn("预期返工", self.js)
        self.assertIn("关键成功标准", self.js)
        self.assertIn("恢复记录", self.js)
        self.assertIn("function renderForecast", self.js)
        self.assertIn("function renderRecovery", self.js)
        self.assertIn("function approveMediaProject", self.js)
        self.assertIn("first_pass_success", self.js)
        self.assertIn('id="forecast-card"', self.html)
        self.assertIn('id="planning-card"', self.html)
        self.assertIn('id="recovery-card"', self.html)
        self.assertIn("max_management_depth", self.js)
        self.assertIn("difficulty_vector", self.js)
        self.assertIn("pre_execution_escalation", self.js)
        self.assertIn("expected_loss", self.js)
        self.assertIn("anonymous_proposals", self.js)
        self.assertIn("hard_constraints_passed", self.js)
        self.assertIn("selected_aliases", self.js)


if __name__ == "__main__":
    unittest.main()
