"""Command-line demo for CostWeave v0.6.5 safe learning."""

from __future__ import annotations

import argparse
import json

from .intelligence.safe_learning import run_demo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline safe-learning contract demo.")
    parser.add_argument("--pretty", action="store_true", help="format JSON for humans")
    args = parser.parse_args(argv)
    print(json.dumps(run_demo(), ensure_ascii=False, sort_keys=True, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
