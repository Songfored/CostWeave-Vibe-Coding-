"""Offline v0.6.3 joint plan/route/schedule search demo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .intelligence.joint_search import JointSearchProblemV1, JointStrategySearcher, build_demo_problem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CostWeave v0.6.3 offline joint strategy search")
    parser.add_argument("--searcher", choices=("greedy", "beam", "successive_halving", "pareto"), default="beam")
    parser.add_argument("--input", type=Path, help="read a JointSearchProblemV1 JSON file in place of the demo")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        problem = build_demo_problem() if args.input is None else JointSearchProblemV1.from_dict(
            json.loads(args.input.read_text(encoding="utf-8-sig"))
        )
        decision, candidates = JointStrategySearcher().search(problem, searcher=args.searcher)
    except (OSError, TypeError, ValueError, KeyError) as error:
        parser.error(str(error))
    payload = {
        "version": "0.6.3",
        "decision": decision.to_dict(),
        "candidate_count": len(candidates),
        "provider_calls": 0,
        "network_calls": 0,
        "commercial_claims_allowed": False,
    }
    if args.input is not None:
        payload["candidates"] = [candidate.to_dict() for candidate in candidates]
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
