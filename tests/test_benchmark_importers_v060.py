"""Adversarial tests for v0.6.0 benchmark and trace import boundaries."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from costweave.benchmarks.importers import (
    BenchmarkImportError,
    FieldMappingV1,
    import_csv,
    import_external_decisions,
    import_jsonl,
    llmrouterbench_mapping,
)
from costweave.intelligence.decision_trace import DecisionTraceV2


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class BenchmarkImporterV060Tests(unittest.TestCase):
    @staticmethod
    def _row(*, model: str, partition: str = "test", case: str = "case-1", prompt: str = "prompt-1") -> dict[str, object]:
        return {
            "source_record_id": f"{case}-{model}",
            "case_id": case,
            "partition": partition,
            "task_family": "code",
            "prompt": prompt,
            "prediction": f"answer-{model}",
            "model_id": model,
            "quality_score": 0.8,
            "accepted": True,
            "input_tokens": 10,
            "output_tokens": 20,
            "cost_usd": 0.01,
            "latency_ms": 100,
            "source_dataset": "fixture",
            "source_version": "1",
            "price_snapshot_id": "prices-1",
            "evidence_level": "E0-simulated",
            "observed_at": "2026-08-09T12:00:00Z",
        }

    @staticmethod
    def _mapping() -> FieldMappingV1:
        return FieldMappingV1(schema_version=None)

    def _write_jsonl(self, rows: list[object], *, encoding: str = "utf-8") -> Path:
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        path = Path(temp.name)
        temp.close()
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding=encoding)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_jsonl_hashes_raw_prompt_and_prediction_and_does_not_retain_them(self):
        path = self._write_jsonl([self._row(model="model-a"), self._row(model="model-b")])
        records = import_jsonl(path, mapping=self._mapping())
        self.assertEqual(2, len(records))
        self.assertEqual(_digest("prompt-1"), records[0].prompt_hash)
        self.assertNotIn("prompt", records[0].to_dict())
        self.assertNotIn("prediction", records[0].to_dict())
        self.assertEqual(_digest("answer-model-a"), next(item for item in records if item.model_id == "model-a").response_hash)

    def test_csv_accepts_bom_and_quoted_newline_without_code_execution(self):
        rows = [self._row(model="model-a"), self._row(model="model-b")]
        header = list(rows[0])
        import csv
        import io

        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
        path = Path(temp.name)
        temp.close()
        path.write_text("\ufeff" + output.getvalue(), encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        records = import_csv(path, mapping=self._mapping())
        self.assertEqual(2, len(records))

    def test_missing_measurement_is_not_silently_defaulted_to_zero(self):
        row = self._row(model="model-a")
        del row["cost_usd"]
        path = self._write_jsonl([row, self._row(model="model-b")])
        with self.assertRaises(BenchmarkImportError):
            import_jsonl(path, mapping=self._mapping())

    def test_boolean_parser_is_strict(self):
        row_a = self._row(model="model-a")
        row_b = self._row(model="model-b")
        row_a["accepted"] = "yes"
        path = self._write_jsonl([row_a, row_b])
        with self.assertRaises(BenchmarkImportError):
            import_jsonl(path, mapping=self._mapping())

    def test_bad_json_utf8_and_size_are_rejected(self):
        bad_json = self._write_jsonl([])
        bad_json.write_text("{not-json}\n", encoding="utf-8")
        with self.assertRaises(BenchmarkImportError):
            import_jsonl(bad_json, mapping=self._mapping())
        bad_utf8 = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        bad_utf8_path = Path(bad_utf8.name)
        bad_utf8.close()
        bad_utf8_path.write_bytes(b"\xff\xfe")
        self.addCleanup(lambda: bad_utf8_path.unlink(missing_ok=True))
        with self.assertRaises(BenchmarkImportError):
            import_jsonl(bad_utf8_path, mapping=self._mapping())
        with patch("costweave.benchmarks.importers.MAX_IMPORT_BYTES", 1):
            with self.assertRaises(BenchmarkImportError):
                import_jsonl(self._write_jsonl([self._row(model="model-a")]), mapping=self._mapping())

    def test_duplicate_and_partition_leakage_are_rejected_by_contract(self):
        first = self._row(model="model-a", partition="train")
        second = self._row(model="model-b", partition="test")
        path = self._write_jsonl([first, second])
        with self.assertRaises(BenchmarkImportError):
            import_jsonl(path, mapping=self._mapping())
        duplicate = self._write_jsonl([self._row(model="model-a"), self._row(model="model-a")])
        with self.assertRaises(BenchmarkImportError):
            import_jsonl(duplicate, mapping=self._mapping())

    def test_missing_source_id_is_deterministic_but_line_sensitive(self):
        first = self._row(model="model-a")
        second = self._row(model="model-b")
        first.pop("source_record_id")
        second.pop("source_record_id")
        path = self._write_jsonl([first, second])
        records = import_jsonl(path, mapping=self._mapping())
        self.assertEqual(records, import_jsonl(path, mapping=self._mapping()))
        self.assertNotEqual(records[0].source_record_id, records[1].source_record_id)

    def test_mapping_rejects_duplicate_source_columns_and_llmrouterbench_is_explicit(self):
        with self.assertRaises(ValueError):
            FieldMappingV1(prompt="same", prompt_hash="same")
        mapping = llmrouterbench_mapping()
        self.assertEqual("task_id", mapping.case_id)
        self.assertIsNone(mapping.partition)
        self.assertEqual("origin_query", mapping.prompt)
        self.assertEqual("prediction", mapping.prediction)
        self.assertIsNone(mapping.prompt_hash)


class ExternalDecisionImporterV060Tests(unittest.TestCase):
    @staticmethod
    def _trace(project_id: str = "project-a", case_id: str = "case-1") -> DecisionTraceV2:
        return DecisionTraceV2(
            project_id=project_id,
            case_id=case_id,
            task_belief_id="task_belief_" + "a" * 32,
            system_belief_id="system_belief_" + "b" * 32,
            candidate_strategy_ids=("strategy-a", "strategy-b"),
            candidate_probabilities={"strategy-a": 0.7, "strategy-b": 0.3},
            selected_strategy_id="strategy-a",
            meta_action="fast",
            reason_codes=("recorded_external",),
            evidence_ids=("evidence-1",),
            policy_id="external-policy",
            policy_version="1",
            created_at="2026-08-09T12:00:00Z",
        )

    def test_external_trace_round_trip_and_project_normalization(self):
        trace = self._trace("Project-A")
        imported = import_external_decisions([trace.to_dict()], project_id="project-a")
        self.assertEqual((trace,), imported)

    def test_external_trace_rejects_duplicate_invalid_and_mixed_projects(self):
        first = self._trace("project-a", "case-1").to_dict()
        duplicate = self._trace("project-a", "case-1").to_dict()
        with self.assertRaises(BenchmarkImportError):
            import_external_decisions([first, duplicate])
        with self.assertRaises(BenchmarkImportError):
            import_external_decisions([{**first, "trace_id": "bad"}])
        with self.assertRaises(BenchmarkImportError):
            import_external_decisions([first, self._trace("project-b", "case-2").to_dict()])

    def test_external_jsonl_uses_same_utf8_and_file_limits(self):
        trace_path = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        path = Path(trace_path.name)
        trace_path.close()
        path.write_text(json.dumps(self._trace().to_dict()) + "\n", encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        self.assertEqual(1, len(import_external_decisions(path)))


if __name__ == "__main__":
    unittest.main()
