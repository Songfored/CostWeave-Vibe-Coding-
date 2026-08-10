"""Offline v0.6.3 joint plan/route/schedule search demo."""

from __future__ import annotations

import argparse
import json

from .intelligence.joint_search import JointStrategySearcher, build_demo_problem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CostWeave v0.6.3 offline joint strategy search")
    parser.add_argument("--searcher", choices=("greedy", "beam", "successive_halving", "pareto"), default="beam")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    problem = build_demo_problem()
    decision, candidates = JointStrategySearcher().search(problem, searcher=args.searcher)
    payload = {
        "version": "0.6.3",
        "decision": decision.to_dict(),
        "candidate_count": len(candidates),
        "provider_calls": 0,
        "network_calls": 0,
        "commercial_claims_allowed": False,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
