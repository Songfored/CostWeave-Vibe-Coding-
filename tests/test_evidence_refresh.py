import hashlib
import json
import unittest
from pathlib import Path

from costweave.model_catalog import EVIDENCE_REGISTRY, EVIDENCE_SNAPSHOT_DATE, SNAPSHOT_DATE, catalog_metadata


class EvidenceRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = Path(__file__).parents[1] / 'costweave'
        cls.evidence = json.loads((cls.package / EVIDENCE_REGISTRY).read_text(encoding='utf-8'))

    def test_historical_snapshot_is_identical_and_dates_do_not_refresh_prices(self):
        previous = self.evidence['previous_snapshot']
        self.assertEqual(previous['sha256'], hashlib.sha256((self.package / previous['path']).read_bytes()).hexdigest())
        self.assertEqual(SNAPSHOT_DATE, self.evidence['facts_snapshot_date'])
        self.assertEqual(EVIDENCE_SNAPSHOT_DATE, self.evidence['snapshot_date'])
        self.assertLess(SNAPSHOT_DATE, EVIDENCE_SNAPSHOT_DATE)
        self.assertEqual(EVIDENCE_SNAPSHOT_DATE, catalog_metadata()['capability_evidence_date'])

    def test_all_references_resolve_without_cross_model_assignment(self):
        rows = [r for group in self.evidence['benchmark_snapshots'].values() for r in group]
        ids = {r['id']: r for r in rows}
        self.assertEqual(len(rows), len(ids))
        records = {**self.evidence['models'], **self.evidence['observed_models']}
        for row in rows:
            self.assertIn(row['model_record_id'], records)
            self.assertIn(row['id'], records[row['model_record_id']]['benchmark_refs'])
        for mid, record in records.items():
            for ref in record['benchmark_refs']:
                self.assertEqual(mid, ids[ref]['model_record_id'])

    def test_source_types_and_versions_are_not_mixed(self):
        for group, rows in self.evidence['benchmark_snapshots'].items():
            for row in rows:
                self.assertTrue(row['source_url'].startswith('https://'))
                self.assertEqual(EVIDENCE_SNAPSHOT_DATE, row['retrieved_at'])
                self.assertEqual('vendor_reported' if group == 'vendor_reported' else 'independent', row['evidence_type'])
                if group == 'artificial_analysis':
                    self.assertEqual('4.3', row['index_version'])
                if 'rank' in row:
                    self.assertLessEqual(row['rank_range'][0], row['rank'])
                    self.assertLessEqual(row['rank'], row['rank_range'][1])
                    self.assertGreater(row['votes'], 0)
                    self.assertGreaterEqual(row['uncertainty'], 0)

    def test_vendor_conflict_is_not_silently_adopted(self):
        conflict = self.evidence['source_conflicts'][0]
        self.assertFalse(conflict['resolved'])
        self.assertIsNone(conflict['adopted_score'])
        self.assertEqual({64.0, 65.4}, {r['score'] for r in conflict['observations']})
        self.assertFalse(any(r['benchmark'] == conflict['benchmark'] for r in self.evidence['benchmark_snapshots']['vendor_reported']))

    def test_new_model_identity_does_not_relabel_historical_model(self):
        notice = self.evidence['identity_notices'][0]
        self.assertEqual('deepseek-v4-flash', notice['model_record_id'])
        self.assertEqual('deepseek-v4.1-flash', notice['official_alias_now_serves'])
        records = self.evidence['observed_models']
        self.assertEqual('deepseek-flash', records['deepseek-v4.1-flash']['api_model_id'])
        self.assertIsNone(records['glm-5.3']['api_model_id'])
        for record in records.values():
            self.assertEqual('evidence_only_not_added_to_routing', record['status'])


if __name__ == '__main__':
    unittest.main()
