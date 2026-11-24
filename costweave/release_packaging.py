"""Build clean, auditable release artifacts from an allowlisted staging tree."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

from .version import ACTIVE_WORK_PACKAGE, VERSION_TAG, __version__


ROOT_FILES = ("README.md", "LICENSE", "pyproject.toml", ".gitignore")
SOURCE_DIRECTORIES = ("costweave", "docs", "examples", "scripts", "tests")
BLOCKED_PARTS = {
    ".agents",
    ".codex",
    ".git",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "__pycache__",
    "build",
    "dist",
    "outputs",
    "work",
}
BLOCKED_NAMES = {
    ".env",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
    "runtime.db",
    "runtime.sqlite",
    "runtime.sqlite3",
}
BLOCKED_SUFFIXES = (
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".key",
    ".log",
    ".pem",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
)
STAGING_MARKER = ".costweave-clean-staging"
ARTIFACT_MARKER = ".costweave-release-artifacts"


class ReleasePackagingError(RuntimeError):
    """Raised when a release tree or artifact violates the delivery contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalised_parts(value: str | Path) -> tuple[str, ...]:
    return tuple(part.lower() for part in PurePosixPath(str(value).replace("\\", "/")).parts)


def forbidden_reason(value: str | Path) -> str | None:
    parts = _normalised_parts(value)
    if not parts:
        return "empty path"
    if any(part in {"", ".", ".."} for part in parts):
        return "unsafe path segment"
    if parts[0].endswith(":") or str(value).startswith(("/", "\\")):
        return "absolute path"
    if any(part in BLOCKED_PARTS for part in parts):
        return "generated or local-only directory"
    name = parts[-1]
    if name in BLOCKED_NAMES or name.startswith(".env."):
        return "secret or runtime file"
    if name.endswith(BLOCKED_SUFFIXES):
        return "runtime, log, cache, or secret suffix"
    return None


def _source_files(source_root: Path) -> Iterable[tuple[Path, Path]]:
    for root_name in ROOT_FILES:
        source = source_root / root_name
        if not source.is_file():
            raise ReleasePackagingError(f"required source file is missing: {root_name}")
        yield source, Path(root_name)
    for directory_name in SOURCE_DIRECTORIES:
        directory = source_root / directory_name
        if not directory.is_dir():
            raise ReleasePackagingError(
                f"required source directory is missing: {directory_name}"
            )
        for source in sorted(directory.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root)
            if forbidden_reason(relative) is None:
                yield source, relative


def _safe_replaceable_directory(path: Path, marker: str) -> bool:
    return path.is_dir() and (path / marker).is_file()


def _prepare_directory(path: Path, *, marker: str, clean: bool) -> None:
    if path.exists():
        if not clean:
            raise ReleasePackagingError(f"directory already exists: {path}")
        if not _safe_replaceable_directory(path, marker):
            raise ReleasePackagingError(
                f"refusing to replace unmarked directory: {path}"
            )
        shutil.rmtree(path)
    path.mkdir(parents=True)
    (path / marker).write_text(f"CostWeave {VERSION_TAG}\n", encoding="utf-8")


def stage_source(source_root: Path, staging_root: Path, *, clean: bool = False) -> dict:
    source_root = source_root.resolve()
    staging_root = staging_root.resolve()
    if staging_root == source_root or source_root in staging_root.parents:
        # Staging below the repository is expected only in an ignored work/ tree.
        try:
            first = staging_root.relative_to(source_root).parts[0].lower()
        except (IndexError, ValueError):
            first = ""
        if first != "work":
            raise ReleasePackagingError(
                "staging inside the repository is only allowed below work/"
            )
    _prepare_directory(staging_root, marker=STAGING_MARKER, clean=clean)
    copied: list[str] = []
    for source, relative in _source_files(source_root):
        reason = forbidden_reason(relative)
        if reason is not None:
            raise ReleasePackagingError(f"forbidden staged path {relative}: {reason}")
        target = staging_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(relative.as_posix())
    validate_tree(staging_root, ignored_names={STAGING_MARKER})
    return {
        "source_root": str(source_root),
        "staging_root": str(staging_root),
        "files": len(copied),
        "source_manifest_sha256": hashlib.sha256(
            "\n".join(copied).encode("utf-8")
        ).hexdigest(),
    }


def validate_tree(root: Path, *, ignored_names: set[str] | None = None) -> None:
    ignored = set(ignored_names or ())
    violations: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name in ignored:
            continue
        relative = path.relative_to(root)
        reason = forbidden_reason(relative)
        if reason:
            violations.append(f"{relative.as_posix()}: {reason}")
    if violations:
        raise ReleasePackagingError(
            "release tree contains forbidden entries: " + "; ".join(violations[:20])
        )


