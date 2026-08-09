"""CLI contract tests for the v0.6.0 offline tournament."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TournamentCLIV060Tests(unittest.TestCase):
    def _run(self, *args: str):
        return subprocess.run(
            [sys.executable, "-B", "-m", "costweave.tournament_cli", *args],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            encoding="utf-8",
        )

    def test_default_seed_is_offline_and_emits_report(self):
        result = self._run("--bootstrap-samples", "100")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("E0-simulated", payload["evidence_level"])
        self.assertEqual(0, payload["network_calls"])
        self.assertEqual(0, payload["provider_calls"])
        self.assertIn("report_hash", payload)

    def test_pretty_and_output_create_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory, "report.json")
            result = self._run("--pretty", "--bootstrap-samples", "100", "--output", str(destination))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(destination.is_file())
            self.assertEqual("{", destination.read_text(encoding="utf-8")[0])
            repeated = self._run("--bootstrap-samples", "100", "--output", str(destination))
            self.assertEqual(2, repeated.returncode)

    def test_invalid_contract_arguments_return_two_without_secret_or_network(self):
        result = self._run("--bootstrap-samples", "99")
        self.assertEqual(2, result.returncode)
        self.assertNotIn("api", result.stderr.lower())
        self.assertNotIn("http", result.stderr.lower())

    def test_unknown_dataset_path_returns_two_without_path_leak(self):
        result = self._run("--dataset", "C:/does/not/exist.json", "--bootstrap-samples", "100")
        self.assertEqual(2, result.returncode)
        self.assertNotIn("does/not/exist", result.stderr)

    def test_external_decisions_are_validated_before_run(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl") as handle:
            path = Path(handle.name)
        path.write_text('{"invalid":true}\n', encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        result = self._run("--external-decisions", str(path), "--bootstrap-samples", "100")
        self.assertEqual(2, result.returncode)


if __name__ == "__main__":
    unittest.main()
