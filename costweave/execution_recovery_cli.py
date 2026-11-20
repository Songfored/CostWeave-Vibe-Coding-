"""Offline v0.6.4 execution-time belief and economic recovery demo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .intelligence.execution_recovery import EconomicRecoveryPlanner, ExecutionSnapshotV1, run_demo


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="CostWeave v0.6.4 offline execution recovery")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    parser.add_argument("--input", type=Path, help="read an ExecutionSnapshotV1 JSON file in place of the demo")
    args = parser.parse_args(argv)
    try:
        if args.input is None:
            decision = run_demo()
        else:
            snapshot = ExecutionSnapshotV1.from_dict(json.loads(args.input.read_text(encoding="utf-8-sig")))
            decision, _ = EconomicRecoveryPlanner().decide(snapshot)
    except (OSError, TypeError, ValueError, KeyError) as error:
        parser.error(str(error))
    payload = {
        "version": "0.6.4",
        "schema_version": "v0.6.4",
        "decision": decision.to_dict(),
        "action": decision.action,
        "recovery_advantage_usd": decision.recovery_advantage_usd,
        "provider_calls": 0,
        "network_calls": 0,
        "commercial_claims_allowed": False,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))


if __name__ == "__main__":
    main()
