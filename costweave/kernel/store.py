"""Durable attempts, immutable validated artifacts and a bounded multi-slot execution ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections import Counter
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
    attempt_no INTEGER NOT NULL DEFAULT 1,
    cost_units INTEGER NOT NULL CHECK(cost_units >= 0),
    actual_units INTEGER CHECK(actual_units BETWEEN 0 AND cost_units),
    accepted INTEGER CHECK(accepted IN (0,1)),
    superseded INTEGER NOT NULL DEFAULT 0 CHECK(superseded IN (0,1)),
    risk_used REAL NOT NULL DEFAULT 0.0,
    result_sha256 TEXT,
    returned_json TEXT,
    returned_units INTEGER CHECK(returned_units BETWEEN 0 AND cost_units),
    receipt_id TEXT,
    stop_reason TEXT,
    reason TEXT NOT NULL DEFAULT '',
    uncertain_reason TEXT,
    UNIQUE(run_id, op_id, attempt_no),
    CHECK ((actual_units IS NULL) = (accepted IS NULL)),
    CHECK ((returned_json IS NULL) = (returned_units IS NULL))
);
CREATE INDEX IF NOT EXISTS kernel_active_attempts
    ON kernel_attempts(run_id) WHERE accepted IS NULL;
CREATE TABLE IF NOT EXISTS kernel_events (
    seq INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES kernel_runs,
    type TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS kernel_events_order ON kernel_events(run_id, seq);
COMMIT;
"""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(encoded):
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_NOT_SUPPLIED = object()


class RunConflictError(ValueError):
    """This run ID already names a different immutable job."""


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


def _reserved_or_spent(db, run_id) -> int:
    return db.execute(
        "SELECT COALESCE(SUM(CASE WHEN accepted IS NOT NULL THEN actual_units ELSE cost_units END), 0) "
        "FROM kernel_attempts WHERE run_id=?",
        (run_id,),
    ).fetchone()[0]


def _operation(job, op_id):
    for operation in job["operations"]:
        if operation["id"] == op_id:
            return operation
    raise ValueError(f"unregistered operation: {op_id}")


def _available(db, run_id):
    return dict(db.execute("SELECT name,sha256 FROM kernel_artifacts WHERE run_id=? ORDER BY name", (run_id,)))


def _exhausted_ops(job: dict, settled_rows) -> set[str]:
    counts = Counter(row["op_id"] for row in settled_rows)
    limits = {op["id"]: int(op.get("max_attempts", 1)) for op in job["operations"]}
    return {op_id for op_id, count in counts.items() if count >= limits.get(op_id, 1)}


def _attempt_view(row) -> dict:
    item = {k: row[k] for k in ("id", "op_id", "cost_units")}
    item["attempt_no"] = row["attempt_no"]
    item["superseded"] = bool(row["superseded"])
    item["phase"] = "claimed"
    if row["returned_json"] is not None:
        item.update(
            phase="returned",
            value=json.loads(row["returned_json"]),
            actual_units=row["returned_units"],
        )
        if row["receipt_id"] is not None:
            item["receipt_id"] = row["receipt_id"]
    return item


def _logical_digest(db: sqlite3.Connection) -> str:
    payload = {
        "runs": [dict(r) for r in db.execute("SELECT * FROM kernel_runs ORDER BY run_id")],
        "blobs": [dict(r) for r in db.execute("SELECT * FROM kernel_blobs ORDER BY sha256")],
        "artifacts": [dict(r) for r in db.execute("SELECT * FROM kernel_artifacts ORDER BY run_id, name")],
        "attempts": [dict(r) for r in db.execute("SELECT * FROM kernel_attempts ORDER BY id")],
        "events": [dict(r) for r in db.execute("SELECT * FROM kernel_events ORDER BY seq")],
    }
    return _digest(_canonical(payload))


