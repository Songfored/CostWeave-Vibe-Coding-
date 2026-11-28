"""Drive an artifact job from verified, durable state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

from .contracts import Job, Operation, canonical_json
from .planner import Plan, decide
from .ports import AdmissionDenied, CancellationToken, KnownFailure, Registry, default_registry
from .solver import select_parallel_wave
from .store import KernelStore, deadline_horizon, wall_clock_ms


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

    def _dispatch(
        self,
        run_id: str,
        op: Operation,
        inputs: dict,
        attempt_id: str,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        try:
            result = self.registry.perform(op, inputs, attempt_id=attempt_id, cancel_token=cancel_token)
        except AdmissionDenied:
            self._write(self.store.settle, run_id, attempt_id, accepted=False, reason="admission_denied",
                        actual_units=0, stop_reason="admission_denied", require_unreturned=True, clock_ms=self.clock_ms)
        except KnownFailure as error:
            try:
                reason = "cancelled" if cancel_token is not None and cancel_token.cancelled else "executor_failed"
                self._write(self.store.settle, run_id, attempt_id, accepted=False, reason=reason,
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

    def _validate_return(self, run_id: str, attempt: Mapping[str, Any], op: Operation, inputs: dict, rule) -> tuple[bool, list[str]]:
        try:
            passed = self.registry.validate(op, attempt["value"], inputs)
            if passed and rule is not None:
                passed = self.registry.accept(op.produces, rule, attempt["value"])
        except Exception:
            # A validator fault can be retried locally without regenerating the result.
            self._write(self.store.mark_uncertain, run_id, attempt["id"], "validator_error")
            return False, []
        superseded = self._write(
            self.store.settle,
            run_id,
            attempt["id"],
            accepted=passed,
            reason="accepted" if passed else "validation_rejected",
            actual_units=attempt["actual_units"], clock_ms=self.clock_ms,
        )
        return True, superseded

    def _drain_returned(self, run_id: str, snapshot: dict, by_id: dict[str, Operation], rules: dict) -> tuple[bool, list[str]]:
        """Validate all returned active attempts in snapshot. Returns (ok, superseded_ids)."""
        superseded_all: list[str] = []
        for attempt in snapshot["active_attempts"]:
            if attempt["phase"] != "returned":
                continue
            op = by_id[attempt["op_id"]]
            inputs = {key: snapshot["artifacts"][key] for key in op.requires}
            ok, sup = self._validate_return(run_id, attempt, op, inputs, rules.get(op.produces))
            superseded_all.extend(sup)
            if not ok:
                return False, superseded_all
        return True, superseded_all

    def _remaining_time(self, snapshot):
        deadline = snapshot.get("deadline_at_ms")
        if deadline is None:
            return None
        return max(0, min(deadline_horizon(snapshot["job"]), deadline - self.clock_ms()))

    def _choose(self, job, snapshot, remaining, left):
        artifacts = snapshot["artifacts"]
        elapsed = max(0, job.deadline_ms - left) if job.deadline_ms is not None and left is not None else 0
        return decide(job, artifacts, snapshot["excluded"], remaining,
                      remaining_ms=left, elapsed_ms=elapsed,
                      risk_remaining=max(0.0, job.max_risk_alpha - snapshot["risk_spent"] - snapshot["risk_held"]))

    def _record_choice(self, run_id, snapshot, choice):
        expected = {"expected_excluded": snapshot["excluded"], "expected_hashes": snapshot["artifact_hashes"]}
        if choice.status == "waiting_for_input":
            return self._write(self.store.request_input, run_id, choice, clock_ms=self.clock_ms, **expected)
        status = "complete" if choice.status == "complete" else "blocked"
        return self._write(self.store.finish, run_id, status, choice.status, plan=choice, **expected)

    def _resume_serial(self, run_id: str, snapshot: dict, job: Job, by_id: dict[str, Operation], rules: dict) -> dict:
        while snapshot["status"] not in {"complete", "blocked", "waiting_for_input"}:
            attempt = snapshot["active_attempt"]
            if attempt is not None:
                if attempt["phase"] != "returned":
                    break
                op = by_id[attempt["op_id"]]
                inputs = {key: snapshot["artifacts"][key] for key in op.requires}
                ok, _ = self._validate_return(run_id, attempt, op, inputs, rules.get(op.produces))
                if not ok:
                    return self.store.snapshot(run_id)
            else:
                remaining = job.budget_units - snapshot["spent_units"] - snapshot["held_units"]

                def choose(left):
                    return self._choose(job, snapshot, remaining, left)

                choice = choose(self._remaining_time(snapshot))
                if choice.status != "ready":
                    self._record_choice(run_id, snapshot, choice)
                else:
                    op = by_id[choice.operations[0]]
                    attempt_id = self._write(self.store.claim, run_id, op.id, plan=choice, clock_ms=self.clock_ms,
                                             expected_excluded=snapshot["excluded"], expected_hashes=snapshot["artifact_hashes"],
                                             replan=choose)
                    if attempt_id is not None:
                        inputs = {key: snapshot["artifacts"][key] for key in op.requires}
                        self._dispatch(run_id, op, inputs, attempt_id)
            snapshot = self.store.snapshot(run_id)
        return snapshot

    def _resume_parallel(self, run_id: str, snapshot: dict, job: Job, by_id: dict[str, Operation], rules: dict) -> dict:
        # Drain any already-returned attempts before entering concurrent wave loop.
        if any(a["phase"] == "returned" for a in snapshot["active_attempts"]):
            ok, _ = self._drain_returned(run_id, snapshot, by_id, rules)
            snapshot = self.store.snapshot(run_id)
            if not ok:
                return snapshot

        # If unreturned pre-existing attempts remain (crash recovery), wait for reconcile.
        if any(a["phase"] == "claimed" for a in snapshot["active_attempts"]):
            return snapshot

        tokens: dict[str, CancellationToken] = {}
        attempt_meta: dict[str, Operation] = {}
        futures: dict[Future, str] = {}

        with ThreadPoolExecutor(max_workers=job.max_parallel, thread_name_prefix="cw-kernel") as pool:
            while snapshot["status"] not in {"complete", "blocked", "waiting_for_input"}:
                if any(a["phase"] == "returned" for a in snapshot["active_attempts"]):
                    ok, superseded = self._drain_returned(run_id, snapshot, by_id, rules)
                    for sup_id in superseded:
                        if sup_id in tokens:
                            tokens[sup_id].cancel("superseded")
                    snapshot = self.store.snapshot(run_id)
                    if not ok:
                        for tok in tokens.values():
                            tok.cancel("validator_error")
                        break

                if snapshot["status"] in {"complete", "blocked"}:
                    break

                # If un-tracked claimed attempts exist (e.g. unknown failure requiring reconcile), abort/wait.
                untracked_claimed = [
                    a for a in snapshot["active_attempts"]
                    if a["phase"] == "claimed" and a["id"] not in attempt_meta
                ]
                if untracked_claimed:
                    for tok in tokens.values():
                        tok.cancel("needs_reconciliation")
                    break

                in_flight_ops = {op.id for op in attempt_meta.values()}
                in_flight_outputs = {op.produces for op in attempt_meta.values()}
                remaining = job.budget_units - snapshot["spent_units"] - snapshot["held_units"]
                risk_rem = max(0.0, job.max_risk_alpha - snapshot["risk_spent"] - snapshot["risk_held"])
                left = self._remaining_time(snapshot)
                elapsed_ms = (job.deadline_ms - left) if job.deadline_ms is not None and left is not None else 0

                def _launch_op(op_id: str, claim_plan: Any) -> bool:
                    nonlocal remaining
                    op = by_id[op_id]
                    if op.cost_units > remaining:
                        return False
                    attempt_id = self._write(
                        self.store.claim,
                        run_id,
                        op.id,
                        expected_excluded=snapshot["excluded"], expected_hashes=snapshot["artifact_hashes"],
                        plan=claim_plan, clock_ms=self.clock_ms,
                        replan=lambda horizon: self._choose(job, snapshot, remaining, horizon),
                    )
                    if attempt_id is None:
                        return False
                    tok = CancellationToken()
                    tokens[attempt_id] = tok
                    attempt_meta[attempt_id] = op
                    inputs = {key: snapshot["artifacts"][key] for key in op.requires}
                    fut = pool.submit(self._dispatch, run_id, op, inputs, attempt_id, tok)
                    futures[fut] = attempt_id
                    remaining -= op.cost_units
                    return True

                if len(futures) < job.max_parallel:
                    if not in_flight_ops:
                        choice = self._choose(job, snapshot, remaining, left)
                        if choice.status != "ready":
                            if not futures and not snapshot["active_attempts"]:
                                self._record_choice(run_id, snapshot, choice)
                                snapshot = self.store.snapshot(run_id)
                                continue
                        else:
                            wave = select_parallel_wave(
                                job,
                                choice.operations[0],
                                choice.operations,
                                snapshot["artifacts"],
                                snapshot["excluded"],
                                in_flight_ops,
                                in_flight_outputs,
                                remaining,
                                risk_rem,
                                elapsed_ms=elapsed_ms,
                            )
                            authorized = wave.operations if not (choice.evidence or choice.verification) else choice.operations[:1]
                            for idx, op_id in enumerate(authorized):
                                if len(futures) >= job.max_parallel:
                                    break
                                op = by_id[op_id]
                                claim_plan = (
                                    choice
                                    if idx == 0 and choice.cost_units is not None and choice.cost_units <= remaining
                                    else replace(choice, operations=(op.id,), cost_units=op.cost_units)
                                )
                                _launch_op(op_id, claim_plan)
                    elif not job.evidence_rules and not job.verification_policy and left != 0:
                        avail = set(snapshot["artifacts"].keys())
                        virtual_excluded = set(snapshot["excluded"]) | in_flight_ops
                        optimistic_avail = avail | in_flight_outputs
                        if not (set(job.goals) <= optimistic_avail):
                            opt_check = decide(
                                job,
                                snapshot["artifacts"],
                                virtual_excluded,
                                remaining,
                                anticipated=in_flight_outputs,
                                risk_remaining=risk_rem,
                                elapsed_ms=elapsed_ms,
                            )
                            if opt_check.status in {"unreachable", "risk_exceeded"}:
                                for tok in tokens.values():
                                    tok.cancel(f"goal_{opt_check.status}")
                            elif opt_check.status == "ready":
                                min_prob = min(o.pass_prob for o in attempt_meta.values())
                                min_lat = min(o.latency_p50_ms for o in attempt_meta.values())
                                for op_id in opt_check.operations:
                                    if len(futures) >= job.max_parallel:
                                        break
                                    if op_id in virtual_excluded:
                                        continue
                                    op = by_id[op_id]
                                    if (
                                        op.produces in optimistic_avail
                                        or op.guard_state(snapshot["artifacts"]) != "satisfied"
                                        or not avail.issuperset(op.requires)
                                        or op.cost_units > remaining
                                        or op.false_pass_risk > risk_rem + 1e-12
                                        or (job.deadline_ms is not None and elapsed_ms + op.latency_p50_ms > job.deadline_ms)
                                    ):
                                        continue
                                    latency_gain = job.latency_weight_units_per_sec * (min(min_lat, op.latency_p50_ms) / 1000.0)
                                    option_loss = (1.0 - min_prob) * op.effective_expected_units
                                    deadline_pressure = (
                                        job.deadline_ms is not None
                                        and elapsed_ms + min_lat + op.latency_p50_ms > job.deadline_ms
                                    )
                                    if min_prob >= 1.0 - 1e-9 or latency_gain > option_loss + 1e-9 or deadline_pressure:
                                        claim_plan = replace(opt_check, operations=(op.id,), cost_units=op.cost_units)
                                        if _launch_op(op_id, claim_plan):
                                            in_flight_ops.add(op_id)
                                            virtual_excluded.add(op_id)
                                            in_flight_outputs.add(op.produces)
                                            optimistic_avail.add(op.produces)

                if not futures:
                    # A stale CAS must replan. A different worker's unresolved reservation
                    # will be surfaced on the next iteration as needs_reconciliation.
                    snapshot = self.store.snapshot(run_id)
                    continue

                timeout = None if left is None else left / 1000
                done, _ = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
                if not done:
                    for token in tokens.values():
                        token.cancel("deadline_expired")
                    # The reservation belongs to this attempt until its executor
                    # returns a receipt. A timeout alone does not establish its charge.
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for fut in done:
                    att_id = futures.pop(fut)
                    tokens.pop(att_id, None)
                    attempt_meta.pop(att_id, None)
                    fut.result()

                snapshot = self.store.snapshot(run_id)
                if any(a["phase"] == "returned" for a in snapshot["active_attempts"]):
                    ok, superseded = self._drain_returned(run_id, snapshot, by_id, rules)
                    for sup_id in superseded:
                        if sup_id in tokens:
                            tokens[sup_id].cancel("superseded")
                    snapshot = self.store.snapshot(run_id)
                    if not ok:
                        for tok in tokens.values():
                            tok.cancel("validator_error")
                        break

                # If all goals are now produced and accepted, cancel any superfluous remaining work.
                if set(job.goals) <= snapshot["artifacts"].keys():
                    for tok in tokens.values():
                        tok.cancel("goals_completed")

            # Drain remaining futures cleanly before returning.
            if futures:
                wait(list(futures.keys()))
                for fut in list(futures.keys()):
                    att_id = futures.pop(fut)
                    tokens.pop(att_id, None)
                    attempt_meta.pop(att_id, None)
                    fut.result()
                snapshot = self.store.snapshot(run_id)
                if any(a["phase"] == "returned" for a in snapshot["active_attempts"]):
                    self._drain_returned(run_id, snapshot, by_id, rules)
                    snapshot = self.store.snapshot(run_id)
        return snapshot

    def resume(self, run_id: str) -> dict:
        snapshot = self.store.snapshot(run_id)
        job = Job.from_dict(snapshot["job"])
        self.registry.check(job)
        by_id = {op.id: op for op in job.operations}
        rules = job.acceptance
        if snapshot["status"] == "waiting_for_input" and self._remaining_time(snapshot) == 0:
            self._write(self.store.expire_wait, run_id, clock_ms=self.clock_ms)
            snapshot = self.store.snapshot(run_id)
        if job.max_parallel <= 1:
            return self._resume_serial(run_id, snapshot, job, by_id, rules)
        return self._resume_parallel(run_id, snapshot, job, by_id, rules)

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
        "run_id": snapshot["run_id"],
        "status": snapshot["status"],
        "reason": snapshot["reason"],
        "goals": snapshot["job"]["goals"],
        "outputs": {k: snapshot["artifacts"][k] for k in snapshot["job"]["goals"] if k in snapshot["artifacts"]},
        "artifact_hashes": snapshot["artifact_hashes"],
        "job_sha256": snapshot["job_sha256"],
        "spent_units": snapshot["spent_units"],
        "held_units": snapshot["held_units"],
        "budget_units": snapshot["job"]["budget_units"],
        "risk_spent": snapshot.get("risk_spent", 0.0),
        "risk_held": snapshot.get("risk_held", 0.0),
        "active_attempt": ({key: attempt[key] for key in ("id", "op_id", "phase", "cost_units")}
                           if attempt is not None else None),
        "active_attempts": [
            {key: item[key] for key in ("id", "op_id", "phase", "cost_units", "superseded")}
            for item in snapshot.get("active_attempts", ())
        ],
        **({"deadline_at_ms": snapshot["deadline_at_ms"]} if "deadline_at_ms" in snapshot else {}),
        **({"clarification": dict(snapshot["clarification"])} if "clarification" in snapshot else {}),
    }
