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
    args = parser.parse_args()
    total = 0
    failures = 0
    started = time.monotonic()
    for module in modules():
        module_started = time.monotonic()
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "unittest",
                    module,
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1.0, args.timeout),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            failures += 1
            print(
                f"TIMEOUT {module} after {args.timeout:.1f}s",
                flush=True,
            )
            if exc.stdout:
                print(exc.stdout)
            if exc.stderr:
                print(exc.stderr)
            continue
        elapsed = time.monotonic() - module_started
        combined = (completed.stdout or "") + (completed.stderr or "")
        match = RESULT_PATTERN.search(combined)
        count = int(match.group(1)) if match else 0
        total += count
        if completed.returncode == 0:
            print(
                f"PASS {module} ({count} tests, {elapsed:.2f}s)",
                flush=True,
            )
        else:
            failures += 1
            print(
                f"FAIL {module} (exit {completed.returncode})",
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
