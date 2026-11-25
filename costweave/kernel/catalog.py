"""Compile a goal request against reusable, host-authored production recipes."""

from collections import defaultdict
from collections.abc import Mapping

from .contracts import MAX_OPERATIONS, Job, Operation, _fields, _names, parse_evidence_rules


def compile_request(request: Mapping, catalog: Mapping) -> Job:
    _fields(request, {"goals", "inputs", "budget_units"},
            {"search_limit", "planning_policy", "evidence_rules", "verification_policy"}, "goal request")
    _fields(catalog, {"operations", "acceptance"}, set(), "recipe catalog")
    goals = _names(request["goals"], "goals")
    if not isinstance(request["inputs"], dict) or not isinstance(catalog["acceptance"], dict):
        raise ValueError("inputs and catalog acceptance must be objects")
    recipes = catalog["operations"]
    if not isinstance(recipes, list) or len(recipes) > MAX_OPERATIONS:
        raise ValueError(f"catalog requires an array of at most {MAX_OPERATIONS} operations")
    ops = [Operation.from_dict(row) for row in recipes]
    if len({op.id for op in ops}) != len(ops):
        raise ValueError("catalog operation IDs must be unique")

    evidence = parse_evidence_rules(request.get("evidence_rules", []))
    verification = request.get("verification_policy")
    requested = {rule.request for rule in evidence if rule.request is not None}
    actions = {rule.operation for rule in evidence if rule.operation is not None}
    # Compilation includes possible future inputs. Dispatch still requires their actual acceptance.
    available = set(request["inputs"]) | requested
    while True:
        generated = {op.produces for op in ops if available.issuperset(op.requires)}
        if generated <= available:
            break
        available.update(generated)
    if missing := set(goals) - available:
        raise ValueError(f"catalog cannot produce these goals from the supplied inputs: {sorted(missing)}")

    # Walk backward from requested outputs, retaining every viable alternative.
    producers = defaultdict(list)
    for op in ops:
        if available.issuperset(op.requires):
            producers[op.produces].append(op)
    pending = list(goals) + [rule.artifact for rule in evidence] + list(requested)
    if verification is not None:
        if not isinstance(verification, dict) or not isinstance(verification.get("artifact"), str):
            raise ValueError("verification_policy requires an observation artifact")
        pending.append(verification["artifact"])
    required = set(request["inputs"])
    selected = {op.id: op for op in ops if op.id in actions}
    for op in selected.values():
        if not available.issuperset(op.requires):
            raise ValueError(f"catalog cannot satisfy prerequisites for evidence operation {op.id!r}")
        pending.append(op.produces)
        pending.extend(op.requires)
    while pending:
        artifact = pending.pop()
        if artifact in required:
            continue
        required.add(artifact)
        for op in producers[artifact]:
            selected[op.id] = op
            pending.extend(op.requires)
    rules = {key: rule for key, rule in catalog["acceptance"].items() if key in required}
    return Job.from_dict({**request, "operations": [op.to_dict() for op in selected.values()], "acceptance": rules})
