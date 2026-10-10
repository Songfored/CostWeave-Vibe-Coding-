from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from costweave.real_smoke_eval import (
    PRICE_ASSUMPTIONS,
    RealSmokeError,
    _load_registry,
    validate_output,
)


class RealSmokeEvalTests(unittest.TestCase):
    def test_validator_requires_exact_deterministic_result(self):
        accepted = validate_output('{"answer":402,"checksum":"cswae"}')
        self.assertTrue(accepted["accepted"])
        self.assertFalse(
            validate_output('{"answer":402,"checksum":"cswae","extra":1}')["accepted"]
        )
        self.assertFalse(validate_output("not json")["accepted"])

    def test_validator_accepts_json_fence_but_not_wrong_math(self):
        self.assertTrue(
            validate_output('```json\n{"answer":402,"checksum":"cswae"}\n```')["accepted"]
        )
        self.assertFalse(
            validate_output('{"answer":401,"checksum":"cswae"}')["accepted"]
        )

    def test_registry_requires_verified_channels_and_loaded_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.json"
            rows = []
            for provider_id in PRICE_ASSUMPTIONS:
                env_name = "TEST_" + provider_id.upper().replace("-", "_")
                os.environ[env_name] = "test-secret-value"
                rows.append(
                    {
                        "provider_id": provider_id,
                        "discovery_status": "verified",
                        "selection_verified": True,
                        "selected_model": "fixture-model",
                        "chat_completions_url": "https://example.invalid/v1/chat/completions",
                        "secret_env": env_name,
                    }
                )
            path.write_text(json.dumps({"providers": rows}), encoding="utf-8")
            try:
                self.assertEqual(3, len(_load_registry(path)))
                rows[0]["selection_verified"] = False
                path.write_text(json.dumps({"providers": rows}), encoding="utf-8")
                with self.assertRaises(RealSmokeError):
                    _load_registry(path)
            finally:
                for row in rows:
                    os.environ.pop(row["secret_env"], None)


if __name__ == "__main__":
    unittest.main()
