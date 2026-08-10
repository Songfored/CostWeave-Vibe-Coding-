"""Offline v0.6.4 execution-time belief and economic recovery demo."""

from __future__ import annotations

import argparse
import json

from .intelligence.execution_recovery import run_demo


def main() -> None:
    parser = argparse.ArgumentParser(description="CostWeave v0.6.4 offline execution recovery")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    args = parser.parse_args()
    decision = run_demo()
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
