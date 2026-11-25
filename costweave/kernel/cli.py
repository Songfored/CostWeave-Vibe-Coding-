"""Command-line interface for artifact planning, execution, inspection and recovery."""

import argparse
import json
import os
import sys
from importlib.metadata import entry_points
from pathlib import Path
from uuid import uuid4

from .contracts import Job
from .catalog import compile_request
from .ports import Registry, default_registry
from .runtime import Runtime, public_state
from .store import KernelStore


def _registry(name):
    if not name:
        return default_registry()
    matches = list(entry_points(group="costweave.registries", name=name))
    if len(matches) != 1:
        raise ValueError(f"expected one installed CostWeave registry named {name!r}")
    registry = matches[0].load()()
    if not isinstance(registry, Registry):
        raise ValueError("registry entry point must return a Registry")
    return registry


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith(("--host", "--port")):
        from ..server import main as serve
        serve(argv)
        return 0
    parser = argparse.ArgumentParser(prog="costweave", description="Plan and execute verifiable artifact jobs")
    parser.add_argument("--db", default=os.environ.get("COSTWEAVE_ARTIFACT_DB") or
                        str(Path.home() / ".costweave" / "artifact-runtime.sqlite3"), help="SQLite run ledger")
    parser.add_argument("--registry", help="host-installed costweave.registries entry point")
    commands = parser.add_subparsers(dest="command", required=True)
    server_command = commands.add_parser("serve", help="start the local web and HTTP service")
    server_command.add_argument("--host", default="127.0.0.1")
    server_command.add_argument("--port", type=int, default=8765)
    preview = commands.add_parser("plan", help="preview a verified job without executing it")
    run = commands.add_parser("run", help="submit and drive a job")
    for command in (preview, run):
        command.add_argument("job")
        command.add_argument("--catalog", help="compile this goal request against a JSON recipe catalog")
    run.add_argument("--id", help="stable ID for idempotent submission")
    for name in ("resume", "show", "events"):
        commands.add_parser(name).add_argument("run_id")
    reconcile = commands.add_parser("reconcile", help="record a confirmed outcome and resume")
    reconcile.add_argument("run_id")
    reconcile.add_argument("attempt_id")
    reconcile.add_argument("receipt")
    provide = commands.add_parser("provide", help="validate a requested input and continue the waiting run")
    provide.add_argument("run_id")
    provide.add_argument("artifact", help="requested artifact name")
    provide.add_argument("value_file", help="JSON file containing the requested value")
    args = parser.parse_args(argv)
    if args.command == "serve":
        from ..server import main as serve
        serve(["--host", args.host, "--port", str(args.port)])
        return 0
    store = KernelStore(":memory:" if args.command == "plan" else args.db)
    try:
        if args.command == "events":
            output = store.events(args.run_id)
        elif args.command == "show":
            output = public_state(store.snapshot(args.run_id))
        else:
            runtime = Runtime(store, _registry(args.registry))
            if args.command in {"plan", "run"}:
                data = _read(args.job)
                job = compile_request(data, _read(args.catalog)) if args.catalog else Job.from_dict(data)
                if args.command == "plan":
                    output = runtime.preview(job).to_dict()
                else:
                    output = public_state(runtime.submit(args.id or uuid4().hex, job))
            elif args.command == "resume":
                output = public_state(runtime.resume(args.run_id))
            elif args.command == "provide":
                output = public_state(runtime.provide(args.run_id, args.artifact, _read(args.value_file)))
            else:
                output = public_state(runtime.reconcile(args.run_id, args.attempt_id, _read(args.receipt)))
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
        state = output.get("status") if isinstance(output, dict) else None
        if state == "needs_reconciliation":
            return 3
        if state == "waiting_for_input":
            return 4
        if state in {"blocked", "unreachable", "over_budget", "search_limit", "unavailable",
                     "evidence_action_rejected", "evidence_prerequisite_missing", "deadline_expired",
                     "deadline_unreachable", "quality_threshold_not_met", "insufficient_evidence",
                     "no_eligible_verification_route", "verification_route_failed"}:
            return 2
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        store.close()
