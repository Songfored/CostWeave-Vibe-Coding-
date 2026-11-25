"""Durable attempts, immutable validated artifacts and a bounded execution ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .contracts import Job, canonical_json


_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS kernel_runs (
    run_id TEXT PRIMARY KEY,
    job TEXT NOT NULL,
    job_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','complete','blocked')),
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kernel_blobs (
    sha256 TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kernel_artifacts (
    run_id TEXT NOT NULL REFERENCES kernel_runs,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL REFERENCES kernel_blobs,
    PRIMARY KEY(run_id, name)
);
CREATE TABLE IF NOT EXISTS kernel_attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES kernel_runs,
    op_id TEXT NOT NULL,
    cost_units INTEGER NOT NULL CHECK(cost_units >= 0),
    actual_units INTEGER CHECK(actual_units BETWEEN 0 AND cost_units),
    accepted INTEGER CHECK(accepted IN (0,1)),
    result_sha256 TEXT,
    returned_json TEXT,
    returned_units INTEGER CHECK(returned_units BETWEEN 0 AND cost_units),
    receipt_id TEXT,
    stop_reason TEXT,
    reason TEXT NOT NULL DEFAULT '',
    uncertain_reason TEXT,
    UNIQUE(run_id, op_id),
    CHECK ((actual_units IS NULL) = (accepted IS NULL)),
    CHECK ((returned_json IS NULL) = (returned_units IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS kernel_one_active_attempt
    ON kernel_attempts(run_id) WHERE accepted IS NULL;
CREATE TABLE IF NOT EXISTS kernel_events (
    seq INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES kernel_runs,
    type TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS kernel_events_order ON kernel_events(run_id, seq);
CREATE TABLE IF NOT EXISTS kernel_clarifications (
    run_id TEXT NOT NULL REFERENCES kernel_runs,
    artifact TEXT NOT NULL,
    reason TEXT NOT NULL,
    supplied_sha256 TEXT REFERENCES kernel_blobs,
    PRIMARY KEY(run_id, artifact)
);
CREATE UNIQUE INDEX IF NOT EXISTS kernel_one_pending_clarification
    ON kernel_clarifications(run_id) WHERE supplied_sha256 IS NULL;
COMMIT;
"""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(encoded):
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_NOT_SUPPLIED = object()


class RunConflictError(ValueError):
    """An immutable job or supplied input conflicts with an earlier submission."""


def _charge(attempt, actual):
    charge = attempt["cost_units"] if actual is None else actual
    if type(charge) is not int or not 0 <= charge <= attempt["cost_units"]:
        raise ValueError("actual units exceed or invalidate the reservation")
    return charge


def _event(db, run_id, kind, **metadata):
    db.execute("INSERT INTO kernel_events(run_id,type,data) VALUES (?,?,?)",
               (run_id, kind, _canonical(metadata)))


def _artifact(db, run_id, name, encoded, digest):
    db.execute("INSERT OR IGNORE INTO kernel_blobs(sha256,json) VALUES (?,?)", (digest, encoded))
    db.execute("INSERT INTO kernel_artifacts(run_id,name,sha256) VALUES (?,?,?)", (run_id, name, digest))


