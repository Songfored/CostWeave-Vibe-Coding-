from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from costweave.adapters.simulated_media import (
    PreviewApprovalRequired,
    SimulatedMediaAdapter,
)
from costweave.control_kernel import ControlKernel
from costweave.domain import RunRequest
from costweave.media.bibles import BibleBuilder
from costweave.media.continuity import ContinuityValidator
from costweave.media.intent import MediaIntentAnalyzer
from costweave.media.media_cost import MediaCostLedger
from costweave.media.media_dag import MediaProjectBuilder
from costweave.media.prompt_compiler import PromptCompiler
from costweave.media.shot_contract import ShotContract


VIDEO_GOAL = "制作30秒AI电影，主角必须保持一致，包含配音"
IMAGE_GOAL = "生成一张品牌海报，LOGO必须保持不变"


def profile(goal: str = VIDEO_GOAL):
    return MediaIntentAnalyzer().analyze(goal)


def project(goal: str = VIDEO_GOAL, run_id: str = "run_media_test"):
    demand = profile(goal)
    return MediaProjectBuilder().build(
        run_id=run_id, goal=goal, profile=demand
    )


class MediaIntentTests(unittest.TestCase):
    def test_non_media_goal_stays_out_of_media_runtime(self):
        result = profile("分析一份产品需求并输出风险清单")
        self.assertFalse(result.is_media)
        self.assertEqual("none", result.kind)

    def test_single_image_uses_lightweight_workflow(self):
        result = profile(IMAGE_GOAL)
        self.assertEqual("single_image", result.kind)
        self.assertFalse(result.requires_film_dag)
        self.assertEqual(1, result.shot_count)

    def test_image_set_is_recognized(self):
        self.assertEqual(
            "image_set", profile("制作一组多张系列插画").kind
        )

    def test_thirty_second_video_gets_six_to_ten_shots(self):
        result = profile()
        self.assertEqual(30.0, result.duration_seconds)
        self.assertTrue(6 <= result.shot_count <= 10)

    def test_minutes_are_converted_to_seconds(self):
        self.assertEqual(
            90.0, profile("制作1.5分钟动画短片").duration_seconds
        )

    def test_long_film_is_capped_at_ten_shots(self):
        result = profile("制作3分钟电影")
        self.assertEqual("film", result.kind)
        self.assertEqual(10, result.shot_count)

    def test_short_video_has_at_least_six_shots(self):
        self.assertEqual(6, profile("制作5秒短视频").shot_count)

    def test_format_sound_and_strategy_are_extracted(self):
        result = profile("全自动制作30秒4K竖屏视频，含音乐和30fps")
        self.assertEqual("3840x2160", result.resolution)
        self.assertEqual("9:16", result.aspect_ratio)
        self.assertEqual(30, result.fps)
        self.assertTrue(result.sound_required)
        self.assertEqual("auto_director", result.strategy)

    def test_manual_strategy_is_extracted(self):
        self.assertEqual(
            "manual_director", profile("逐镜头手动制作30秒影片").strategy
        )

    def test_locked_requirements_capture_constraints(self):
        result = profile("制作短片。主角必须穿红衣；品牌LOGO不得改变")
        self.assertEqual(2, len(result.locked_requirements))


