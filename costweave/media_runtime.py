"""Persistent, metadata-only media production runtime."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .adapters.simulated_media import SimulatedMediaAdapter
from .contracts_v4 import utc_now
from .media.continuity import ContinuityValidator
from .media.intent import MediaIntentAnalyzer
from .media.media_cost import MediaCostEntry, MediaCostLedger
from .media.media_dag import MediaProject, MediaProjectBuilder
from .media.prompt_compiler import PromptCompiler
from .media.shot_contract import ShotContract


class MediaRuntime:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.intent = MediaIntentAnalyzer()
        self.builder = MediaProjectBuilder()
        self.compiler = PromptCompiler()
        self.adapter = SimulatedMediaAdapter()
        self.validator = ContinuityValidator()
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

    def ensure_project(
        self, *, run_id: str, goal: str
    ) -> dict[str, Any] | None:
        existing = self.project(run_id)
        if existing is not None:
            return existing
        profile = self.intent.analyze(goal)
        if not profile.is_media:
            return None
        project = self.builder.build(
            run_id=run_id, goal=goal, profile=profile
        )
        now = utc_now()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO media_projects(project_id,run_id,kind,"
                    "strategy,status,profile_json,project_json,revision,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        project.project_id,
                        run_id,
                        profile.kind,
                        profile.strategy,
                        project.status,
                        json.dumps(
                            profile.to_dict(),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        json.dumps(
                            project.to_dict(),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        project.revision,
                        now,
                        now,
                    ),
                )
                self._persist_bibles(connection, project)
                for shot in project.shots:
                    connection.execute(
                        "INSERT INTO shot_contracts(project_id,shot_id,"
                        "sequence,status,approval_status,contract_json,"
                        "locked_intent_hash,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            project.project_id,
                            shot.shot_id,
                            shot.sequence,
                            "planned",
                            (
                                "not_required"
                                if profile.strategy == "auto_director"
                                or not profile.requires_film_dag
                                else "pending"
                            ),
                            json.dumps(
                                shot.to_dict(),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            shot.locked_intent_hash,
                            now,
                            now,
                        ),
                    )
                    prompt = self.compiler.compile(
                        shot, project.bibles
                    )
                    connection.execute(
                        "INSERT INTO prompt_revisions(prompt_id,project_id,"
                        "shot_id,revision,target_adapter,"
                        "locked_intent_hash,semantic_drift,prompt_json,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            prompt.prompt_id,
                            project.project_id,
                            shot.shot_id,
                            prompt.revision,
                            prompt.target_adapter,
                            prompt.locked_intent_hash,
                            int(
                                bool(
                                    prompt.semantic_diff[
                                        "locked_changed"
                                    ]
                                )
                            ),
                            json.dumps(
                                prompt.to_dict(),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            now,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        ledger = MediaCostLedger()
        ledger.add(
            project_id=project.project_id,
            shot_id=None,
            category="storyboard",
            amount=0.004 * len(project.shots),
        )
        self._persist_ledger(ledger.entries())
        if profile.requires_film_dag:
            for stage in ("keyframe", "animatic"):
                with ThreadPoolExecutor(
                    max_workers=min(4, len(project.shots)),
                    thread_name_prefix=f"media-{stage}",
                ) as pool:
                    list(
                        pool.map(
                            lambda shot: self._render_stage(
                                project, shot, stage
                            ),
                            project.shots,
                        )
                    )
            if profile.strategy == "auto_director":
                self.approve_project(run_id)
        else:
            for shot in project.shots:
                self._render_final(project, shot, approved=True)
        return self.project(run_id)

    @staticmethod
    def _persist_bibles(
        connection: sqlite3.Connection, project: MediaProject
    ) -> None:
        bundle = project.bibles
        records = [
            (
                "creative_brief",
                bundle.brief.brief_id,
                1,
                bundle.brief.to_dict(),
                bundle.film.locked_intent_hash,
            ),
            (
                "film",
                bundle.film.bible_id,
                bundle.film.revision,
                bundle.film.to_dict(),
                bundle.film.locked_intent_hash,
            ),
            *[
                (
                    "character",
                    item.character_id,
                    item.revision,
                    item.to_dict(),
                    bundle.film.locked_intent_hash,
                )
                for item in bundle.characters
            ],
            *[
                (
                    "location",
                    item.location_id,
                    item.revision,
                    item.to_dict(),
                    bundle.film.locked_intent_hash,
                )
                for item in bundle.locations
            ],
            (
                "asset_registry",
                bundle.assets.registry_id,
                bundle.assets.revision,
                bundle.assets.to_dict(),
                bundle.film.locked_intent_hash,
            ),
        ]
        for bible_type, bible_id, revision, content, lock_hash in records:
            connection.execute(
                "INSERT INTO media_bible_revisions(project_id,bible_type,"
                "bible_id,revision,content_json,locked_intent_hash,"
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    project.project_id,
                    bible_type,
                    bible_id,
                    revision,
                    json.dumps(
                        content, ensure_ascii=False, sort_keys=True
                    ),
                    lock_hash,
                    utc_now(),
                ),
            )

    def _render_stage(
        self,
        project: MediaProject,
        shot: ShotContract,
        stage: str,
        *,
        attempt: int = 1,
    ) -> dict[str, Any]:
        artifact = self.adapter.render(
            shot, stage=stage, attempt=attempt
        )
        self._persist_attempt(
            project.project_id,
            shot.shot_id,
            stage,
            attempt,
            "preview",
            artifact,
        )
        ledger = MediaCostLedger()
        ledger.add(
            project_id=project.project_id,
            shot_id=shot.shot_id,
            category="keyframe" if stage == "keyframe" else "animatic",
            amount=artifact["estimated_cost"],
            seconds=shot.duration_seconds,
            attempt=attempt,
        )
        self._persist_ledger(ledger.entries())
        return artifact

    def _render_final(
        self,
        project: MediaProject,
        shot: ShotContract,
        *,
        approved: bool,
        inject_failure: str | None = None,
    ) -> dict[str, Any]:
        attempt = self._next_attempt(
            project.project_id, shot.shot_id, "final"
        )
        artifact = self.adapter.render(
            shot,
            stage="final",
            attempt=attempt,
            preview_approved=approved,
            inject_failure=inject_failure,
        )
        previous = self._previous_artifact(project, shot)
        next_id = (
            project.shots[shot.sequence].shot_id
            if shot.sequence < len(project.shots)
            else None
        )
        result = self.validator.validate(
            shot,
            artifact,
            previous_artifact=previous,
            next_shot_id=next_id,
        )
        status = "accepted" if result.passed else "rejected"
        self._persist_attempt(
            project.project_id,
            shot.shot_id,
            "final",
            attempt,
            status,
            artifact,
        )
        self._persist_check(project.project_id, result.to_dict())
        ledger = MediaCostLedger()
        ledger.add(
            project_id=project.project_id,
            shot_id=shot.shot_id,
            category=(
                "final_render" if attempt == 1 else "rerender"
            ),
            amount=artifact["estimated_cost"],
            seconds=shot.duration_seconds,
            accepted=result.passed,
            attempt=attempt,
        )
        ledger.add(
            project_id=project.project_id,
            shot_id=shot.shot_id,
            category="continuity_validation",
            amount=0.0015,
            attempt=attempt,
        )
        self._persist_ledger(ledger.entries())
        if not result.passed:
            affected = self.validator.affected_neighbors(
                project.shots,
                shot.shot_id,
                boundary_changed=(
                    "boundary_discontinuity" in result.defects
                ),
            )
            affected.update(result.suspect_shots)
            self._mark_shots(
                project.project_id,
                affected - {shot.shot_id},
                "suspect",
            )
            if attempt <= int(
                shot.generation_policy["max_rerenders"]
            ):
                return self._render_final(
                    project, shot, approved=approved
                )
        else:
            self._mark_shots(
                project.project_id, {shot.shot_id}, "validated"
            )
        return {
            "artifact": artifact,
            "continuity": result.to_dict(),
        }

    def approve_project(
        self,
        run_id: str,
        *,
        idempotency_key: str | None = None,
        fail_shot_id: str | None = None,
        failure_kind: str = "identity",
    ) -> dict[str, Any]:
        with self._lock:
            replay = self._media_command_replay(
                idempotency_key, "approve_project", run_id
            )
            if replay is not None:
                return replay
            result = self._approve_project_once(
                run_id,
                fail_shot_id=fail_shot_id,
                failure_kind=failure_kind,
            )
            self._record_media_command(
                idempotency_key, "approve_project", run_id, result
            )
            return result

    def _approve_project_once(
        self,
        run_id: str,
        *,
        fail_shot_id: str | None = None,
        failure_kind: str = "identity",
    ) -> dict[str, Any]:
        data = self.project(run_id)
        if data is None:
            raise KeyError(run_id)
        project = self._hydrate_project(data)
        with self._connection() as connection:
            connection.execute(
                "UPDATE shot_contracts SET approval_status='approved',"
                "updated_at=? WHERE project_id=?",
                (utc_now(), project.project_id),
            )
        completed = {
            shot.shot_id
            for shot in project.shots
            if self._accepted_final_exists(
                project.project_id, shot.shot_id
            )
        }
        while len(completed) < len(project.shots):
            ready_ids = self.builder.ready_shots(
                project.shots, completed
            )
            if not ready_ids:
                raise RuntimeError("media shot DAG cannot make progress")
            ready = [
                shot for shot in project.shots
                if shot.shot_id in ready_ids
            ]
            with ThreadPoolExecutor(
                max_workers=min(4, len(ready)),
                thread_name_prefix="media-final",
            ) as pool:
                futures = [
                    pool.submit(
                        self._render_final,
                        project,
                        shot,
                        approved=True,
                        inject_failure=(
                            failure_kind
                            if shot.shot_id == fail_shot_id
                            else None
                        ),
                    )
                    for shot in ready
                ]
                for future in futures:
                    future.result()
            completed.update(ready_ids)
        with self._connection() as connection:
            connection.execute(
                "UPDATE media_projects SET status='completed',updated_at=? "
                "WHERE project_id=?",
                (utc_now(), project.project_id),
            )
        return self.project(run_id) or {}

    def approve_shot(
        self,
        run_id: str,
        shot_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        target_id = f"{run_id}:{shot_id}"
        with self._lock:
            replay = self._media_command_replay(
                idempotency_key, "approve_shot", target_id
            )
            if replay is not None:
                return replay
            result = self._approve_shot_once(run_id, shot_id)
            self._record_media_command(
                idempotency_key, "approve_shot", target_id, result
            )
            return result

    def _approve_shot_once(self, run_id: str, shot_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            project_row = connection.execute(
                "SELECT project_id FROM media_projects WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if project_row is None:
                raise KeyError(run_id)
            if connection.execute(
                "SELECT 1 FROM shot_contracts WHERE project_id=? AND shot_id=?",
                (project_row["project_id"], shot_id),
            ).fetchone() is None:
                raise KeyError(shot_id)
        data = self.project(run_id)
        project = self._hydrate_project(data)
        shot = next(item for item in project.shots if item.shot_id == shot_id)
        with self._connection() as connection:
            connection.execute(
                "UPDATE shot_contracts SET approval_status='approved',"
                "updated_at=? WHERE project_id=? AND shot_id=?",
                (utc_now(), project.project_id, shot_id),
            )
        if not self._accepted_final_exists(project.project_id, shot_id):
            self._render_final(project, shot, approved=True)
        return self.shot(run_id, shot_id) or {}

    def _media_command_replay(
        self,
        key: str | None,
        operation: str,
        target_id: str,
    ) -> dict[str, Any] | None:
        if key is None:
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT operation,target_id,result_json FROM media_commands "
                "WHERE idempotency_key=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["target_id"] != target_id:
            raise ValueError("media idempotency key was reused for another command")
        return json.loads(row["result_json"])

    def _record_media_command(
        self,
        key: str | None,
        operation: str,
        target_id: str,
        result: dict[str, Any],
    ) -> None:
        if key is None:
            return
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO media_commands(idempotency_key,operation,target_id,"
                "result_json,completed_at) VALUES(?,?,?,?,?)",
                (
                    key,
                    operation,
                    target_id,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )

    def _hydrate_project(self, data: dict[str, Any]) -> MediaProject:
        from .media.bibles import (
            AssetRegistry,
            BibleBundle,
            CharacterBible,
            CreativeBrief,
            FilmBible,
            LocationBible,
        )
        from .media.intent import MediaDemandProfile

        raw = data["project"]
        bibles = raw["bibles"]
        bundle = BibleBundle(
            CreativeBrief(**bibles["brief"]),
            FilmBible(**bibles["film"]),
            tuple(CharacterBible(**item) for item in bibles["characters"]),
            tuple(LocationBible(**item) for item in bibles["locations"]),
            AssetRegistry(**bibles["assets"]),
        )
        return MediaProject(
            raw["project_id"],
            raw["run_id"],
            MediaDemandProfile(**raw["profile"]),
            bundle,
            tuple(
                ShotContract(
                    **{
                        key: (
                            tuple(value)
                            if key
                            in {
                                "character_refs",
                                "asset_refs",
                                "reference_dependencies",
                                "acceptance_criteria",
                                "locked_intent",
                            }
                            else value
                        )
                        for key, value in item.items()
                        if key != "locked_intent_hash"
                    }
                )
                for item in raw["shots"]
            ),
            raw["workflow_kind"],
            tuple(raw["stages"]),
            raw["status"],
            raw["revision"],
        )

    def _previous_artifact(
        self, project: MediaProject, shot: ShotContract
    ) -> dict[str, Any] | None:
        previous_id = shot.generation_policy.get("boundary_predecessor")
        if previous_id is None and shot.reference_dependencies:
            previous_id = shot.reference_dependencies[-1]
        if previous_id is None:
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT artifact_json FROM media_render_attempts "
                "WHERE project_id=? AND shot_id=? AND stage IN ('final','animatic') "
                "AND status='accepted' ORDER BY "
                "CASE stage WHEN 'final' THEN 0 ELSE 1 END,attempt DESC LIMIT 1",
                (project.project_id, previous_id),
            ).fetchone()
        return json.loads(row["artifact_json"]) if row else None

    def _accepted_final_exists(
        self, project_id: str, shot_id: str
    ) -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM media_render_attempts WHERE project_id=? "
                "AND shot_id=? AND stage='final' AND status='accepted'",
                (project_id, shot_id),
            ).fetchone() is not None

    def _next_attempt(
        self, project_id: str, shot_id: str, stage: str
    ) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt),0) value "
                "FROM media_render_attempts WHERE project_id=? "
                "AND shot_id=? AND stage=?",
                (project_id, shot_id, stage),
            ).fetchone()
        return int(row["value"]) + 1

    def _persist_attempt(
        self,
        project_id: str,
        shot_id: str,
        stage: str,
        attempt: int,
        status: str,
        artifact: dict[str, Any],
    ) -> None:
        attempt_id = "media_attempt_" + hashlib.sha256(
            f"{project_id}:{shot_id}:{stage}:{attempt}".encode()
        ).hexdigest()[:26]
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO media_render_attempts(attempt_id,"
                "project_id,shot_id,stage,attempt,status,simulated,"
                "artifact_json,cost,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    project_id,
                    shot_id,
                    stage,
                    attempt,
                    status,
                    1,
                    json.dumps(
                        artifact, ensure_ascii=False, sort_keys=True
                    ),
                    artifact["estimated_cost"],
                    utc_now(),
                ),
            )

    def _persist_check(
        self, project_id: str, result: dict[str, Any]
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO media_continuity_checks(check_id,"
                "project_id,shot_id,passed,defects_json,"
                "suspect_shots_json,result_json,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    result["check_id"],
                    project_id,
                    result["shot_id"],
                    int(result["passed"]),
                    json.dumps(result["defects"]),
                    json.dumps(result["suspect_shots"]),
                    json.dumps(
                        result, ensure_ascii=False, sort_keys=True
                    ),
                    utc_now(),
                ),
            )

    def _persist_ledger(
        self, entries: list[MediaCostEntry]
    ) -> None:
        with self._connection() as connection:
            for item in entries:
                connection.execute(
                    "INSERT OR IGNORE INTO media_cost_ledger(entry_id,"
                    "project_id,shot_id,category,amount,seconds,accepted,"
                    "attempt,entry_json,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        item.entry_id,
                        item.project_id,
                        item.shot_id,
                        item.category,
                        item.amount,
                        item.seconds,
                        int(item.accepted),
                        item.attempt,
                        json.dumps(
                            item.to_dict(),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        utc_now(),
                    ),
                )

    def _mark_shots(
        self, project_id: str, shot_ids: set[str], status: str
    ) -> None:
        with self._connection() as connection:
            for shot_id in shot_ids:
                connection.execute(
                    "UPDATE shot_contracts SET status=?,updated_at=? "
                    "WHERE project_id=? AND shot_id=?",
                    (status, utc_now(), project_id, shot_id),
                )

    def project(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM media_projects WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["profile"] = json.loads(item.pop("profile_json"))
        item["project"] = json.loads(item.pop("project_json"))
        item["shots"] = self.shots(run_id)
        item["cost"] = self.cost_summary(item["project_id"])
        item["render_summary"] = self.render_summary(
            item["project_id"]
        )
        return item

    def shots(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            project = connection.execute(
                "SELECT project_id FROM media_projects WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if project is None:
                return []
            rows = connection.execute(
                "SELECT * FROM shot_contracts WHERE project_id=? "
                "ORDER BY sequence",
                (project["project_id"],),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["contract"] = json.loads(item.pop("contract_json"))
            item["prompts"] = self.prompts(
                item["project_id"], item["shot_id"]
            )
            item["attempts"] = self.attempts(
                item["project_id"], item["shot_id"]
            )
            result.append(item)
        return result

    def shot(self, run_id: str, shot_id: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self.shots(run_id)
                if item["shot_id"] == shot_id
            ),
            None,
        )

    def prompts(
        self, project_id: str, shot_id: str
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT prompt_json FROM prompt_revisions "
                "WHERE project_id=? AND shot_id=? ORDER BY revision",
                (project_id, shot_id),
            ).fetchall()
        return [json.loads(row["prompt_json"]) for row in rows]

    def attempts(
        self, project_id: str, shot_id: str
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM media_render_attempts "
                "WHERE project_id=? AND shot_id=? "
                "ORDER BY stage,attempt",
                (project_id, shot_id),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["simulated"] = bool(item["simulated"])
            item["artifact"] = json.loads(item.pop("artifact_json"))
            result.append(item)
        return result

    def render_summary(self, project_id: str) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT stage,status,COUNT(*) count "
                "FROM media_render_attempts WHERE project_id=? "
                "GROUP BY stage,status",
                (project_id,),
            ).fetchall()
        result = {
            "keyframe_attempts": 0,
            "animatic_attempts": 0,
            "final_render_attempts": 0,
            "accepted_final_shots": 0,
            "rejected_final_attempts": 0,
        }
        for row in rows:
            attempt_key = (
                "final_render_attempts"
                if row["stage"] == "final"
                else f"{row['stage']}_attempts"
            )
            result[attempt_key] = (
                result.get(attempt_key, 0)
                + int(row["count"])
            )
            if row["stage"] == "final" and row["status"] == "accepted":
                result["accepted_final_shots"] += int(row["count"])
            if row["stage"] == "final" and row["status"] == "rejected":
                result["rejected_final_attempts"] += int(row["count"])
        return result

    def cost_summary(self, project_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT category,amount,seconds,accepted "
                "FROM media_cost_ledger WHERE project_id=?",
                (project_id,),
            ).fetchall()
        total = sum(float(row["amount"]) for row in rows)
        seconds = sum(
            float(row["seconds"])
            for row in rows
            if row["accepted"]
            and row["category"] in {"final_render", "rerender"}
        )
        shots = sum(
            bool(row["accepted"])
            for row in rows
            if row["category"] in {"final_render", "rerender"}
        )
        return {
            "total": round(total, 8),
            "accepted_seconds": round(seconds, 4),
            "accepted_shots": shots,
            "cost_per_accepted_second": (
                round(total / seconds, 8) if seconds else None
            ),
            "cost_per_accepted_shot": (
                round(total / shots, 8) if shots else None
            ),
            "categories": {
                category: round(
                    sum(
                        float(row["amount"])
                        for row in rows
                        if row["category"] == category
                    ),
                    8,
                )
                for category in {
                    row["category"] for row in rows
                }
            },
        }

    def counts(self) -> dict[str, int]:
        tables = (
            "media_projects",
            "media_bible_revisions",
            "shot_contracts",
            "prompt_revisions",
            "media_render_attempts",
            "media_continuity_checks",
            "media_cost_ledger",
        )
        with self._connection() as connection:
            return {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in tables
            }
