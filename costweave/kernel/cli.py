"""Command-line interface for artifact planning, execution, inspection and recovery."""

import argparse
import json
import os
import sys
from importlib.metadata import entry_points
from pathlib import Path
from uuid import uuid4

from .beliefs import BeliefEngine, BeliefStore
from .contracts import Job
from .catalog import compile_request
from .compiler import ModelSpec, TaskSpec, compile_task_hypergraph
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
        command.add_argument("--compile-models", help="compile a TaskSpec JSON against a ModelSpec list JSON")
    run.add_argument("--id", help="stable ID for idempotent submission")
    for name in ("resume", "show", "events"):
        commands.add_parser(name).add_argument("run_id")
    reconcile = commands.add_parser("reconcile", help="record a confirmed outcome and resume")
    reconcile.add_argument("run_id")
    reconcile.add_argument("attempt_id")
    reconcile.add_argument("receipt")
    beliefs_cmd = commands.add_parser("beliefs", help="inspect, promote, or rollback learned routing beliefs")
    beliefs_sub = beliefs_cmd.add_subparsers(dest="belief_action", required=True)
    beliefs_sub.add_parser("status")
    beliefs_promote = beliefs_sub.add_parser("promote")
    beliefs_promote.add_argument("candidate")
    beliefs_promote.add_argument("observations")
    beliefs_sub.add_parser("rollback")
    args = parser.parse_args(argv)
    if args.command == "serve":
        from ..server import main as serve
        serve(["--host", args.host, "--port", str(args.port)])
        return 0
    if args.command == "beliefs":
        bstore = BeliefStore(args.db)
        try:
            if args.belief_action == "status":
                output = bstore.active_version()
            elif args.belief_action == "promote":
                cand = BeliefEngine.from_dict(_read(args.candidate))
                obs = _read(args.observations)
                output = bstore.evaluate_and_promote(cand, obs)
            else:
                output = bstore.rollback()
            print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        except (OSError, ValueError, TypeError, KeyError) as error:
            print(str(error), file=sys.stderr)
            return 1
        finally:
            bstore.close()
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
                if args.compile_models:
                    models_raw = _read(args.compile_models)
                    if isinstance(models_raw, dict) and "models" in models_raw:
                        models_raw = models_raw["models"]
                    task_raw = data["task"] if isinstance(data, dict) and "task" in data else data
                    models_seq = [ModelSpec.from_dict(m) for m in models_raw]
                    job = compile_task_hypergraph(TaskSpec.from_dict(task_raw), models_seq)
                elif args.catalog:
                    job = compile_request(data, _read(args.catalog))
                else:
                    job = Job.from_dict(data)
                if args.command == "plan":
                    output = runtime.preview(job).to_dict()
                else:
                    output = public_state(runtime.submit(args.id or uuid4().hex, job))
            elif args.command == "resume":
                output = public_state(runtime.resume(args.run_id))
            else:
                output = public_state(runtime.reconcile(args.run_id, args.attempt_id, _read(args.receipt)))
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
        state = output.get("status") if isinstance(output, dict) else None
        if state == "needs_reconciliation":
            return 3
        return 2 if state in {"blocked", "unreachable", "over_budget", "search_limit"} else 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        store.close()