class BibleContractAndPromptTests(unittest.TestCase):
    def setUp(self):
        self.project = project()
        self.shot = self.project.shots[0]

    def test_bible_ids_are_deterministic(self):
        builder = BibleBuilder()
        first = builder.build(VIDEO_GOAL, profile())
        second = builder.build(VIDEO_GOAL, profile())
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_bibles_cover_character_location_and_assets(self):
        bibles = self.project.bibles
        self.assertGreaterEqual(len(bibles.characters), 1)
        self.assertGreaterEqual(len(bibles.locations), 1)
        self.assertEqual(len(bibles.characters), len(bibles.assets.assets))

    def test_every_shot_carries_locked_intent_hash(self):
        hashes = {item.locked_intent_hash for item in self.project.shots}
        self.assertEqual(1, len(hashes))

    def test_contract_rejects_nonpositive_duration(self):
        values = {
            key: value
            for key, value in self.shot.to_dict().items()
            if key != "locked_intent_hash"
        }
        values["duration_seconds"] = 0
        with self.assertRaises(ValueError):
            ShotContract(**values)

    def test_contract_rejects_invalid_resolution(self):
        values = {
            key: value
            for key, value in self.shot.to_dict().items()
            if key != "locked_intent_hash"
        }
        values["resolution"] = "HD"
        with self.assertRaises(ValueError):
            ShotContract(**values)

    def test_prompt_inherits_all_reference_bibles(self):
        prompt = PromptCompiler().compile(
            self.shot, self.project.bibles
        )
        self.assertIn(self.project.bibles.film.bible_id, prompt.inherited_refs)
        self.assertIn(
            self.project.bibles.assets.registry_id, prompt.inherited_refs
        )

    def test_prompt_compiler_cannot_override_locked_intent(self):
        with self.assertRaises(ValueError):
            PromptCompiler().compile(
                self.shot,
                self.project.bibles,
                locked_overrides=("different intent",),
            )

    def test_prompt_semantic_diff_preserves_locked_fields(self):
        prompt = PromptCompiler().compile(
            self.shot, self.project.bibles
        )
        self.assertFalse(prompt.semantic_diff["locked_changed"])
        self.assertEqual(
            self.shot.locked_intent_hash, prompt.locked_intent_hash
        )

    def test_prompt_parameters_match_shot_contract(self):
        prompt = PromptCompiler().compile(
            self.shot, self.project.bibles
        )
        self.assertEqual(self.shot.resolution, prompt.parameters["resolution"])
        self.assertEqual(self.shot.fps, prompt.parameters["fps"])

    def test_prompt_refinement_has_cost_aware_stop_rule(self):
        compiler = PromptCompiler()
        self.assertTrue(
            compiler.should_refine(
                revision=1,
                max_revisions=3,
                marginal_gain=.04,
                added_cost=.01,
                remaining_budget=.02,
            )
        )
        self.assertFalse(
            compiler.should_refine(
                revision=2,
                max_revisions=3,
                marginal_gain=.01,
                added_cost=.01,
                remaining_budget=.02,
            )
        )


class MediaDagAdapterAndValidationTests(unittest.TestCase):
    def setUp(self):
        self.project = project()
        self.shot = self.project.shots[0]
        self.adapter = SimulatedMediaAdapter()

    def test_video_shot_durations_sum_to_requested_duration(self):
        self.assertAlmostEqual(
            30.0,
            sum(item.duration_seconds for item in self.project.shots),
            places=3,
        )

    def test_shot_ids_are_unique_across_runs(self):
        other = project(run_id="run_media_other")
        self.assertTrue(
            {item.shot_id for item in self.project.shots}.isdisjoint(
                {item.shot_id for item in other.shots}
            )
        )

    def test_dag_exposes_multiple_parallel_ready_shots(self):
        ready = MediaProjectBuilder.ready_shots(self.project.shots, set())
        self.assertGreater(len(ready), 1)

    def test_dependencies_block_only_dependent_shots(self):
        dependent = next(
            item for item in self.project.shots
            if item.reference_dependencies
        )
        ready = MediaProjectBuilder.ready_shots(self.project.shots, set())
        self.assertNotIn(dependent.shot_id, ready)
        completed = set(dependent.reference_dependencies)
        self.assertIn(
            dependent.shot_id,
            MediaProjectBuilder.ready_shots(
                self.project.shots, completed
            ),
        )

    def test_final_render_requires_preview_approval(self):
        with self.assertRaises(PreviewApprovalRequired):
            self.adapter.render(self.shot, stage="final")

    def test_adapter_is_metadata_only(self):
        artifact = self.adapter.render(self.shot, stage="animatic")
        self.assertTrue(artifact["simulated"])
        self.assertFalse(artifact["contains_real_media"])

    def test_identity_failure_is_detected(self):
        artifact = self.adapter.render(
            self.shot,
            stage="final",
            preview_approved=True,
            inject_failure="identity",
        )
        result = ContinuityValidator().validate(self.shot, artifact)
        self.assertIn("identity_drift", result.defects)

    def test_boundary_failure_marks_neighbors_suspect(self):
        shot = next(
            item for item in self.project.shots
            if item.reference_dependencies
        )
        artifact = self.adapter.render(
            shot,
            stage="final",
            preview_approved=True,
            inject_failure="boundary",
        )
        result = ContinuityValidator().validate(
            shot,
            artifact,
            previous_artifact={"end_state": "expected"},
            next_shot_id=self.project.shots[shot.sequence].shot_id,
        )
        self.assertIn("boundary_discontinuity", result.defects)
        self.assertGreaterEqual(len(result.suspect_shots), 2)

    def test_nonboundary_failure_only_invalidates_failed_shot(self):
        validator = ContinuityValidator()
        affected = validator.affected_neighbors(
            self.project.shots,
            self.project.shots[2].shot_id,
            boundary_changed=False,
        )
        self.assertEqual({self.project.shots[2].shot_id}, affected)


