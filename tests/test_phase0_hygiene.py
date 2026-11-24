from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

from costweave.event_store import EventStore
from costweave.experimental_calls import ExternalCostOverrunError
from costweave.outcome_dataset import SENSITIVE_PATTERNS, _sensitive_findings
from costweave.release_packaging import create_source_zip, stage_source, validate_archive
from costweave.security_store import ProjectRevokedError, SecurityStore


class Phase0HygieneTests(unittest.TestCase):
    def test_sensitive_patterns_detect_secrets_emails_and_phones(self) -> None:
        self.assertEqual(4, len(SENSITIVE_PATTERNS))
        self.assertTrue(_sensitive_findings({"note": "api_key=sk-test-12345"}))
        self.assertTrue(_sensitive_findings({"auth": "Bearer abcdef1234567890"}))
        self.assertTrue(_sensitive_findings({"contact": "alice.dev@example.com"}))
        self.assertTrue(_sensitive_findings({"mobile": "13812345678"}))
        self.assertEqual([], _sensitive_findings({"summary": "clean metrics", "units": 42}))
        self.assertIsNotNone(ExternalCostOverrunError)

    def test_packaged_archive_contains_examples_and_scripts_and_runs_quickstart(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            stage_source(repo_root, staging)
            self.assertTrue((staging / "examples" / "artifact-runtime" / "handoff.json").is_file())
            self.assertTrue((staging / "examples" / "artifact-runtime" / "catalog.json").is_file())
            self.assertTrue((staging / "scripts" / "compare_artifact_runtime.py").is_file())

            archive_path = root / "source.zip"
            create_source_zip(staging, archive_path)
            self.assertEqual(0, validate_archive(archive_path)["violations"])

            unpacked = root / "unpacked"
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(unpacked)
            extracted_root = next(p for p in unpacked.iterdir() if p.is_dir())

            db_path = root / "quickstart.sqlite3"
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            for cmd in (
                [sys.executable, "-m", "costweave", "--db", str(db_path), "plan",
                 "examples/artifact-runtime/handoff.json", "--catalog", "examples/artifact-runtime/catalog.json"],
                [sys.executable, "-m", "costweave", "--db", str(db_path), "run",
                 "examples/artifact-runtime/handoff.json", "--catalog", "examples/artifact-runtime/catalog.json",
                 "--id", "first-handoff"],
                [sys.executable, "-m", "costweave", "--db", str(db_path), "events", "first-handoff"],
            ):
                proc = subprocess.run(cmd, cwd=extracted_root, env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(0, proc.returncode, proc.stderr)

    def test_shared_project_gate_allows_parallel_io_while_revocation_drains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "security.sqlite3"
            EventStore(db_path)
            store = SecurityStore(db_path)
            both_entered = threading.Barrier(2)
            release_io = threading.Event()
            errors: list[Exception] = []

            def worker() -> None:
                try:
                    with store.active_project_operation("parallel-proj"):
                        both_entered.wait(timeout=3.0)
                        release_io.wait(timeout=3.0)
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            time.sleep(0.08)

            revoked = threading.Event()

            def revoker() -> None:
                store.revoke_project("parallel-proj", idempotency_key="revoke-parallel-001")
                revoked.set()

            tr = threading.Thread(target=revoker)
            tr.start()
            time.sleep(0.05)
            self.assertFalse(revoked.is_set(), "revocation must wait for both active operations")

            release_io.set()
            t1.join(3.0)
            t2.join(3.0)
            tr.join(3.0)
            self.assertEqual([], errors)
            self.assertTrue(revoked.is_set())

            with self.assertRaises(ProjectRevokedError):
                with store.active_project_operation("parallel-proj"):
                    pass


if __name__ == "__main__":
    unittest.main()
