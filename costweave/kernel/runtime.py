"""Drive an artifact job from verified, durable state."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any

from .contracts import Job
from .planner import Plan, plan
from .ports import AdmissionDenied, KnownFailure, Registry, default_registry
from .store import KernelStore


class Runtime:
    def __init__(self, store: KernelStore, registry: Registry | None = None, *, mutation_guard=nullcontext):
        self.store = store
        self.registry = registry if registry is not None else default_registry()
        self.mutation_guard = mutation_guard

    def _write(self, method, *args, **kwargs):
        with self.mutation_guard():
            return method(*args, **kwargs)

    def _check_contract(self, job: Job) -> None:
        self.registry.check(job)
        acceptance = job.acceptance
        for key, value in job.inputs.items():
            if key in acceptance and not self.registry.accept(key, acceptance[key], value):
                raise ValueError(f"initial artifact {key!r} failed its acceptance rule")

    def preview(self, job: Job) -> Plan:
        self._check_contract(job)
        return plan(job, job.inputs.keys(), (), job.budget_units)

    def submit(self, run_id: str, job: Job) -> dict:
        self._check_contract(job)
        self._write(self.store.create, run_id, job.to_dict())
        return self.resume(run_id)

    def _dispatch(self, run_id, op, inputs, attempt_id):
        try:
            result = self.registry.perform(op, inputs, attempt_id=attempt_id)
        except AdmissionDenied:
            self._write(self.store.settle, run_id, attempt_id, accepted=False, reason="admission_denied",
                        actual_units=0, stop_reason="admission_denied", require_unreturned=True)
        except KnownFailure as error:
            try:
                self._write(self.store.settle, run_id, attempt_id, accepted=False, reason="executor_failed",
                            actual_units=error.actual_units, require_unreturned=True)
            except ValueError:
                self._write(self.store.mark_uncertain, run_id, attempt_id, "invalid_execution_receipt")
        except Exception:
            self._write(self.store.mark_uncertain, run_id, attempt_id, "execution_outcome_unknown")
        else:
            try:
                self._write(self.store.record_result, run_id, attempt_id, result.value,
                            actual_units=result.actual_units, receipt_id=result.receipt_id)
            except (TypeError, ValueError):
                self._write(self.store.mark_uncertain, run_id, attempt_id, "invalid_execution_receipt")

    def _validate_return(self, run_id, attempt, op, inputs, rule):
        try:
            passed = self.registry.validate(op, attempt["value"], inputs)
            if passed and rule is not None:
                passed = self.registry.accept(op.produces, rule, attempt["value"])
        except Exception:
            # A validator fault can be retried locally without regenerating the result.
            self._write(self.store.mark_uncertain, run_id, attempt["id"], "validator_error")
            return False
        self._write(self.store.settle, run_id, attempt["id"], accepted=passed,
                    reason="accepted" if passed else "validation_rejected", actual_units=attempt["actual_units"])
        return True

    def resume(self, run_id: str) -> dict:
        snapshot = self.store.snapshot(run_id)
        job = Job.from_dict(snapshot["job"])
        self.registry.check(job)
        by_id = {op.id: op for op in job.operations}
        rules = job.acceptance
        while snapshot["status"] not in {"complete", "blocked"}:
            attempt = snapshot["active_attempt"]
            if attempt is not None:
                if attempt["phase"] != "returned":
                    break
                op = by_id[attempt["op_id"]]
                inputs = {key: snapshot["artifacts"][key] for key in op.requires}
                if not self._validate_return(run_id, attempt, op, inputs, rules.get(op.produces)):
                    return self.store.snapshot(run_id)
            else:
                remaining = job.budget_units - snapshot["spent_units"] - snapshot["held_units"]
                choice = plan(job, snapshot["artifacts"].keys(), snapshot["excluded"], remaining)
                if choice.status != "ready":
                    status = "complete" if choice.status == "complete" else "blocked"
                    self._write(self.store.finish, run_id, status, choice.status, expected_excluded=snapshot["excluded"])
                else:
                    op = by_id[choice.operations[0]]
                    attempt_id = self._write(self.store.claim, run_id, op.id,
                                             expected_excluded=snapshot["excluded"], plan=choice)
                    if attempt_id is not None:
                        inputs = {key: snapshot["artifacts"][key] for key in op.requires}
                        self._dispatch(run_id, op, inputs, attempt_id)
            snapshot = self.store.snapshot(run_id)
        return snapshot

    def reconcile(self, run_id: str, attempt_id: str, receipt: Mapping[str, Any]) -> dict:
        """Apply an operator-confirmed outcome. A returned value still faces normal validation."""
        if not isinstance(receipt, Mapping):
            raise ValueError("receipt must be a JSON object")
        outcome = receipt.get("outcome")
        required = {"outcome", "value"} if outcome == "returned" else {"outcome"}
        if not required <= receipt.keys() or receipt.keys() - (required | {"actual_units"}):
            raise ValueError("unexpected or missing reconciliation fields")
        if outcome == "returned":
            self._write(self.store.record_result, run_id, attempt_id, receipt["value"], actual_units=receipt.get("actual_units"))
        elif outcome == "confirmed_failed":
            self._write(self.store.settle, run_id, attempt_id, accepted=False, reason="confirmed_failed",
                        actual_units=receipt.get("actual_units"), require_unreturned=True)
        else:
            raise ValueError("receipt outcome must be returned or confirmed_failed")
        return self.resume(run_id)


def public_state(snapshot: Mapping[str, Any]) -> dict:
    """Only committed artifacts are presented as outputs; staged responses remain private."""
    attempt = snapshot["active_attempt"]
    return {
        "run_id": snapshot["run_id"], "status": snapshot["status"], "reason": snapshot["reason"],
        "goals": snapshot["job"]["goals"],
        "outputs": {k: snapshot["artifacts"][k] for k in snapshot["job"]["goals"] if k in snapshot["artifacts"]},
        "artifact_hashes": snapshot["artifact_hashes"], "job_sha256": snapshot["job_sha256"],
        "spent_units": snapshot["spent_units"], "held_units": snapshot["held_units"],
        "budget_units": snapshot["job"]["budget_units"],
        "active_attempt": ({key: attempt[key] for key in ("id", "op_id", "phase", "cost_units")}
                           if attempt is not None else None),
    }
