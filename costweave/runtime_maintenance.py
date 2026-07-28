"""Safe diagnostics, backup, verification, and restore for the local runtime."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKUP_ID = re.compile(r"^runtime-\d{8}T\d{6}Z-[a-f0-9]{8}\.sqlite3$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class RuntimeMaintenance:
    """Operate only on a configured runtime and its sibling backup directory."""

    def __init__(self, runtime_path: str | Path) -> None:
        self.runtime_path = Path(runtime_path).resolve()
        self.backup_dir = self.runtime_path.parent / "backups"

    @staticmethod
    def _checks(connection: sqlite3.Connection) -> dict[str, Any]:
        quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        foreign = [
            {
                "table": row[0],
                "rowid": row[1],
                "parent": row[2],
                "foreign_key": row[3],
            }
            for row in connection.execute("PRAGMA foreign_key_check")
        ]
        return {
            "quick_check": quick,
            "foreign_key_violations": foreign,
            "healthy": quick == ["ok"] and not foreign,
        }

    def diagnostics(self) -> dict[str, Any]:
        if not self.runtime_path.is_file():
            return {
                "healthy": False,
                "database": self.runtime_path.name,
                "error": "runtime_database_missing",
            }
        with closing(
            sqlite3.connect(self.runtime_path, timeout=5.0)
        ) as connection:
            checks = self._checks(connection)
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
            migrations = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
        return {
            **checks,
            "database": self.runtime_path.name,
            "journal_mode": str(journal).lower(),
            "migrations": int(migrations),
            "size_bytes": self.runtime_path.stat().st_size,
            "sha256": _sha256(self.runtime_path),
            "backups": len(self.list_backups()),
        }

    def create_backup(self) -> dict[str, Any]:
        diagnostics = self.diagnostics()
        if not diagnostics.get("healthy"):
            raise RuntimeError("refusing to back up an unhealthy runtime database")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        seed = f"{stamp}:{diagnostics['sha256']}".encode("utf-8")
        name = f"runtime-{stamp}-{hashlib.sha256(seed).hexdigest()[:8]}.sqlite3"
        destination = self.backup_dir / name
        temporary = destination.with_suffix(".sqlite3.tmp")
        with closing(
            sqlite3.connect(self.runtime_path, timeout=5.0)
        ) as source:
            with closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
        with closing(sqlite3.connect(temporary)) as connection:
            verified = self._checks(connection)
        if not verified["healthy"]:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("backup verification failed")
        temporary.replace(destination)
        manifest = {
            "backup_id": name,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "size_bytes": destination.stat().st_size,
            "sha256": _sha256(destination),
            "source_database": self.runtime_path.name,
            "verified": True,
        }
        destination.with_suffix(".json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return manifest

    def list_backups(self) -> list[dict[str, Any]]:
        if not self.backup_dir.is_dir():
            return []
        result: list[dict[str, Any]] = []
        for path in sorted(self.backup_dir.glob("runtime-*.sqlite3"), reverse=True):
            if not BACKUP_ID.fullmatch(path.name):
                continue
            manifest_path = path.with_suffix(".json")
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["integrity_match"] = (
                    manifest.get("sha256") == _sha256(path)
                )
            except (OSError, ValueError, TypeError):
                continue
            result.append(manifest)
        return result

    def verify_backup(self, backup_id: str) -> dict[str, Any]:
        if not BACKUP_ID.fullmatch(backup_id):
            raise ValueError("invalid backup id")
        path = self.backup_dir / backup_id
        if not path.is_file():
            raise KeyError(backup_id)
        with closing(sqlite3.connect(path, timeout=5.0)) as connection:
            checks = self._checks(connection)
        manifest_path = path.with_suffix(".json")
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.is_file()
            else {}
        )
        digest = _sha256(path)
        return {
            "backup_id": backup_id,
            **checks,
            "sha256": digest,
            "manifest_match": manifest.get("sha256") == digest,
        }

    def restore_backup(
        self, backup_id: str, *, runtime_closed: bool = False
    ) -> dict[str, Any]:
        """Restore only when the caller has explicitly stopped the runtime."""

        if not runtime_closed:
            raise RuntimeError("runtime must be stopped before restore")
        verification = self.verify_backup(backup_id)
        if not verification["healthy"] or not verification["manifest_match"]:
            raise RuntimeError("refusing to restore an unverified backup")
        source_path = self.backup_dir / backup_id
        temporary = self.runtime_path.with_suffix(".restore.tmp")
        with closing(sqlite3.connect(source_path)) as source:
            with closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
        with closing(sqlite3.connect(temporary)) as connection:
            checks = self._checks(connection)
        if not checks["healthy"]:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("restored database failed verification")
        temporary.replace(self.runtime_path)
        return {
            "restored": True,
            "backup_id": backup_id,
            "sha256": _sha256(self.runtime_path),
        }
