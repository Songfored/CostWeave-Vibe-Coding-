"""SQLite persistence and deletion governance for outcome learning copies."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .evaluation_protocol import canonical_json
from .outcome_dataset import (
    LabelEvidence,
    OutcomeDataset,
    OutcomeGovernanceError,
    TrainingFrozenError,
    GovernedOutcome,
)


class OutcomeDatasetStore:
    """Persist immutable dataset versions and append-only label evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _hash(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def record(self, dataset: OutcomeDataset) -> bool:
        manifest_raw = canonical_json(dataset.manifest())
        quality_raw = canonical_json(dataset.quality_report)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT manifest_json,quality_report_json "
                    "FROM outcome_datasets WHERE dataset_id=?",
                    (dataset.dataset_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        canonical_json(
                            json.loads(existing["manifest_json"])
                        )
                        != manifest_raw
                        or canonical_json(
                            json.loads(existing["quality_report_json"])
                        )
                        != quality_raw
                    ):
                        raise OutcomeGovernanceError(
                            "dataset id was reused for different evidence"
                        )
                    count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM outcome_learning_samples "
                            "WHERE dataset_id=?",
                            (dataset.dataset_id,),
                        ).fetchone()[0]
                    )
                    deleted = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM outcome_deletion_audits "
                            "WHERE dataset_id=?",
                            (dataset.dataset_id,),
                        ).fetchone()[0]
                    )
                    if count + deleted != len(dataset.samples):
                        raise OutcomeGovernanceError(
                            "persisted learning copy is incomplete"
                        )
                    connection.execute("COMMIT")
                    return False

                manifest = dataset.manifest()
                connection.execute(
                    "INSERT INTO outcome_datasets("
                    "dataset_id,schema_version,builder_revision,"
                    "source_report_id,source_report_hash,purpose,"
                    "sample_count,effective_task_count,content_hash,"
                    "governance_passed,training_allowed,manifest_json,"
                    "quality_report_json,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        dataset.dataset_id,
                        dataset.schema_version,
                        dataset.builder_revision,
                        dataset.source_report_id,
                        dataset.source_report_hash,
                        dataset.purpose,
                        len(dataset.samples),
                        float(manifest["effective_task_count"]),
                        dataset.content_hash,
                        int(bool(manifest["governance_passed"])),
                        int(bool(manifest["training_allowed"])),
                        manifest_raw,
                        quality_raw,
                        dataset.created_at,
                    ),
                )
                for sample in dataset.samples:
                    sample_raw = canonical_json(sample.to_dict())
                    connection.execute(
                        "INSERT INTO outcome_learning_samples("
                        "sample_id,dataset_id,source_observation_hash,"
                        "anonymous_task_id,anonymous_project_id,"
                        "anonymous_user_id,partition_name,dedupe_group,"
                        "sample_weight,evidence_level,purpose,expires_at,"
                        "sample_json,sample_hash,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            sample.sample_id,
                            dataset.dataset_id,
                            sample.source_observation_hash,
                            sample.anonymous_task_id,
                            sample.anonymous_project_id,
                            sample.anonymous_user_id,
                            sample.partition,
                            sample.dedupe_group,
                            sample.sample_weight,
                            sample.evidence_level,
                            sample.purpose,
                            sample.expires_at,
                            sample_raw,
                            self._hash(sample_raw),
                            dataset.created_at,
                        ),
                    )
                    for event in sample.label_events:
                        self._insert_label(connection, event)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def _insert_label(
        self,
        connection: sqlite3.Connection,
        event: LabelEvidence,
    ) -> bool:
        raw = canonical_json(event.to_dict())
        existing = connection.execute(
            "SELECT event_json FROM outcome_label_events "
            "WHERE label_event_id=?",
            (event.label_event_id,),
        ).fetchone()
        if existing is not None:
            if canonical_json(json.loads(existing["event_json"])) != raw:
                raise OutcomeGovernanceError(
                    "label event id was reused for different evidence"
                )
            return False
        connection.execute(
            "INSERT INTO outcome_label_events("
            "label_event_id,sample_id,source,verdict,independent,"
            "blinded,delayed,evidence_level,evaluator_revision,"
            "event_json,event_hash,recorded_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.label_event_id,
                event.sample_id,
                event.source,
                event.verdict,
                int(event.independent),
                int(event.blinded),
                int(event.delayed),
                event.evidence_level,
                event.evaluator_revision,
                raw,
                self._hash(raw),
                event.recorded_at,
            ),
        )
        return True

    def append_label(self, event: LabelEvidence) -> bool:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute(
                    "SELECT 1 FROM outcome_learning_samples "
                    "WHERE sample_id=?",
                    (event.sample_id,),
                ).fetchone()
                if exists is None:
                    raise OutcomeGovernanceError(
                        "label target is not an active learning copy"
                    )
                inserted = self._insert_label(connection, event)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return inserted

    def manifest(self, dataset_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM outcome_datasets "
                "WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()
        return json.loads(row["manifest_json"]) if row else None

    def quality_report(self, dataset_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT quality_report_json FROM outcome_datasets "
                "WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()
        return json.loads(row["quality_report_json"]) if row else None

    def sample(self, sample_id: str) -> GovernedOutcome | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT sample_json FROM outcome_learning_samples "
                "WHERE sample_id=?",
                (sample_id,),
            ).fetchone()
            if row is None:
                return None
            labels = connection.execute(
                "SELECT event_json FROM outcome_label_events "
                "WHERE sample_id=? ORDER BY recorded_at,label_event_id",
                (sample_id,),
            ).fetchall()
        sample = GovernedOutcome.from_dict(json.loads(row["sample_json"]))
        for label in labels:
            sample = sample.append_label(
                LabelEvidence.from_dict(json.loads(label["event_json"]))
            )
        return sample

    def samples(self, dataset_id: str) -> list[GovernedOutcome]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sample_id FROM outcome_learning_samples "
                "WHERE dataset_id=? "
                "ORDER BY partition_name,anonymous_task_id,sample_id",
                (dataset_id,),
            ).fetchall()
        return [
            sample
            for row in rows
            if (sample := self.sample(str(row["sample_id"]))) is not None
        ]

    def delete_learning_copy(
        self,
        sample_id: str,
        *,
        reason_code: str,
        requested_at: str,
    ) -> bool:
        if not reason_code or len(reason_code) > 80:
            raise OutcomeGovernanceError(
                "a bounded deletion reason code is required"
            )
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT dataset_id,sample_hash "
                    "FROM outcome_learning_samples WHERE sample_id=?",
                    (sample_id,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return False
                label_rows = connection.execute(
                    "SELECT event_hash FROM outcome_label_events "
                    "WHERE sample_id=? ORDER BY event_hash",
                    (sample_id,),
                ).fetchall()
                label_set_hash = self._hash(
                    canonical_json(
                        [item["event_hash"] for item in label_rows]
                    )
                )
                deletion_id = "deletion_" + self._hash(
                    canonical_json(
                        {
                            "dataset_id": row["dataset_id"],
                            "sample_id": sample_id,
                            "sample_hash": row["sample_hash"],
                            "reason_code": reason_code,
                        }
                    )
                )[:32]
                connection.execute(
                    "INSERT OR IGNORE INTO outcome_deletion_audits("
                    "deletion_id,dataset_id,sample_id,sample_hash,"
                    "label_set_hash,reason_code,deleted_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        deletion_id,
                        row["dataset_id"],
                        sample_id,
                        row["sample_hash"],
                        label_set_hash,
                        reason_code,
                        requested_at,
                    ),
                )
                connection.execute(
                    "DELETE FROM outcome_learning_samples "
                    "WHERE sample_id=?",
                    (sample_id,),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def purge_expired(self, *, now: str) -> int:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sample_id FROM outcome_learning_samples "
                "WHERE expires_at<=? ORDER BY sample_id",
                (now,),
            ).fetchall()
        deleted = 0
        for row in rows:
            deleted += self.delete_learning_copy(
                str(row["sample_id"]),
                reason_code="ttl_expired",
                requested_at=now,
            )
        return deleted

    def deletion_audits(
        self, dataset_id: str
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM outcome_deletion_audits "
                "WHERE dataset_id=? ORDER BY deleted_at,deletion_id",
                (dataset_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def training_snapshot(
        self, dataset_id: str
    ) -> tuple[GovernedOutcome, ...]:
        manifest = self.manifest(dataset_id)
        if manifest is None:
            raise OutcomeGovernanceError("dataset does not exist")
        if not bool(manifest.get("training_allowed")):
            reasons = ", ".join(manifest.get("freeze_reasons", ()))
            raise TrainingFrozenError(
                f"dataset {dataset_id} is frozen: {reasons}"
            )
        samples = self.samples(dataset_id)
        if len(samples) != int(manifest["sample_count"]):
            raise TrainingFrozenError(
                "learning copy changed after dataset registration"
            )
        unsafe = [
            item.sample_id
            for item in samples
            if item.resolved_label.get("state")
            in {"unlabelled", "conflict"}
        ]
        if unsafe:
            raise TrainingFrozenError(
                "training snapshot contains unresolved labels"
            )
        return tuple(samples)

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            values = {
                "outcome_datasets": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM outcome_datasets"
                    ).fetchone()[0]
                ),
                "outcome_learning_samples": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM outcome_learning_samples"
                    ).fetchone()[0]
                ),
                "outcome_label_events": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM outcome_label_events"
                    ).fetchone()[0]
                ),
                "outcome_deletion_audits": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM outcome_deletion_audits"
                    ).fetchone()[0]
                ),
            }
        return values


__all__ = ["OutcomeDatasetStore"]
