"""Persistence for v0.5.6.5 offline policy candidates and evidence."""

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
from .policy_candidate import (
    OfflineBanditEvaluation,
    PolicyCandidate,
    PolicyCandidateError,
    PolicyCard,
    PolicyFreezeAssessment,
)


DELETION_REASON_CODES = frozenset(
    {"user_request", "superseded", "governance_failure", "test_cleanup"}
)


class PolicyCandidateStore:
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

    def record_candidate(self, candidate: PolicyCandidate) -> bool:
        value = candidate.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                source = connection.execute(
                    "SELECT e.predictor_artifact_id,e.offline_evaluation_passed,"
                    "a.dataset_content_hash "
                    "FROM predictor_evaluation_reports e "
                    "JOIN predictor_candidate_artifacts a "
                    "ON a.artifact_id=e.predictor_artifact_id "
                    "WHERE e.report_id=?",
                    (candidate.predictor_evaluation_report_id,),
                ).fetchone()
                if (
                    source is None
                    or source["predictor_artifact_id"]
                    != candidate.predictor_artifact_id
                    or source["dataset_content_hash"]
                    != candidate.dataset_content_hash
                    or not bool(source["offline_evaluation_passed"])
                ):
                    raise PolicyCandidateError(
                        "policy candidate requires a matching persisted "
                        "passing predictor evaluation"
                    )
                tombstone = connection.execute(
                    "SELECT 1 FROM policy_candidate_deletions WHERE candidate_id=?",
                    (candidate.candidate_id,),
                ).fetchone()
                if tombstone is not None:
                    raise PolicyCandidateError(
                        "a deleted policy candidate identity cannot be reused"
                    )
                existing = connection.execute(
                    "SELECT candidate_json FROM policy_candidates "
                    "WHERE candidate_id=?",
                    (candidate.candidate_id,),
                ).fetchone()
                if existing is not None:
                    if not self._same(existing["candidate_json"], value):
                        raise PolicyCandidateError("policy candidate id was reused")
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO policy_candidates("
                    "candidate_id,predictor_artifact_id,"
                    "predictor_evaluation_report_id,dataset_content_hash,status,"
                    "eligible_for_offline_bandit,advisory_only,default_policy,"
                    "routing_authority,may_change_actual_route,"
                    "automatic_promotion_allowed,commercial_claims_allowed,"
                    "candidate_json,candidate_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        candidate.candidate_id,
                        candidate.predictor_artifact_id,
                        candidate.predictor_evaluation_report_id,
                        candidate.dataset_content_hash,
                        candidate.status,
                        int(candidate.eligible_for_offline_bandit),
                        int(candidate.advisory_only),
                        int(candidate.default_policy),
                        int(candidate.routing_authority),
                        int(candidate.may_change_actual_route),
                        int(candidate.automatic_promotion_allowed),
                        int(candidate.commercial_claims_allowed),
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

    def record_evaluation(self, report: OfflineBanditEvaluation) -> bool:
        value = report.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                candidate = connection.execute(
                    "SELECT 1 FROM policy_candidates WHERE candidate_id=?",
                    (report.policy_candidate_id,),
                ).fetchone()
                if candidate is None:
                    raise PolicyCandidateError(
                        "bandit evaluation requires a persisted policy candidate"
                    )
                existing = connection.execute(
                    "SELECT evaluation_json FROM offline_bandit_evaluations "
                    "WHERE evaluation_id=? OR "
                    "(candidate_id=? AND observation_set_hash=?)",
                    (
                        report.evaluation_id,
                        report.policy_candidate_id,
                        report.observation_set_hash,
                    ),
                ).fetchall()
                if existing:
                    if len(existing) != 1 or not self._same(
                        existing[0]["evaluation_json"], value
                    ):
                        raise PolicyCandidateError(
                            "bandit evaluation identity was reused"
                        )
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO offline_bandit_evaluations("
                    "evaluation_id,candidate_id,observation_set_hash,state,"
                    "common_support,counterfactual_conclusion_allowed,"
                    "eligible_for_v057_shadow_review,advisory_only,"
                    "routing_authority,may_change_actual_route,"
                    "automatic_promotion_allowed,commercial_claims_allowed,"
                    "evaluation_json,evaluation_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        report.evaluation_id,
                        report.policy_candidate_id,
                        report.observation_set_hash,
                        report.state,
                        int(report.common_support),
                        int(report.counterfactual_conclusion_allowed),
                        int(report.eligible_for_v057_shadow_review),
                        int(report.advisory_only),
                        int(report.routing_authority),
                        int(report.may_change_actual_route),
                        int(report.automatic_promotion_allowed),
                        int(report.commercial_claims_allowed),
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

    def record_freeze(self, report: PolicyFreezeAssessment) -> bool:
        value = report.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                candidate = connection.execute(
                    "SELECT 1 FROM policy_candidates WHERE candidate_id=?",
                    (report.policy_candidate_id,),
                ).fetchone()
                if candidate is None:
                    raise PolicyCandidateError(
                        "freeze assessment requires a persisted candidate"
                    )
                existing = connection.execute(
                    "SELECT assessment_json FROM policy_freeze_assessments "
                    "WHERE assessment_id=? OR "
                    "(candidate_id=? AND signals_hash=?)",
                    (
                        report.assessment_id,
                        report.policy_candidate_id,
                        report.signals_hash,
                    ),
                ).fetchall()
                if existing:
                    if len(existing) != 1 or not self._same(
                        existing[0]["assessment_json"], value
                    ):
                        raise PolicyCandidateError(
                            "policy freeze identity was reused"
                        )
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO policy_freeze_assessments("
                    "assessment_id,candidate_id,signals_hash,state,frozen,"
                    "advisory_only,routing_authority,may_change_actual_route,"
                    "automatic_promotion_allowed,assessment_json,"
                    "assessment_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        report.assessment_id,
                        report.policy_candidate_id,
                        report.signals_hash,
                        report.state,
                        int(report.frozen),
                        int(report.advisory_only),
                        int(report.routing_authority),
                        int(report.may_change_actual_route),
                        int(report.automatic_promotion_allowed),
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

    def record_card(self, card: PolicyCard) -> bool:
        value = card.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                links = connection.execute(
                    "SELECT "
                    "(SELECT candidate_id FROM offline_bandit_evaluations "
                    "WHERE evaluation_id=?) AS evaluation_candidate_id,"
                    "(SELECT candidate_id FROM policy_freeze_assessments "
                    "WHERE assessment_id=?) AS freeze_candidate_id,"
                    "(SELECT candidate_id FROM policy_candidates "
                    "WHERE candidate_id=?) AS candidate_id",
                    (
                        card.bandit_evaluation_id,
                        card.freeze_assessment_id,
                        card.policy_candidate_id,
                    ),
                ).fetchone()
                if (
                    links is None
                    or links["candidate_id"] != card.policy_candidate_id
                    or links["evaluation_candidate_id"] != card.policy_candidate_id
                    or links["freeze_candidate_id"] != card.policy_candidate_id
                ):
                    raise PolicyCandidateError(
                        "policy card requires persisted matching inputs"
                    )
                existing = connection.execute(
                    "SELECT card_json FROM policy_cards WHERE card_id=? OR "
                    "(candidate_id=? AND bandit_evaluation_id=? "
                    "AND freeze_assessment_id=?)",
                    (
                        card.card_id,
                        card.policy_candidate_id,
                        card.bandit_evaluation_id,
                        card.freeze_assessment_id,
                    ),
                ).fetchall()
                if existing:
                    if len(existing) != 1 or not self._same(
                        existing[0]["card_json"], value
                    ):
                        raise PolicyCandidateError("policy card identity was reused")
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO policy_cards("
                    "card_id,candidate_id,bandit_evaluation_id,"
                    "freeze_assessment_id,status,eligible_for_v057_review,"
                    "advisory_only,routing_authority,may_change_actual_route,"
                    "automatic_promotion_allowed,commercial_claims_allowed,"
                    "card_json,card_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        card.card_id,
                        card.policy_candidate_id,
                        card.bandit_evaluation_id,
                        card.freeze_assessment_id,
                        card.status,
                        int(card.eligible_for_v057_review),
                        int(card.advisory_only),
                        int(card.routing_authority),
                        int(card.may_change_actual_route),
                        int(card.automatic_promotion_allowed),
                        int(card.commercial_claims_allowed),
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

    def _read(
        self,
        *,
        table: str,
        identifier_column: str,
        identifier: str,
        json_column: str,
        hash_column: str,
    ) -> dict[str, Any] | None:
        allowed = {
            ("policy_candidates", "candidate_id", "candidate_json", "candidate_hash"),
            (
                "offline_bandit_evaluations",
                "evaluation_id",
                "evaluation_json",
                "evaluation_hash",
            ),
            (
                "policy_freeze_assessments",
                "assessment_id",
                "assessment_json",
                "assessment_hash",
            ),
            ("policy_cards", "card_id", "card_json", "card_hash"),
        }
        if (table, identifier_column, json_column, hash_column) not in allowed:
            raise PolicyCandidateError("unsupported policy record type")
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {json_column},{hash_column} FROM {table} "
                f"WHERE {identifier_column}=?",
                (identifier,),
            ).fetchone()
        if row is None:
            return None
        raw = str(row[json_column])
        if self._hash(raw) != row[hash_column]:
            raise PolicyCandidateError("stored policy record failed integrity")
        return json.loads(raw)

    def candidate(self, candidate_id: str) -> dict[str, Any] | None:
        return self._read(
            table="policy_candidates",
            identifier_column="candidate_id",
            identifier=candidate_id,
            json_column="candidate_json",
            hash_column="candidate_hash",
        )

    def evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        return self._read(
            table="offline_bandit_evaluations",
            identifier_column="evaluation_id",
            identifier=evaluation_id,
            json_column="evaluation_json",
            hash_column="evaluation_hash",
        )

    def freeze(self, assessment_id: str) -> dict[str, Any] | None:
        return self._read(
            table="policy_freeze_assessments",
            identifier_column="assessment_id",
            identifier=assessment_id,
            json_column="assessment_json",
            hash_column="assessment_hash",
        )

    def card(self, card_id: str) -> dict[str, Any] | None:
        return self._read(
            table="policy_cards",
            identifier_column="card_id",
            identifier=card_id,
            json_column="card_json",
            hash_column="card_hash",
        )

    def export_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        candidate = self.candidate(candidate_id)
        if candidate is None:
            return None
        with self._connection() as connection:
            references = {
                "evaluations": connection.execute(
                    "SELECT evaluation_id FROM offline_bandit_evaluations "
                    "WHERE candidate_id=? ORDER BY created_at,evaluation_id",
                    (candidate_id,),
                ).fetchall(),
                "freezes": connection.execute(
                    "SELECT assessment_id FROM policy_freeze_assessments "
                    "WHERE candidate_id=? ORDER BY created_at,assessment_id",
                    (candidate_id,),
                ).fetchall(),
                "cards": connection.execute(
                    "SELECT card_id FROM policy_cards WHERE candidate_id=? "
                    "ORDER BY created_at,card_id",
                    (candidate_id,),
                ).fetchall(),
            }
        return {
            "candidate": candidate,
            "evaluations": [
                self.evaluation(row["evaluation_id"])
                for row in references["evaluations"]
            ],
            "freeze_assessments": [
                self.freeze(row["assessment_id"])
                for row in references["freezes"]
            ],
            "policy_cards": [
                self.card(row["card_id"]) for row in references["cards"]
            ],
        }

    def delete_candidate(self, candidate_id: str, *, reason_code: str) -> bool:
        if reason_code not in DELETION_REASON_CODES:
            raise PolicyCandidateError("policy deletion reason is invalid")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_tombstone = connection.execute(
                    "SELECT reason_code FROM policy_candidate_deletions "
                    "WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if existing_tombstone is not None:
                    if existing_tombstone["reason_code"] != reason_code:
                        raise PolicyCandidateError(
                            "policy deletion reason cannot be rewritten"
                        )
                    connection.execute("COMMIT")
                    return False
                candidate = connection.execute(
                    "SELECT candidate_hash FROM policy_candidates "
                    "WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if candidate is None:
                    connection.execute("COMMIT")
                    return False
                hashes: list[str] = [candidate["candidate_hash"]]
                child_count = 0
                for table, hash_column in (
                    ("offline_bandit_evaluations", "evaluation_hash"),
                    ("policy_freeze_assessments", "assessment_hash"),
                    ("policy_cards", "card_hash"),
                ):
                    rows = connection.execute(
                        f"SELECT {hash_column} FROM {table} WHERE candidate_id=? "
                        f"ORDER BY {hash_column}",
                        (candidate_id,),
                    ).fetchall()
                    hashes.extend(str(row[hash_column]) for row in rows)
                    child_count += len(rows)
                bundle_hash = self._hash(canonical_json(sorted(hashes)))
                connection.execute(
                    "INSERT INTO policy_candidate_deletions("
                    "candidate_id,candidate_hash,bundle_hash,"
                    "deleted_record_count,reason_code,deleted_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        candidate_id,
                        candidate["candidate_hash"],
                        bundle_hash,
                        child_count + 1,
                        reason_code,
                        utc_now(),
                    ),
                )
                connection.execute(
                    "DELETE FROM policy_candidates WHERE candidate_id=?",
                    (candidate_id,),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def deletion(self, candidate_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM policy_candidate_deletions WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                "policy_candidates": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM policy_candidates"
                    ).fetchone()[0]
                ),
                "offline_bandit_evaluations": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM offline_bandit_evaluations"
                    ).fetchone()[0]
                ),
                "bandit_common_support_reports": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM offline_bandit_evaluations "
                        "WHERE common_support=1"
                    ).fetchone()[0]
                ),
                "counterfactual_conclusions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM offline_bandit_evaluations "
                        "WHERE counterfactual_conclusion_allowed=1"
                    ).fetchone()[0]
                ),
                "v057_shadow_review_candidates": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM policy_cards "
                        "WHERE eligible_for_v057_review=1"
                    ).fetchone()[0]
                ),
                "policy_freeze_assessments": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM policy_freeze_assessments"
                    ).fetchone()[0]
                ),
                "frozen_policy_candidates": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM policy_freeze_assessments "
                        "WHERE frozen=1"
                    ).fetchone()[0]
                ),
                "policy_cards": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM policy_cards"
                    ).fetchone()[0]
                ),
                "policy_candidate_deletions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM policy_candidate_deletions"
                    ).fetchone()[0]
                ),
                "policy_candidate_route_interventions": int(
                    connection.execute(
                        "SELECT "
                        "(SELECT COUNT(*) FROM policy_candidates "
                        "WHERE routing_authority=1 OR may_change_actual_route=1) + "
                        "(SELECT COUNT(*) FROM offline_bandit_evaluations "
                        "WHERE routing_authority=1 OR may_change_actual_route=1) + "
                        "(SELECT COUNT(*) FROM policy_freeze_assessments "
                        "WHERE routing_authority=1 OR may_change_actual_route=1) + "
                        "(SELECT COUNT(*) FROM policy_cards "
                        "WHERE routing_authority=1 OR may_change_actual_route=1)"
                    ).fetchone()[0]
                ),
                "policy_production_promotions": 0,
            }


__all__ = ["DELETION_REASON_CODES", "PolicyCandidateStore"]
