"""Immutable SQLite records for predictor admission and frozen receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .contracts_v4 import utc_now
from .evaluation_protocol import canonical_json
from .predictor_admission import (
    FrozenPredictionReceipt,
    PredictorAdmissionError,
    PredictorAdmissionReport,
)


class PredictorAdmissionStore:
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

    @staticmethod
    def _same(raw: str, value: Mapping[str, Any]) -> bool:
        return canonical_json(json.loads(raw)) == canonical_json(value)

    def record_report(self, report: PredictorAdmissionReport) -> bool:
        value = report.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                dataset = connection.execute(
                    "SELECT content_hash,training_allowed FROM outcome_datasets "
                    "WHERE dataset_id=?",
                    (report.dataset_id,),
                ).fetchone()
                if (
                    dataset is None
                    or dataset["content_hash"] != report.dataset_content_hash
                ):
                    raise PredictorAdmissionError(
                        "admission report does not match a persisted dataset version"
                    )
                if report.training_allowed and not bool(dataset["training_allowed"]):
                    raise PredictorAdmissionError(
                        "admission report cannot bypass the dataset training freeze"
                    )
                existing = connection.execute(
                    "SELECT report_json FROM predictor_admission_reports "
                    "WHERE report_id=?",
                    (report.report_id,),
                ).fetchone()
                if existing is not None:
                    if not self._same(existing["report_json"], value):
                        raise PredictorAdmissionError(
                            "admission report id was reused for different evidence"
                        )
                    connection.execute("COMMIT")
                    return False
                conflict = connection.execute(
                    "SELECT report_json FROM predictor_admission_reports "
                    "WHERE dataset_id=? AND dataset_content_hash=? AND profile=?",
                    (
                        report.dataset_id,
                        report.dataset_content_hash,
                        report.profile,
                    ),
                ).fetchone()
                if conflict is not None:
                    raise PredictorAdmissionError(
                        "dataset/profile admission decision is not immutable"
                    )
                connection.execute(
                    "INSERT INTO predictor_admission_reports("
                    "report_id,dataset_id,dataset_content_hash,profile,state,"
                    "training_allowed,policy_promotion_allowed,summary_hash,"
                    "report_json,report_hash,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        report.report_id,
                        report.dataset_id,
                        report.dataset_content_hash,
                        report.profile,
                        report.state,
                        int(report.training_allowed),
                        int(report.policy_promotion_allowed),
                        report.summary_hash,
                        raw,
                        self._hash(raw),
                        utc_now(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def report(self, report_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT report_json,report_hash FROM predictor_admission_reports "
                "WHERE report_id=?",
                (report_id,),
            ).fetchone()
        if row is None:
            return None
        raw = str(row["report_json"])
        if self._hash(raw) != row["report_hash"]:
            raise PredictorAdmissionError("stored admission report failed integrity")
        return json.loads(raw)

    def record_receipt(self, receipt: FrozenPredictionReceipt) -> bool:
        value = receipt.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT receipt_json FROM frozen_prediction_receipts "
                    "WHERE receipt_id=?",
                    (receipt.receipt_id,),
                ).fetchone()
                if existing is not None:
                    if not self._same(existing["receipt_json"], value):
                        raise PredictorAdmissionError(
                            "prediction receipt id was reused for different content"
                        )
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO frozen_prediction_receipts("
                    "receipt_id,feature_fingerprint,admission_report_id,status,"
                    "learned,routing_authority,receipt_json,receipt_hash,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        receipt.receipt_id,
                        receipt.feature_fingerprint,
                        receipt.admission_report_id,
                        receipt.status,
                        int(receipt.learned),
                        int(receipt.routing_authority),
                        raw,
                        self._hash(raw),
                        utc_now(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def receipt(self, receipt_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT receipt_json,receipt_hash FROM frozen_prediction_receipts "
                "WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            return None
        raw = str(row["receipt_json"])
        if self._hash(raw) != row["receipt_hash"]:
            raise PredictorAdmissionError(
                "stored prediction receipt failed integrity"
            )
        return json.loads(raw)

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                "predictor_admission_reports": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM predictor_admission_reports"
                    ).fetchone()[0]
                ),
                "calibration_candidates": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM predictor_admission_reports "
                        "WHERE training_allowed=1"
                    ).fetchone()[0]
                ),
                "frozen_prediction_receipts": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM frozen_prediction_receipts"
                    ).fetchone()[0]
                ),
                "authoritative_learned_predictions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM frozen_prediction_receipts "
                        "WHERE learned=1 OR routing_authority=1"
                    ).fetchone()[0]
                ),
            }


__all__ = ["PredictorAdmissionStore"]
