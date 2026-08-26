from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from costweave.release_packaging import (
    ReleasePackagingError,
    create_source_zip,
    forbidden_reason,
    stage_source,
    validate_archive,
)
from costweave.version import ACTIVE_WORK_PACKAGE, VERSION_TAG, __version__


class ReleasePackagingV068Tests(unittest.TestCase):
    def test_public_identity_has_one_canonical_value(self):
        self.assertEqual("0.6.8", __version__)
        self.assertEqual("v0.6.8", VERSION_TAG)
        self.assertEqual("Delivery & UX Convergence", ACTIVE_WORK_PACKAGE)

    def test_generated_and_sensitive_paths_are_rejected(self):
        for path in (
            "work/runtime.sqlite3",
            "outputs/old.zip",
            ".codex/local.json",
            "logs/session.log",
            "costweave/__pycache__/version.pyc",
            "secrets/.env.production",
            "private/provider.key",
        ):
            self.assertIsNotNone(forbidden_reason(path), path)
        self.assertIsNone(forbidden_reason("costweave/version.py"))

    def test_clean_stage_and_zip_use_allowlist(self):
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            result = stage_source(source_root, staging)
            self.assertGreater(result["files"], 100)
            self.assertTrue((staging / "costweave" / "version.py").is_file())
            self.assertFalse((staging / "full_v064_tests.log").exists())
            output = root / "CostWeave-v0.6.8.zip"
            create_source_zip(staging, output)
            validation = validate_archive(output)
            self.assertEqual(0, validation["violations"])
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
            self.assertTrue(
                all(name.startswith("CostWeave-v0.6.8/") for name in names)
            )

    def test_stage_refuses_unmarked_replacement(self):
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary) / "staging"
            staging.mkdir()
            (staging / "user-file.txt").write_text("preserve", encoding="utf-8")
            with self.assertRaises(ReleasePackagingError):
                stage_source(source_root, staging, clean=True)
            self.assertTrue((staging / "user-file.txt").is_file())


if __name__ == "__main__":
    unittest.main()