def create_source_zip(staging_root: Path, output: Path) -> Path:
    prefix = f"CostWeave-{VERSION_TAG}"
    timestamp = (2000, 1, 1, 0, 0, 0)
    files = [
        path
        for path in sorted(staging_root.rglob("*"))
        if path.is_file() and path.name != STAGING_MARKER
    ]
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            relative = path.relative_to(staging_root).as_posix()
            reason = forbidden_reason(relative)
            if reason:
                raise ReleasePackagingError(f"forbidden zip path {relative}: {reason}")
            info = zipfile.ZipInfo(f"{prefix}/{relative}", timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return output


def create_source_tar(staging_root: Path, output: Path) -> Path:
    prefix = f"costweave-{__version__}"
    files = [
        path
        for path in sorted(staging_root.rglob("*"))
        if path.is_file() and path.name != STAGING_MARKER
    ]
    with output.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=946684800) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in files:
                    relative = path.relative_to(staging_root).as_posix()
                    reason = forbidden_reason(relative)
                    if reason:
                        raise ReleasePackagingError(
                            f"forbidden source archive path {relative}: {reason}"
                        )
                    info = tarfile.TarInfo(f"{prefix}/{relative}")
                    info.size = path.stat().st_size
                    info.mode = 0o644
                    info.mtime = 946684800
                    with path.open("rb") as handle:
                        archive.addfile(info, handle)
    return output


def _archive_members(path: Path) -> list[str]:
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return [member.name for member in archive.getmembers() if member.isfile()]
    raise ReleasePackagingError(f"unsupported release artifact: {path.name}")


def validate_archive(path: Path) -> dict:
    members = _archive_members(path)
    violations = [
        f"{member}: {reason}"
        for member in members
        if (reason := forbidden_reason(member)) is not None
    ]
    if violations:
        raise ReleasePackagingError(
            f"{path.name} contains forbidden entries: " + "; ".join(violations[:20])
        )
    return {"artifact": path.name, "members": len(members), "violations": 0}


def _build_wheel(staging_root: Path, artifacts_root: Path) -> Path:
    environment = dict(os.environ)
    environment.setdefault("SOURCE_DATE_EPOCH", "946684800")
    command = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        "--no-cache-dir",
        "--wheel-dir",
        str(artifacts_root),
        str(staging_root),
    ]
    completed = subprocess.run(
        command,
        cwd=staging_root.parent,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-4000:]
        raise ReleasePackagingError(f"clean wheel build failed: {detail}")
    wheels = sorted(artifacts_root.glob(f"costweave-{__version__}-*.whl"))
    if len(wheels) != 1:
        raise ReleasePackagingError("build did not produce exactly one wheel")
    return wheels[0]


def build_release(
    source_root: Path,
    staging_root: Path,
    artifacts_root: Path,
    *,
    clean: bool = False,
) -> dict:
    source_root = source_root.resolve()
    staging_root = staging_root.resolve()
    artifacts_root = artifacts_root.resolve()
    if artifacts_root == source_root:
        raise ReleasePackagingError("artifact directory cannot be the source root")
    stage = stage_source(source_root, staging_root, clean=clean)
    _prepare_directory(artifacts_root, marker=ARTIFACT_MARKER, clean=clean)

    source_zip = create_source_zip(
        staging_root, artifacts_root / f"CostWeave-{VERSION_TAG}.zip"
    )
    source_tar = create_source_tar(
        staging_root, artifacts_root / f"costweave-{__version__}.tar.gz"
    )
    wheel = _build_wheel(staging_root, artifacts_root)
    artifacts = [source_tar, wheel, source_zip]
    validations = [validate_archive(path) for path in artifacts]

    artifact_rows = [
        {
            "name": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(artifacts, key=lambda item: item.name.lower())
    ]
    manifest = {
        "schema_version": "costweave-release-manifest-v1",
        "version": __version__,
        "version_tag": VERSION_TAG,
        "work_package": ACTIVE_WORK_PACKAGE,
        "production_ready": False,
        "evidence_level": "E0-local-verification",
        "real_provider_calls": 0,
        "paid_calls": 0,
        "clean_staging": stage,
        "archive_validation": validations,
        "artifacts": artifact_rows,
    }
    manifest_path = artifacts_root / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksummed = [*artifacts, manifest_path]
    sums_path = artifacts_root / "SHA256SUMS.txt"
    sums_path.write_text(
        "".join(
            f"{sha256_file(path)}  {path.name}\n"
            for path in sorted(checksummed, key=lambda item: item.name.lower())
        ),
        encoding="utf-8",
    )
    return {
        **manifest,
        "artifact_directory": str(artifacts_root),
        "manifest": manifest_path.name,
        "checksums": sums_path.name,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Build clean CostWeave {VERSION_TAG} release artifacts"
    )
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument(
        "--staging",
        type=Path,
        default=Path("work") / f"release-{VERSION_TAG}" / "staging",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("dist") / VERSION_TAG
    )
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        result = build_release(
            args.source,
            args.staging,
            args.output,
            clean=args.clean,
        )
        success = True
    except (OSError, ReleasePackagingError) as exc:
        result = {
            "version": __version__,
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        success = False
    else:
        result["success"] = True
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ReleasePackagingError",
    "build_release",
    "create_source_tar",
    "create_source_zip",
    "forbidden_reason",
    "sha256_file",
    "stage_source",
    "validate_archive",
    "validate_tree",
]
