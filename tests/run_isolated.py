"""Run every unittest module in a fresh process.

Some integration tests intentionally create background orchestration threads.
Process isolation prevents one module's timing or global state from making a
later module flaky while preserving normal unittest exit codes.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import threading
import time
from pathlib import Path


RESULT_PATTERN = re.compile(r"Ran (\d+) tests?")


def modules() -> list[str]:
    root = Path(__file__).resolve().parent
    return [
        f"tests.{path.stem}"
        for path in sorted(root.glob("test_*.py"))
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run CostWeave unittest modules in isolated processes"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="maximum seconds for one test module",
    )
    parser.add_argument(
        "--module",
        action="append",
        help="run only this unittest module; may be repeated",
    )
    args = parser.parse_args()
    total = 0
    failures = 0
    started = time.monotonic()
    selected_modules = args.module or modules()
    for module in selected_modules:
        module_started = time.monotonic()
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "unittest",
                module,
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def drain(stream, target):
            if stream is not None:
                for chunk in iter(stream.readline, ""):
                    target.append(chunk)

        readers = (
            threading.Thread(
                target=drain,
                args=(process.stdout, stdout_parts),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, stderr_parts),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=max(1.0, args.timeout))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            for reader in readers:
                reader.join(timeout=1)
            failures += 1
            print(
                f"TIMEOUT {module} after {args.timeout:.1f}s",
                flush=True,
            )
            combined = "".join(stdout_parts + stderr_parts)
            if combined:
                print(combined)
            continue
        for reader in readers:
            reader.join(timeout=1)
        combined = "".join(stdout_parts + stderr_parts)
        elapsed = time.monotonic() - module_started
        match = RESULT_PATTERN.search(combined)
        count = int(match.group(1)) if match else 0
        total += count
        if returncode == 0:
            print(
                f"PASS {module} ({count} tests, {elapsed:.2f}s)",
                flush=True,
            )
        else:
            failures += 1
            print(
                f"FAIL {module} (exit {returncode})",
                flush=True,
            )
            print(combined)
    elapsed = time.monotonic() - started
    print(
        f"TOTAL {total} tests; failures={failures}; "
        f"elapsed={elapsed:.2f}s",
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
