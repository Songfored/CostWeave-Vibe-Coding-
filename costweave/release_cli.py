"""Offline fresh-install, execution, replay, and backup verifier."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from . import __version__
from .adapters import AdapterRuntimePolicy
from .gateway_litellm import public_litellm_gateway_policy
from .catalog_store import CatalogStore
from .domain import RunMode, RunRequest, RunStatus
from .engine import RunManager
from .release_readiness import build_release_status
from .runtime_maintenance import RuntimeMaintenance


TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


def verify_fresh_install(*, timeout_seconds: float = 30.0) -> dict:
    with tempfile.TemporaryDirectory(prefix="costweave-release-") as temporary:
        root = Path(temporary)
        manager = RunManager(
            CatalogStore(root / "catalog.json"),
            runtime_db_path=str(root / "runtime.sqlite3"),
            recover=False,
        )
        record = manager.create(
            RunRequest(
                goal="Verify a fresh offline CostWeave stable installation",
                mode=RunMode.ECONOMY,
                budget=0.25,
                max_concurrency=2,
            ),
            idempotency_key="release-fresh-install-run-v0584",
        )
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while record.status not in TERMINAL and time.monotonic() < deadline:
            time.sleep(0.05)
        manager.shutdown(wait_timeout=5.0)
        if record.status not in TERMINAL:
            raise RuntimeError("fresh-install verification run timed out")
        kernel = manager.control_kernel
        if kernel is None:
            raise RuntimeError("persistent runtime is disabled")
        status = build_release_status(
            kernel,
            adapter_policy=AdapterRuntimePolicy.from_environment().public(),
            litellm_policy=public_litellm_gateway_policy(),
        )
        maintenance = RuntimeMaintenance(kernel.store.path)
        backup = maintenance.create_backup()
        backup_verification = maintenance.verify_backup(backup["backup_id"])
        health = kernel.health()
        no_external_attempts = int(health.get("external_call_attempts", 0)) == 0
        succeeded = bool(
            record.status == RunStatus.COMPLETED
            and status["engineering_ready"]
            and backup_verification["healthy"]
            and backup_verification["manifest_match"]
            and backup_verification["logical_snapshot_match"]
            and no_external_attempts
        )
        return {
            "success": succeeded,
            "version": __version__,
            "run": {
                "status": record.status.value,
                "revision": record.runtime_revision,
            },
            "release": status,
            "backup": {
                "healthy": backup_verification["healthy"],
                "manifest_match": backup_verification["manifest_match"],
                "logical_snapshot_match": backup_verification[
                    "logical_snapshot_match"
                ],
            },
            "external_call_attempts": int(
                health.get("external_call_attempts", 0)
            ),
            "evidence_level": "E0-local-verification",
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify CostWeave v0.5 stable offline installation"
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        report = verify_fresh_install(timeout_seconds=args.timeout)
    except Exception as exc:
        report = {
            "success": False,
            "version": __version__,
            "error": f"{type(exc).__name__}: {exc}",
            "evidence_level": "E0-local-verification",
        }
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0 if report.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