class MediaCostTests(unittest.TestCase):
    def test_cost_ledger_is_idempotent(self):
        ledger = MediaCostLedger()
        first = ledger.add(
            project_id="media_x",
            shot_id="shot_x",
            category="final_render",
            amount=.2,
            seconds=4,
            accepted=True,
        )
        second = ledger.add(
            project_id="media_x",
            shot_id="shot_x",
            category="final_render",
            amount=.2,
            seconds=4,
            accepted=True,
        )
        self.assertEqual(first, second)
        self.assertEqual(1, len(ledger.entries()))

    def test_cost_identity_conflict_is_rejected(self):
        ledger = MediaCostLedger()
        ledger.add(
            project_id="media_x",
            shot_id="shot_x",
            category="final_render",
            amount=.2,
        )
        with self.assertRaises(ValueError):
            ledger.add(
                project_id="media_x",
                shot_id="shot_x",
                category="final_render",
                amount=.3,
            )

    def test_cost_summary_reports_accepted_unit_costs(self):
        ledger = MediaCostLedger()
        ledger.add(
            project_id="media_x",
            shot_id="shot_x",
            category="final_render",
            amount=.2,
            seconds=4,
            accepted=True,
        )
        summary = ledger.summary()
        self.assertEqual(.05, summary["cost_per_accepted_second"])
        self.assertEqual(.2, summary["cost_per_accepted_shot"])


class MediaRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.kernel = ControlKernel(
            os.path.join(self.temp.name, "runtime.sqlite3")
        )

    def tearDown(self):
        self.temp.cleanup()

    def create(self, goal: str, key: str):
        record, _ = self.kernel.create_run(
            RunRequest(goal), idempotency_key=key
        )
        return record

    def test_supervised_project_has_no_final_before_approval(self):
        record = self.create(VIDEO_GOAL, "media-runtime-0001")
        project_data = self.kernel.ensure_media_project(record)
        self.assertEqual(
            0, project_data["render_summary"]["final_render_attempts"]
        )
        self.assertEqual("awaiting_preview_approval", project_data["status"])

    def test_project_approval_renders_and_validates_all_shots(self):
        record = self.create(VIDEO_GOAL, "media-runtime-0002")
        before = self.kernel.ensure_media_project(record)
        after = self.kernel.approve_media_project(str(record.id))
        self.assertEqual("completed", after["status"])
        self.assertEqual(
            len(before["shots"]),
            after["render_summary"]["accepted_final_shots"],
        )

    def test_failed_shot_is_rerendered_locally(self):
        record = self.create(VIDEO_GOAL, "media-runtime-0003")
        before = self.kernel.ensure_media_project(record)
        failed_id = before["shots"][2]["shot_id"]
        after = self.kernel.media_runtime.approve_project(
            str(record.id),
            fail_shot_id=failed_id,
            failure_kind="identity",
        )
        counts = {
            item["shot_id"]: len(
                [
                    attempt for attempt in item["attempts"]
                    if attempt["stage"] == "final"
                ]
            )
            for item in after["shots"]
        }
        self.assertEqual(2, counts[failed_id])
        self.assertTrue(
            all(count == 1 for shot_id, count in counts.items()
                if shot_id != failed_id)
        )

    def test_accepted_cost_covers_requested_seconds(self):
        record = self.create(VIDEO_GOAL, "media-runtime-0004")
        self.kernel.ensure_media_project(record)
        after = self.kernel.approve_media_project(str(record.id))
        self.assertEqual(30.0, after["cost"]["accepted_seconds"])
        self.assertIsNotNone(
            after["cost"]["cost_per_accepted_second"]
        )

    def test_simple_image_skips_preview_gate(self):
        record = self.create(IMAGE_GOAL, "media-runtime-0005")
        data = self.kernel.ensure_media_project(record)
        self.assertEqual("single_image", data["project"]["workflow_kind"])
        self.assertEqual(1, data["render_summary"]["accepted_final_shots"])
        self.assertEqual(0, data["render_summary"]["animatic_attempts"])

    def test_ensure_project_is_idempotent(self):
        record = self.create(VIDEO_GOAL, "media-runtime-0006")
        first = self.kernel.ensure_media_project(record)
        second = self.kernel.ensure_media_project(record)
        self.assertEqual(first, second)

    def test_feature_flag_can_disable_media_project_creation(self):
        record = self.create(VIDEO_GOAL, "media-runtime-0007")
        with patch.dict(
            os.environ, {"COSTWEAVE_MEDIA_WORKFLOW": "0"}
        ):
            from costweave.compat_v4 import media_workflow_enabled

            self.assertFalse(media_workflow_enabled())
        self.assertIsNone(
            self.kernel.media_project("run_that_does_not_exist")
        )


if __name__ == "__main__":
    unittest.main()