class KernelStore:
    """Each state transition is a short transaction. External I/O takes place between claim and settle."""

    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        self._lock = threading.RLock()
        self._memory = self._connect() if self._path == ":memory:" else None
        if self._memory is None:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript(_SCHEMA)
            self._migrate_attempts(db)

    def _migrate_attempts(self, db: sqlite3.Connection) -> None:
        cols = {row["name"] for row in db.execute("PRAGMA table_info(kernel_attempts)")}
        if {"attempt_no", "superseded", "risk_used"} <= cols:
            db.execute("DROP INDEX IF EXISTS kernel_one_active_attempt")
            db.execute("CREATE INDEX IF NOT EXISTS kernel_active_attempts ON kernel_attempts(run_id) WHERE accepted IS NULL")
            return
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DROP INDEX IF EXISTS kernel_one_active_attempt")
            db.execute("""
                CREATE TABLE kernel_attempts_v2 (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES kernel_runs,
                    op_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL DEFAULT 1,
                    cost_units INTEGER NOT NULL CHECK(cost_units >= 0),
                    actual_units INTEGER CHECK(actual_units BETWEEN 0 AND cost_units),
                    accepted INTEGER CHECK(accepted IN (0,1)),
                    superseded INTEGER NOT NULL DEFAULT 0 CHECK(superseded IN (0,1)),
                    risk_used REAL NOT NULL DEFAULT 0.0,
                    result_sha256 TEXT,
                    returned_json TEXT,
                    returned_units INTEGER CHECK(returned_units BETWEEN 0 AND cost_units),
                    receipt_id TEXT,
                    stop_reason TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    uncertain_reason TEXT,
                    UNIQUE(run_id, op_id, attempt_no),
                    CHECK ((actual_units IS NULL) = (accepted IS NULL)),
                    CHECK ((returned_json IS NULL) = (returned_units IS NULL))
                )
            """)
            db.execute("""
                INSERT INTO kernel_attempts_v2 (
                    id, run_id, op_id, attempt_no, cost_units, actual_units, accepted,
                    superseded, risk_used, result_sha256, returned_json, returned_units,
                    receipt_id, stop_reason, reason, uncertain_reason
                )
                SELECT id, run_id, op_id, 1, cost_units, actual_units, accepted,
                       0, 0.0, result_sha256, returned_json, returned_units,
                       receipt_id, stop_reason, reason, uncertain_reason
                FROM kernel_attempts
            """)
            db.execute("DROP TABLE kernel_attempts")
            db.execute("ALTER TABLE kernel_attempts_v2 RENAME TO kernel_attempts")
            db.execute("CREATE INDEX IF NOT EXISTS kernel_active_attempts ON kernel_attempts(run_id) WHERE accepted IS NULL")

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
            job = json.loads(run["job"])
            rows = db.execute(
                "SELECT a.name,a.sha256,b.json FROM kernel_artifacts a JOIN kernel_blobs b ON a.sha256=b.sha256 "
                "WHERE a.run_id=? ORDER BY a.name", (run_id,)
            ).fetchall()
            attempts = db.execute(
                "SELECT * FROM kernel_attempts WHERE run_id=? ORDER BY attempt_no, op_id", (run_id,)
            ).fetchall()
            pending = [a for a in attempts if a["accepted"] is None]
            settled = [a for a in attempts if a["accepted"] is not None]
            active_attempts = [_attempt_view(a) for a in pending]
            active = active_attempts[0] if active_attempts else None
            first_pending = pending[0] if pending else None
            counts = Counter(a["op_id"] for a in attempts)
            return {
                "run_id": run_id,
                "job": job,
                "job_sha256": run["job_sha256"],
                "status": "needs_reconciliation" if pending else run["status"],
                "reason": (first_pending["uncertain_reason"] or "attempt_in_flight") if first_pending else run["reason"],
                "artifacts": {r["name"]: json.loads(r["json"]) for r in rows},
                "artifact_hashes": {r["name"]: r["sha256"] for r in rows},
                "excluded": sorted(_exhausted_ops(job, settled)),
                "attempt_counts": dict(counts),
                "spent_units": sum(a["actual_units"] for a in settled),
                "held_units": sum(a["cost_units"] for a in pending),
                "risk_spent": sum(float(a["risk_used"]) for a in settled if a["accepted"] == 1),
                "active_attempt": active,
                "active_attempts": active_attempts,
            }

    def claim(self, run_id: str, op_id: str, *, expected_excluded=None, plan=None) -> str | None:
        with self._transaction(write=True) as db:
            run = _run(db, run_id)
            job = json.loads(run["job"])
            op = _operation(job, op_id)
            attempts = db.execute(
                "SELECT op_id,accepted,attempt_no FROM kernel_attempts WHERE run_id=?", (run_id,)
            ).fetchall()
            active_rows = [a for a in attempts if a["accepted"] is None]
            settled_rows = [a for a in attempts if a["accepted"] is not None]
            max_parallel = int(job.get("max_parallel", 1))
            max_attempts = int(op.get("max_attempts", 1))
            op_attempts = [a for a in attempts if a["op_id"] == op_id]
            settled_for_op = sum(1 for a in op_attempts if a["accepted"] is not None)
            active_for_op = any(a["accepted"] is None for a in op_attempts)
            available = _available(db, run_id)

            if (
                run["status"] != "active"
                or len(active_rows) >= max_parallel
                or active_for_op
                or settled_for_op >= max_attempts
                or op["produces"] in available
            ):
                return None
            if expected_excluded is not None:
                if set(expected_excluded) != _exhausted_ops(job, settled_rows):
                    return None
            if set(job["goals"]) <= available.keys() or not set(op["requires"]) <= available.keys():
                return None
            remaining = job["budget_units"] - _reserved_or_spent(db, run_id)
            if op["cost_units"] > remaining:
                return None
            if plan is not None and (plan.status != "ready" or plan.operations[:1] != (op_id,) or
                                     plan.cost_units is None or plan.cost_units > remaining):
                raise ValueError("plan does not authorize this attempt within its remaining budget")
            attempt_no = len(op_attempts) + 1
            attempt_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO kernel_attempts(id,run_id,op_id,attempt_no,cost_units) VALUES (?,?,?,?,?)",
                (attempt_id, run_id, op_id, attempt_no, op["cost_units"]),
            )
            _event(db, run_id, "attempt_claimed", attempt_id=attempt_id, op_id=op_id, attempt_no=attempt_no,
                   cost_units=op["cost_units"], input_hashes={key: available[key] for key in op["requires"]},
                   remaining_units=remaining, **({"plan": plan.to_dict()} if plan is not None else {}))
            return attempt_id

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
               require_unreturned: bool = False) -> list[str]:
        if type(accepted) is not bool or not isinstance(reason, str):
            raise ValueError("invalid settlement receipt")
        if stop_reason is not None and (accepted or not isinstance(stop_reason, str) or not stop_reason):
            raise ValueError("a terminal refusal requires a reason and a rejected outcome")
        superseded_ids: list[str] = []
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
            job = json.loads(_run(db, run_id)["job"])
            op = _operation(job, attempt["op_id"])

            effective_accepted = accepted
            effective_reason = reason
            if attempt["accepted"] is None and attempt["superseded"] == 1 and accepted:
                effective_accepted = False
                effective_reason = "superseded" if reason in ("", "accepted") else reason

            if attempt["accepted"] is not None:
                previous = (bool(attempt["accepted"]), attempt["actual_units"], attempt["result_sha256"],
                            attempt["reason"], attempt["stop_reason"])
                if previous != (effective_accepted, charge, digest, effective_reason, stop_reason):
                    raise ValueError("conflicting settlement for the same attempt")
                return []

            risk_used = float(op.get("false_pass_risk", 0.0)) if effective_accepted else 0.0
            if effective_accepted:
                available = _available(db, run_id)
                if op["produces"] not in available:
                    _artifact(db, run_id, op["produces"], encoded, digest)
                competing_ops = [o["id"] for o in job["operations"] if o["produces"] == op["produces"]]
                if competing_ops:
                    placeholders = ",".join("?" for _ in competing_ops)
                    rows = db.execute(
                        f"SELECT id FROM kernel_attempts WHERE run_id=? AND id<>? AND accepted IS NULL AND op_id IN ({placeholders})",
                        (run_id, attempt_id, *competing_ops),
                    ).fetchall()
                    superseded_ids = [r["id"] for r in rows]
                    if superseded_ids:
                        s_placeholders = ",".join("?" for _ in superseded_ids)
                        db.execute(
                            f"UPDATE kernel_attempts SET superseded=1 WHERE id IN ({s_placeholders})",
                            superseded_ids,
                        )
            db.execute(
                "UPDATE kernel_attempts SET actual_units=?,accepted=?,risk_used=?,result_sha256=?,reason=?,stop_reason=? WHERE id=?",
                (charge, effective_accepted, risk_used, digest, effective_reason, stop_reason, attempt_id),
            )
            _event(db, run_id, "attempt_settled", op_id=op["id"], attempt_id=attempt_id, accepted=effective_accepted,
                   artifact=op["produces"], sha256=digest, actual_units=charge, reason=effective_reason)
            if stop_reason is not None:
                db.execute("UPDATE kernel_runs SET status='blocked',reason=? WHERE run_id=?", (stop_reason, run_id))
                _event(db, run_id, "run_finished", status="blocked", reason=stop_reason)
        return superseded_ids

    def finish(self, run_id: str, status: str, reason: str, *, expected_excluded=None) -> bool:
        if status not in {"complete", "blocked"} or not isinstance(reason, str):
            raise ValueError("invalid terminal state")
        with self._transaction(write=True) as db:
            run = _run(db, run_id)
            if run["status"] != "active":
                return run["status"] == status and run["reason"] == reason
            rows = db.execute("SELECT op_id,accepted FROM kernel_attempts WHERE run_id=?", (run_id,)).fetchall()
            if any(r["accepted"] is None for r in rows):
                return False
            job = json.loads(run["job"])
            if expected_excluded is not None and set(expected_excluded) != _exhausted_ops(job, rows):
                return False
            if status == "blocked" and expected_excluded is None:
                raise ValueError("blocked decisions require the snapshot's excluded operation IDs")
            satisfied = set(job["goals"]) <= _available(db, run_id).keys()
            if (status == "complete") != satisfied:
                return False
            db.execute("UPDATE kernel_runs SET status=?,reason=? WHERE run_id=?", (status, reason, run_id))
            _event(db, run_id, "run_finished", status=status, reason=reason)
            return True

    def events(self, run_id: str) -> list[dict]:
        with self._transaction() as db:
            _run(db, run_id)
            rows = db.execute("SELECT seq,type,data FROM kernel_events WHERE run_id=? ORDER BY seq", (run_id,))
            return [dict(seq=r["seq"], type=r["type"], **json.loads(r["data"])) for r in rows]

    def backup_and_verify(self, target_path: str | Path) -> dict:
        """Create an atomic SQLite backup and verify schema/logical SHA-256 parity."""
        dest = Path(target_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as src_db:
            src_hash = _logical_digest(src_db)
            dest_db = sqlite3.connect(str(dest), timeout=10, isolation_level=None)
            dest_db.row_factory = sqlite3.Row
            try:
                src_db.backup(dest_db)
                quick = dest_db.execute("PRAGMA quick_check").fetchone()[0]
                fk_rows = dest_db.execute("PRAGMA foreign_key_check").fetchall()
                dest_hash = _logical_digest(dest_db)
            finally:
                dest_db.close()
        return {
            "backup_path": str(dest),
            "healthy": quick == "ok" and not fk_rows,
            "logical_hash_match": src_hash == dest_hash,
            "logical_sha256": dest_hash,
        }
