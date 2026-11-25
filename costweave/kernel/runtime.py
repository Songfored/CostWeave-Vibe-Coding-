"""Drive an artifact job from verified, durable state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any

from .contracts import Job, canonical_json
from .planner import Plan, decide
from .ports import AdmissionDenied, KnownFailure, Registry, default_registry
from .store import KernelStore, wall_clock_ms


class Runtime:
    def __init__(self, store: KernelStore, registry: Registry | None = None, *, mutation_guard=nullcontext,
                 clock_ms=wall_clock_ms):
        self.store = store
        self.registry = registry if registry is not None else default_registry()
        self.mutation_guard = mutation_guard
        self.clock_ms = clock_ms

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
        return decide(job, job.inputs, (), job.budget_units)

    def inspect(self, run_id: str) -> dict:
        snapshot = self.store.snapshot(run_id)
        if (snapshot["status"] == "waiting_for_input" and "deadline_at_ms" in snapshot
                and self.clock_ms() >= snapshot["deadline_at_ms"]):
            # A read-only lookup can report expiry without mutating the ledger.
            snapshot.update(status="blocked", reason="deadline_expired")
            snapshot.pop("clarification")
        return snapshot

    def submit(self, run_id: str, job: Job) -> dict:
        self._check_contract(job)
        self._write(self.store.create, run_id, job.to_dict(), clock_ms=self.clock_ms)
        return self.resume(run_id)

    def _dispatch(self, run_id, op, inputs, attempt_id):
        try:
            result = self.registry.perform(op, inputs, attempt_id=attempt_id)
        except AdmissionDenied:
            self._write(self.store.settle, run_id, attempt_id, accepted=False, reason="admission_denied",
                        actual_units=0, stop_reason="admission_denied", require_unreturned=True, clock_ms=self.clock_ms)
        except KnownFailure as error:
            try:
                self._write(self.store.settle, run_id, attempt_id, accepted=False, reason="executor_failed",
                            actual_units=error.actual_units, require_unreturned=True, clock_ms=self.clock_ms)
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
                    reason="accepted" if passed else "validation_rejected", actual_units=attempt["actual_units"],
                    clock_ms=self.clock_ms)
        return True

    def resume(self, run_id: str) -> dict:
        snapshot = self.store.snapshot(run_id)
        job = Job.from_dict(snapshot["job"])
        self.registry.check(job)
        by_id = {op.id: op for op in job.operations}
        rules = job.acceptance
        while snapshot["status"] not in {"complete", "blocked"}:
            if snapshot["status"] == "waiting_for_input":
                if "deadline_at_ms" in snapshot and self.clock_ms() >= snapshot["deadline_at_ms"]:
                    self._write(self.store.expire_wait, run_id, clock_ms=self.clock_ms)
                    snapshot = self.store.snapshot(run_id)
                    if snapshot["status"] != "waiting_for_input":
                        continue
                break
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

                def choose(left_ms):
                    return decide(job, snapshot["artifacts"], snapshot["excluded"], remaining, remaining_ms=left_ms)

                left = None
                if "deadline_at_ms" in snapshot:
                    left = max(0, min(job.verification_policy["deadline_ms"], snapshot["deadline_at_ms"] - self.clock_ms()))
                choice = choose(left)
                expected = {"expected_excluded": snapshot["excluded"], "expected_hashes": snapshot["artifact_hashes"]}
                if choice.status == "waiting_for_input":
                    self._write(self.store.request_input, run_id, choice, **expected)
                elif choice.status != "ready":
                    status = "complete" if choice.status == "complete" else "blocked"
                    self._write(self.store.finish, run_id, status, choice.status,
                                plan=choice if choice.evidence or choice.verification else None, **expected)
                else:
                    op = by_id[choice.operations[0]]
                    attempt_id = self._write(self.store.claim, run_id, op.id, plan=choice, clock_ms=self.clock_ms,
                                             replan=choose, **expected)
                    if attempt_id is not None:
                        inputs = {key: snapshot["artifacts"][key] for key in op.requires}
                        self._dispatch(run_id, op, inputs, attempt_id)
            snapshot = self.store.snapshot(run_id)
        return snapshot

    def provide(self, run_id: str, artifact_name: str, value: Any) -> dict:
        """Validate a clarification against the frozen job, persist it, and resume."""
        if not isinstance(artifact_name, str) or not artifact_name:
            raise ValueError("supply a requested artifact name")
        snapshot = self.store.snapshot(run_id)
        job = Job.from_dict(snapshot["job"])
        self.registry.check(job)
        answer = json.loads(canonical_json(value))
        if artifact_name not in snapshot["artifacts"]:
            if snapshot.get("clarification", {}).get("artifact") != artifact_name:
                raise ValueError("this artifact was not requested")
            if "deadline_at_ms" in snapshot and self.clock_ms() >= snapshot["deadline_at_ms"]:
                return self.resume(run_id)
            rule = job.acceptance[artifact_name]
            if not self.registry.accept(artifact_name, rule, answer):
                raise ValueError(f"supplied artifact {artifact_name!r} failed its acceptance rule")
        # The store distinguishes a replay of a previous answer from an unsolicited initial/operation output.
        self._write(self.store.provide_validated, run_id, artifact_name, answer, clock_ms=self.clock_ms)
        return self.resume(run_id)

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
                        actual_units=receipt.get("actual_units"), require_unreturned=True, clock_ms=self.clock_ms)
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
        **({"deadline_at_ms": snapshot["deadline_at_ms"]} if "deadline_at_ms" in snapshot else {}),
        "active_attempt": ({key: attempt[key] for key in ("id", "op_id", "phase", "cost_units")}
                           if attempt is not None else None),
        **({"clarification": dict(snapshot["clarification"])} if "clarification" in snapshot else {}),
    }
