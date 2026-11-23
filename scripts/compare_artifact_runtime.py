"""Run from the checkout with python -m scripts.compare_artifact_runtime."""

import hashlib
import json
import tempfile
from pathlib import Path

from costweave.intelligence.common import NonNegativeEstimate
from costweave.intelligence.joint_search import (
    JointRouteOptionV1, JointRouteSetV1, JointSearchBudgetV1, JointSearchProblemV1,
    JointStrategySearcher, JointTaskNodeV1,
)
from costweave.kernel.contracts import Job, Operation, canonical_json
from costweave.kernel.ports import default_registry
from costweave.kernel.runtime import Runtime
from costweave.kernel.store import KernelStore


TOPOLOGIES = {"direct": ("read-summary", "calculate-inline"),
              "decomposed": ("normalize", "count", "sum", "assemble")}
SCENARIOS = (
    # name, summary total, budget, prevalidated artifacts
    ("valid_summary", 22, 6, {}),
    ("stale_summary", 21, 6, {}),
    ("insufficient_budget", 21, 3, {}),
    ("accepted_shared_input", 21, 5, {"amounts": [12, 10]}),
    ("inline_fallback_fits", 21, 7, {}),
    ("accepted_goal", 21, 0, {"receipt": {"count": 2, "total": 22}}),
)


def make_job(summary_total, budget, prevalidated):
    source = {"lines": [{"quantity": 3, "unit_price": 4}, {"quantity": 2, "unit_price": 5}],
              "summary": {"count": 2, "total": summary_total}}
    ops = [
        Operation("read-summary", ["source"], "receipt", "pick", "json_object", 1,
                  {"source": "source", "path": ["summary"]}),
        Operation("calculate-inline", ["source"], "receipt", "inline", "json_object", 6),
        Operation("normalize", ["source"], "amounts", "line_amounts", "present", 2),
        Operation("count", ["amounts"], "count", "line_count", "present", 1),
        Operation("sum", ["amounts"], "total", "line_sum", "present", 1),
        Operation("assemble", ["count", "total"], "receipt", "collect", "json_object", 1),
    ]
    # The terminal oracle belongs to the fixture. No executor reads it.
    return Job(["receipt"], {"source": source, **prevalidated}, budget, ops, acceptance={
        "receipt": {"validator": "equals", "config": {"expected": {"count": 2, "total": 22}}},
        "amounts": {"validator": "equals", "config": {"expected": [12, 10]}},
    })


def make_registry():
    registry = default_registry()

    def amounts(source):
        return [line["quantity"] * line["unit_price"] for line in source["lines"]]

    def inline(op, inputs, *, attempt_id):
        values = amounts(inputs["source"])
        return {"count": len(values), "total": sum(values)}

    registry.register_executor("inline", inline)
    registry.register_executor("line_amounts", lambda op, inputs, *, attempt_id: amounts(inputs["source"]))
    registry.register_executor("line_count", lambda op, inputs, *, attempt_id: len(inputs["amounts"]))
    registry.register_executor("line_sum", lambda op, inputs, *, attempt_id: sum(inputs["amounts"]))
    return registry


def fixed_decision(job, operators, artifacts, excluded, remaining_units):
    """Compile one fixed topology to the existing v1 search contract."""
    groups = {}
    for op in operators:
        if op.id not in excluded and op.produces not in artifacts:
            groups.setdefault(op.produces, []).append(op)
    nodes, routes = [], []
    required = set(job.goals) - artifacts.keys()
    seen = set()
    while required:
        name = min(required)
        required.remove(name)
        if name in seen:
            continue
        seen.add(name)
        if name not in groups:
            return None
        options = groups[name]
        # The v1 input fixes dependencies before choosing a route.
        prerequisites = tuple(dep for dep in options[0].requires if dep not in artifacts)
        if any(op.requires != options[0].requires for op in options):
            raise ValueError("inconsistent dependencies in fixed topology")
        required.update(prerequisites)
        objective = hashlib.sha256(name.encode()).hexdigest()
        nodes.append(JointTaskNodeV1(name, objective, dependencies=prerequisites))
        routes.append(JointRouteSetV1(name, tuple(
            JointRouteOptionV1(op.id, op.executor, "local", op.validator,
                               predicted_cost_usd=NonNegativeEstimate(op.cost_units, op.cost_units))
            for op in options
        )))
    single = len(nodes) == 1
    # The legacy field is USD. Here its numeric scale encodes test units only;
    # neutralize its built-in planner fee so operation budgets match exactly.
    overhead = 0 if single else 0.002
    problem = JointSearchProblemV1(
        project_id="local-comparison", case_id="receipt", task_family="data",
        nodes=tuple(nodes), route_sets=tuple(routes),
        budget=JointSearchBudgetV1(money_usd=remaining_units + overhead),
        management_modes=("no_delegation" if single else "planner",), recovery_policies=("retry_node",),
        min_success_lower=0, failure_loss_usd=0, latency_weight_usd_per_second=0,
    )
    decision, _ = JointStrategySearcher().search(problem)
    return decision, problem.topological_order()


