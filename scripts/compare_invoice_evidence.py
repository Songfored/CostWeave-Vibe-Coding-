"""Compare three invoice policies locally: python -m scripts.compare_invoice_evidence."""

import json
from pathlib import Path

from costweave.kernel.contracts import Job, Operation
from costweave.kernel.ports import default_registry
from costweave.kernel.runtime import Runtime
from costweave.kernel.store import KernelStore


CASES = Path(__file__).resolve().parents[1] / "examples/evidence-invoice/comparison-cases.json"
ARMS = ("fixed_original", "lowest_cost", "evidence")
ACTION_MS = {"compare-statement": 10, "post-summary": 5, "open-archive": 45, "sum-lines": 10}
OPERATIONS = (
    Operation("compare-statement", ["invoice"], "check", "compare_statement", "present", 1),
    Operation("post-summary", ["invoice"], "receipt", "post_summary", "present", 1),
    Operation("open-archive", ["invoice"], "lines", "pick", "present", 3,
              {"source": "invoice", "path": ["original"]}),
    Operation("sum-lines", ["lines"], "receipt", "sum_lines", "present", 1),
)
ACCEPTANCE = {
    "invoice": {"validator": "invoice_header", "config": {}},
    "lines": {"validator": "original_lines", "config": {}},
    "check": {"validator": "statement_check", "config": {}},
    "receipt": {"validator": "receipt", "config": {}},
}


def original_lines(amounts):
    return {"schema": "invoice-lines.v1", "amounts_cents": amounts}


def valid_lines(value):
    return (isinstance(value, dict) and value.get("schema") == "invoice-lines.v1"
            and isinstance(value.get("amounts_cents"), list) and bool(value["amounts_cents"])
            and all(type(amount) is int and amount >= 0 for amount in value["amounts_cents"]))


def valid_header(value):
    return (isinstance(value, dict) and value.get("kind") == "invoice"
            and type(value.get("summary_cents")) is int and type(value.get("statement_cents")) is int
            and all(isinstance(value.get(field), str) and value[field] for field in ("summary_origin", "statement_origin"))
            and value.get("has_archive") is ("original" in value)
            and ("original" not in value or valid_lines(value["original"])))


def compare_statement(op, inputs, *, attempt_id):
    invoice = inputs["invoice"]
    if invoice["summary_origin"] == invoice["statement_origin"]:
        finding = "same_source"
    elif invoice["summary_cents"] == invoice["statement_cents"]:
        finding = "agree"
    else:
        finding = "disagree"
    return {"finding": finding, "summary_origin": invoice["summary_origin"],
            "statement_origin": invoice["statement_origin"]}


def make_registry():
    registry = default_registry()
    registry.register_executor("compare_statement", compare_statement)
    registry.register_executor("post_summary", lambda op, inputs, *, attempt_id:
                               {"total_cents": inputs["invoice"]["summary_cents"]})
    registry.register_executor("sum_lines", lambda op, inputs, *, attempt_id:
                               {"total_cents": sum(inputs["lines"]["amounts_cents"])})
    registry.register_validator("original_lines", lambda op, value, inputs: valid_lines(value))
    registry.register_validator("invoice_header", lambda op, value, inputs: valid_header(value))
    registry.register_validator("statement_check", lambda op, value, inputs: isinstance(value, dict)
                                and value.get("finding") in ("agree", "disagree", "same_source"))
    registry.register_validator("receipt", lambda op, value, inputs: isinstance(value, dict)
                                and type(value.get("total_cents")) is int and value["total_cents"] >= 0)
    return registry


def make_job(case, arm):
    # Only operational inputs enter the job. The audit and the reply stay with the evaluator.
    invoice = {"kind": "invoice", "summary_cents": case["summary_cents"], "statement_cents": case["statement_cents"],
               "summary_origin": "invoice_ocr", "statement_origin": case["statement_origin"],
               "has_archive": "archive" in case}
    if "archive" in case:
        invoice["original"] = original_lines(case["archive"])
    original_route = [
        {"artifact": "invoice", "field": "has_archive", "equals": True, "operation": "open-archive"},
        {"artifact": "invoice", "field": "has_archive", "equals": False,
         "request": "lines", "reason": "Supply the original invoice line amounts in cents."},
        {"artifact": "lines", "field": "schema", "equals": "invoice-lines.v1", "operation": "sum-lines"},
    ]
    rules = {
        "lowest_cost": [],
        "fixed_original": original_route,
        "evidence": [
            {"artifact": "check", "field": "finding", "equals": "agree", "acquire": True,
             "operation": "post-summary"},
            *original_route,
        ],
    }
    return Job(["receipt"], {"invoice": invoice}, case["budget"], OPERATIONS,
               acceptance=ACCEPTANCE, evidence_rules=rules[arm])


def run_case(runtime, case, arm):
    run_id = f"{case['id']}-{arm}"
    state = runtime.submit(run_id, make_job(case, arm))
    requested = state["status"] == "waiting_for_input"
    wait_ms = 0
    if requested and "reply" in case:
        wait_ms = case["reply_ms"]
        state = runtime.provide(run_id, "lines", original_lines(case["reply"]))
    attempts = [event["op_id"] for event in runtime.store.events(run_id) if event["type"] == "attempt_claimed"]
    elapsed = None if state["status"] == "waiting_for_input" else sum(ACTION_MS[op] for op in attempts) + wait_ms
    delivered = state["status"] == "complete"
    issued = state["artifacts"]["receipt"]["total_cents"] if delivered else None
    correct = delivered and issued == case["audited_cents"]
    return {"status": state["status"], "attempts": attempts, "finding": state["artifacts"].get("check", {}).get("finding"),
            "issued_cents": issued, "spent_units": state["spent_units"], "clarification_requested": requested,
            "elapsed_ms": elapsed, "false_delivery": delivered and not correct, "correct_delivery": correct,
            "on_time_correct": correct and elapsed <= case["deadline_ms"]}


def summarize(cases, arm):
    runs = [case["arms"][arm] for case in cases]
    total = sum(run["spent_units"] for run in runs)
    on_time = sum(run["on_time_correct"] for run in runs)
    return {"runs": len(runs), "spent_units": total, "correct_deliveries": sum(run["correct_delivery"] for run in runs),
            "false_deliveries": sum(run["false_delivery"] for run in runs), "on_time_correct": on_time,
            "clarifications": sum(run["clarification_requested"] for run in runs),
            "unresolved": sum(run["status"] == "waiting_for_input" for run in runs),
            "spent_per_on_time_correct": total / on_time if on_time else None}


def compare():
    cases = []
    store = KernelStore(":memory:")
    try:
        runtime = Runtime(store, make_registry())
        for case in json.loads(CASES.read_text(encoding="utf-8")):
            cases.append({"case": case["id"], "budget_units": case["budget"], "deadline_ms": case["deadline_ms"],
                          "audited_cents": case["audited_cents"],
                          "arms": {arm: run_case(runtime, case, arm) for arm in ARMS}})
    finally:
        store.close()
    return {
        "evidence": "Seven authored regression cases. The audited totals are withheld from every policy and executor.",
        "cost": "Integer test units for all attempted operations. Clarification has modeled delay and no fee.",
        "timing": "Deadlines score completed runs using modeled serial action times and reply delay. They do not steer decisions. Unanswered requests have no observed end time.",
        "provider_calls": 0, "action_ms": ACTION_MS, "summary": {arm: summarize(cases, arm) for arm in ARMS},
        "cases": cases,
    }


if __name__ == "__main__":
    print(json.dumps(compare(), indent=2))