def _run(db, run_id):
    row = db.execute("SELECT * FROM kernel_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return row


def _attempt(db, run_id, attempt_id):
    row = db.execute("SELECT * FROM kernel_attempts WHERE run_id=? AND id=?", (run_id, attempt_id)).fetchone()
    if row is None:
        raise KeyError(attempt_id)
    return row


def _amount(db, run_id):
    return db.execute("SELECT COALESCE(SUM(actual_units),0) FROM kernel_attempts WHERE run_id=?",
                      (run_id,)).fetchone()[0]


def _operation(job, op_id):
    for operation in job["operations"]:
        if operation["id"] == op_id:
            return operation
    raise ValueError(f"unregistered operation: {op_id}")


def _available(db, run_id):
    return dict(db.execute("SELECT name,sha256 FROM kernel_artifacts WHERE run_id=? ORDER BY name", (run_id,)))


def _pending_clarification(db, run_id):
    return db.execute("SELECT artifact,reason FROM kernel_clarifications "
                      "WHERE run_id=? AND supplied_sha256 IS NULL", (run_id,)).fetchone()


class KernelStore:
    """Each file operation is a short transaction. External I/O takes place between claim and settle."""

    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        self._lock = threading.RLock()
        self._memory = self._connect() if self._path == ":memory:" else None
        if self._memory is None:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript(_SCHEMA)

    def _connect(self):
        db = sqlite3.connect(self._path, timeout=10, isolation_level=None, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _connection(self):
        if self._path == ":memory:":
            with self._lock:
                if self._memory is None:
                    raise RuntimeError("in-memory store is closed")
                yield self._memory
        else:
            db = self._connect()
            try:
                yield db
            finally:
                db.close()

    @contextmanager
    def _transaction(self, *, write=False):
        with self._connection() as db:
            with db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db

    def close(self):
        with self._lock:
            if self._memory is not None:
                self._memory.close()
                self._memory = None

    def create(self, run_id: str, job_dict: dict) -> None:
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 200 or not run_id.isprintable() or run_id != run_id.strip():
            raise ValueError("invalid run ID")
        job = Job.from_dict(job_dict).to_dict()
        encoded = _canonical(job)
        digest = _digest(encoded)
        with self._transaction(write=True) as db:
            existing = db.execute("SELECT job_sha256 FROM kernel_runs WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise RunConflictError("run ID is already bound to a different job")
                return
            db.execute("INSERT INTO kernel_runs VALUES (?,?,?,'active','')", (run_id, encoded, digest))
            _event(db, run_id, "run_created", job_sha256=digest, budget_units=job["budget_units"])
            for key, value in sorted(job["inputs"].items()):
                payload = _canonical(value)
                sha = _digest(payload)
                _artifact(db, run_id, key, payload, sha)
                _event(db, run_id, "input_recorded", artifact=key, sha256=sha)

    def snapshot(self, run_id: str) -> dict:
        with self._transaction() as db:
            run = _run(db, run_id)
            rows = db.execute(
                "SELECT a.name,a.sha256,b.json FROM kernel_artifacts a JOIN kernel_blobs b ON a.sha256=b.sha256 "
                "WHERE a.run_id=? ORDER BY a.name", (run_id,)
            ).fetchall()
            attempts = db.execute("SELECT * FROM kernel_attempts WHERE run_id=? ORDER BY op_id", (run_id,)).fetchall()
            pending = next((a for a in attempts if a["accepted"] is None), None)
            active = None
            if pending:
                active = {k: pending[k] for k in ("id", "op_id", "cost_units")}
                active["phase"] = "claimed"
                if pending["returned_json"] is not None:
                    active.update(phase="returned", value=json.loads(pending["returned_json"]),
                                  actual_units=pending["returned_units"])
                    if pending["receipt_id"] is not None:
                        active["receipt_id"] = pending["receipt_id"]
            waiting = _pending_clarification(db, run_id)
            state = {
                "run_id": run_id, "job": json.loads(run["job"]), "job_sha256": run["job_sha256"],
                "status": "needs_reconciliation" if pending else run["status"],
                "reason": (pending["uncertain_reason"] or "attempt_in_flight") if pending else run["reason"],
                "artifacts": {r["name"]: json.loads(r["json"]) for r in rows},
                "artifact_hashes": {r["name"]: r["sha256"] for r in rows},
                "excluded": [a["op_id"] for a in attempts if a["accepted"] is not None],
                "spent_units": sum(a["actual_units"] for a in attempts if a["actual_units"] is not None),
                "held_units": pending["cost_units"] if pending else 0,
                "active_attempt": active,
            }
            if waiting:
                state.update(status="waiting_for_input", reason=waiting["reason"], clarification=dict(waiting))
            return state

    def claim(self, run_id: str, op_id: str, *, expected_excluded=None, expected_hashes=None, plan=None) -> str | None:
        with self._transaction(write=True) as db:
            run = _run(db, run_id)
            job = json.loads(run["job"])
            op = _operation(job, op_id)
            busy = db.execute("SELECT 1 FROM kernel_attempts WHERE run_id=? AND (op_id=? OR accepted IS NULL)",
                              (run_id, op_id)).fetchone()
            available = _available(db, run_id)
            if run["status"] != "active" or busy or op["produces"] in available or _pending_clarification(db, run_id):
                return None
            if expected_hashes is not None and available != expected_hashes:
                return None
            if expected_excluded is not None:
                settled = db.execute("SELECT op_id FROM kernel_attempts WHERE run_id=? AND accepted IS NOT NULL",
                                     (run_id,)).fetchall()
                if set(expected_excluded) != {row["op_id"] for row in settled}:
                    return None
            if set(job["goals"]) <= available.keys() or not set(op["requires"]) <= available.keys():
                return None
            remaining = job["budget_units"] - _amount(db, run_id)
            if op["cost_units"] > remaining:
                return None
            if plan is not None and (plan.status != "ready" or plan.operations[:1] != (op_id,) or
                                     plan.cost_units is None or plan.cost_units > remaining):
                raise ValueError("plan does not authorize this attempt within its remaining budget")
            attempt_id = uuid.uuid4().hex
            db.execute("INSERT INTO kernel_attempts(id,run_id,op_id,cost_units) VALUES (?,?,?,?)",
                       (attempt_id, run_id, op_id, op["cost_units"]))
            _event(db, run_id, "attempt_claimed", attempt_id=attempt_id, op_id=op_id, cost_units=op["cost_units"],
                   input_hashes={key: available[key] for key in op["requires"]}, remaining_units=remaining,
                   **({"plan": plan.to_dict()} if plan is not None else {}))
            return attempt_id

    def request_input(self, run_id: str, plan, *, expected_excluded, expected_hashes) -> bool:
        """Publish a wait only if the decision's entire observed state still holds."""
        evidence = plan.evidence
        if plan.status != "waiting_for_input" or evidence is None or evidence.request is None:
            raise ValueError("expected a clarification decision")
        with self._transaction(write=True) as db:
            run = _run(db, run_id)
            available = _available(db, run_id)
            if (run["status"] != "active" or available != expected_hashes or
                    evidence.request in available or _pending_clarification(db, run_id)):
                return False
            attempts = db.execute("SELECT op_id,accepted FROM kernel_attempts WHERE run_id=?", (run_id,)).fetchall()
            if any(a["accepted"] is None for a in attempts) or {a["op_id"] for a in attempts} != set(expected_excluded):
                return False
            job = json.loads(run["job"])
            rules = job.get("evidence_rules", [])
            if (not 0 <= evidence.rule_index < len(rules) or
                    rules[evidence.rule_index].get("request") != evidence.request or
                    rules[evidence.rule_index].get("reason") != evidence.reason or
                    evidence.request not in job["acceptance"] or available.get(evidence.artifact) != evidence.artifact_sha256):
                raise ValueError("clarification does not match frozen evidence and input contract")
            db.execute("INSERT INTO kernel_clarifications(run_id,artifact,reason) VALUES (?,?,?)",
                       (run_id, evidence.request, evidence.reason))
            _event(db, run_id, "input_requested", artifact=evidence.request, reason=evidence.reason, plan=plan.to_dict())
            return True

    def provide_validated(self, run_id: str, artifact: str, value) -> None:
        """Commit a host-validated answer to its open request, or acknowledge an exact replay."""
        encoded = canonical_json(value)
        digest = _digest(encoded)
        with self._transaction(write=True) as db:
            run = _run(db, run_id)
            row = db.execute("SELECT supplied_sha256 FROM kernel_clarifications WHERE run_id=? AND artifact=?",
                             (run_id, artifact)).fetchone()
            if row is None:
                raise ValueError("this artifact was not requested")
            if row["supplied_sha256"] is not None:
                if row["supplied_sha256"] != digest:
                    raise RunConflictError("the requested input already has a different value")
                return
            if run["status"] != "active":
                raise ValueError("the run is no longer waiting for input")
            _artifact(db, run_id, artifact, encoded, digest)
            db.execute("UPDATE kernel_clarifications SET supplied_sha256=? WHERE run_id=? AND artifact=?",
                       (digest, run_id, artifact))
            _event(db, run_id, "input_supplied", artifact=artifact, sha256=digest)

    def mark_uncertain(self, run_id: str, attempt_id: str, reason: str) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("an uncertainty reason is required")
        with self._transaction(write=True) as db:
            attempt = _attempt(db, run_id, attempt_id)
            if attempt["accepted"] is None and attempt["uncertain_reason"] is None:
                db.execute("UPDATE kernel_attempts SET uncertain_reason=? WHERE id=?", (reason, attempt_id))
                _event(db, run_id, "attempt_uncertain", attempt_id=attempt_id, reason=reason)

    def record_result(self, run_id: str, attempt_id: str, value, actual_units: int | None = None,
                      *, receipt_id: str | None = None) -> None:
        """Freeze the provider's return before validation. Only settle may publish it."""
        if receipt_id is not None and (not isinstance(receipt_id, str) or not receipt_id.strip()):
            raise ValueError("invalid executor receipt ID")
        encoded = canonical_json(value)
        with self._transaction(write=True) as db:
            attempt = _attempt(db, run_id, attempt_id)
            charge = _charge(attempt, actual_units)
            if attempt["returned_json"] is not None:
                if (encoded, charge, receipt_id) != (attempt["returned_json"], attempt["returned_units"], attempt["receipt_id"]):
                    raise ValueError("conflicting executor receipt")
                return
            if attempt["accepted"] is not None:
                raise ValueError("attempt already settled")
            db.execute("UPDATE kernel_attempts SET returned_json=?,returned_units=?,receipt_id=? WHERE id=?",
                       (encoded, charge, receipt_id, attempt_id))
            _event(db, run_id, "result_recorded", attempt_id=attempt_id, sha256=_digest(encoded), actual_units=charge,
                   receipt_id=receipt_id)

    def settle(self, run_id: str, attempt_id: str, *, accepted: bool, value=_NOT_SUPPLIED,
               reason: str = "", actual_units: int | None = None, stop_reason: str | None = None,
               require_unreturned: bool = False) -> None:
        if type(accepted) is not bool or not isinstance(reason, str):
            raise ValueError("invalid settlement receipt")
        if stop_reason is not None and (accepted or not isinstance(stop_reason, str) or not stop_reason):
            raise ValueError("a terminal refusal requires a reason and a rejected outcome")
        with self._transaction(write=True) as db:
            attempt = _attempt(db, run_id, attempt_id)
            returned = attempt["returned_json"]
            if require_unreturned and returned is not None:
                raise ValueError("a returned executor receipt already exists")
            if value is _NOT_SUPPLIED:
                encoded = returned if returned is not None else "null"
            else:
                encoded = canonical_json(value)
            effective_units = attempt["returned_units"] if actual_units is None else actual_units
            charge = _charge(attempt, effective_units)
            if returned is not None and (encoded != returned or charge != attempt["returned_units"]):
                raise ValueError("settlement differs from the executor receipt")
            digest = _digest(encoded)
            if attempt["accepted"] is not None:
                previous = (attempt["accepted"], attempt["actual_units"], attempt["result_sha256"],
                            attempt["reason"], attempt["stop_reason"])
                if previous != (accepted, charge, digest, reason, stop_reason):
                    raise ValueError("conflicting settlement for the same attempt")
                return
            op = _operation(json.loads(_run(db, run_id)["job"]), attempt["op_id"])
            if accepted:
                _artifact(db, run_id, op["produces"], encoded, digest)
            db.execute("UPDATE kernel_attempts SET actual_units=?,accepted=?,result_sha256=?,reason=?,stop_reason=? WHERE id=?",
                       (charge, accepted, digest, reason, stop_reason, attempt_id))
            _event(db, run_id, "attempt_settled", op_id=op["id"], attempt_id=attempt_id, accepted=accepted,
                   artifact=op["produces"], sha256=digest, actual_units=charge, reason=reason)
            if stop_reason is not None:
                db.execute("UPDATE kernel_runs SET status='blocked',reason=? WHERE run_id=?", (stop_reason, run_id))
                _event(db, run_id, "run_finished", status="blocked", reason=stop_reason)

    def finish(self, run_id: str, status: str, reason: str, *, expected_excluded=None,
               expected_hashes=None, plan=None) -> bool:
        if status not in {"complete", "blocked"} or not isinstance(reason, str):
            raise ValueError("invalid terminal state")
        with self._transaction(write=True) as db:
            run = _run(db, run_id)
            if run["status"] != "active":
                return run["status"] == status and run["reason"] == reason
            if _pending_clarification(db, run_id):
                return False
            rows = db.execute("SELECT op_id,accepted FROM kernel_attempts WHERE run_id=?", (run_id,)).fetchall()
            if any(r["accepted"] is None for r in rows):
                return False
            if expected_excluded is not None and set(expected_excluded) != {r["op_id"] for r in rows}:
                return False
            if status == "blocked" and expected_excluded is None:
                raise ValueError("blocked decisions require the snapshot's excluded operation IDs")
            job = json.loads(run["job"])
            available = _available(db, run_id)
            if expected_hashes is not None and available != expected_hashes:
                return False
            satisfied = set(job["goals"]) <= available.keys()
            if (status == "complete") != satisfied:
                return False
            db.execute("UPDATE kernel_runs SET status=?,reason=? WHERE run_id=?", (status, reason, run_id))
            _event(db, run_id, "run_finished", status=status, reason=reason,
                   **({"plan": plan.to_dict()} if plan is not None else {}))
            return True

    def events(self, run_id: str) -> list[dict]:
        with self._transaction() as db:
            _run(db, run_id)
            rows = db.execute("SELECT seq,type,data FROM kernel_events WHERE run_id=? ORDER BY seq", (run_id,))
            return [dict(seq=r["seq"], type=r["type"], **json.loads(r["data"])) for r in rows]