def final_passed(registry, job, artifacts):
    rules = job.acceptance
    return all(goal in artifacts and registry.accept(goal, rules[goal], artifacts[goal]) for goal in job.goals)


def fixed_run(job, registry, topology):
    # v1 search is advisory. This harness executes its selected routes and gives
    # it a fresh search on the same topology after rejection, retaining accepted work.
    catalog = {op.id: op for op in job.operations}
    operations = [catalog[key] for key in TOPOLOGIES[topology]]
    artifacts = job.inputs
    rules = job.acceptance
    registry.check(job)
    if any(not registry.accept(key, rules[key], val) for key, val in artifacts.items() if key in rules):
        raise ValueError("invalid initial fixture artifact")
    attempts, decisions = [], []
    excluded = set()
    spent = 0
    while not final_passed(registry, job, artifacts):
        found = fixed_decision(job, operations, artifacts, excluded, job.budget_units - spent)
        if found is None:
            break
        decision, order = found
        decisions.append(decision.action)
        if decision.selected_candidate is None:
            break
        selected = dict(decision.selected_candidate.node_routes)
        for name in order:
            op = catalog[selected[name]]
            if spent + op.cost_units > job.budget_units:
                raise AssertionError("selected routes exceed the shared fixture budget")
            inputs = {key: artifacts[key] for key in op.requires}
            value = registry.execute(op, inputs, attempt_id=f"local-{len(attempts)}")
            spent += op.cost_units
            attempts.append(op.id)
            if not registry.validate(op, value, inputs) or (name in rules and not registry.accept(name, rules[name], value)):
                excluded.add(op.id)
                break
            artifacts[name] = value
    return {"final_passed": final_passed(registry, job, artifacts), "spent_units": spent,
            "attempts": attempts, "search_decisions": decisions}


def compare():
    registry = make_registry()
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        store = KernelStore(Path(directory) / "comparison.sqlite3")
        runtime = Runtime(store, registry)
        for name, summary_total, budget, prevalidated in SCENARIOS:
            job = make_job(summary_total, budget, prevalidated)
            state = runtime.submit(name, job)
            attempts = [event["op_id"] for event in store.events(name) if event["type"] == "attempt_claimed"]
            rows.append({
                "case": name, "job_sha256": hashlib.sha256(canonical_json(job.to_dict()).encode()).hexdigest(),
                "budget_units": budget, **{key: fixed_run(job, registry, key) for key in TOPOLOGIES},
                "kernel": {"final_passed": final_passed(registry, job, state["artifacts"]),
                           "spent_units": state["spent_units"], "attempts": attempts, "reason": state["reason"]},
            })
    return {
        "evidence": "Deterministic local invoice fixtures with integer operation quotes.",
        "provider_calls": 0,
        "comparison": "Two fixed v1 topologies, each reconsidering its routes after rejection, versus the artifact runtime.",
        "summary": {
            arm: {"final_passes": sum(row[arm]["final_passed"] for row in rows),
                  "spent_units": sum(row[arm]["spent_units"] for row in rows)}
            for arm in (*TOPOLOGIES, "kernel")
        },
        "cases": rows,
    }


if __name__ == "__main__":
    print(json.dumps(compare(), indent=2, ensure_ascii=False))
