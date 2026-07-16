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
            "outcome-card",
            "copy-result",
            "download-result",
            "dag",
            "timeline",
            "contract",
        }
        self.assertTrue(required <= set(inventory.ids), required - set(inventory.ids))

    def test_primary_controls_are_labelled(self):
        inventory = _MarkupInventory()
        inventory.feed(self.html)
        self.assertIn("goal", inventory.labels)
        self.assertIn("quality", inventory.labels)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('aria-label="页面导航"', self.html)

    def test_responsive_and_accessibility_guards_exist(self):
        self.assertIn("@media (max-width: 980px)", self.css)
        self.assertIn("@media (max-width: 700px)", self.css)
        self.assertIn("@media (max-width: 480px)", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertIn(":focus-visible", self.css)

    def test_result_history_and_failure_interactions_are_wired(self):
        for function_name in (
            "renderOutcome",
            "renderRunError",
            "renderHistory",
            "copyResult",
            "downloadResult",
            "revealTerminalState",
        ):
            self.assertIn(f"function {function_name}", self.js)


if __name__ == "__main__":
    unittest.main()
