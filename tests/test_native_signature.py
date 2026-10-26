import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))

import IncidentAnalysis as A
import ProcessTrace as P


class NativeSignatureTests(unittest.TestCase):
    def test_regression_real_fixture(self):
        fixture_dir = ROOT/'tests/fixtures/restart_164452'
        if not fixture_dir.exists():
            raise unittest.SkipTest('restart_164452 fixture missing')
        with tempfile.TemporaryDirectory() as out_d:
            out_path = Path(out_d)/'analysis'
            res = A.analyze(fixture_dir, output=out_path)
            sig = res.get('native_signature', {})

            self.assertTrue(sig.get('recorded'))
            self.assertEqual(sig.get('verdict'), 'conforming')
            self.assertEqual(sig.get('hresult'), '0x8007139f')
            self.assertEqual(sig.get('module'), 'TaskManagerDataLayer.dll')
            self.assertEqual(sig.get('qpc'), 1731226186497)
            self.assertTrue(sig.get('utc', '').startswith('2026-10-26T16:44:09.109'))
            self.assertEqual(sig.get('lead_seconds_before_first_candidate'), 13.0)

            cc = sig.get('call_chain', [])
            self.assertEqual(len(cc), 5)
            self.assertEqual([c['failure_type_name'] for c in cc], ['Log', 'Log', 'Return', 'Return', 'Exception'])
            self.assertEqual(cc[2]['file_basename'], 'aggregationdatamonitor.cpp')
            self.assertEqual(cc[2]['line'], 640)
            self.assertIn('winrt::hresult_error', cc[2]['message'])
            self.assertEqual(cc[3]['file_basename'], 'aggregationdatamonitor.cpp')
            self.assertEqual(cc[3]['line'], 159)
            self.assertEqual(cc[4]['file_basename'], 'Core.cpp')
            self.assertEqual(cc[4]['line'], 407)

            act = sig.get('activity_before', {})
            self.assertEqual(act.get('process_starts'), 14)
            self.assertEqual(act.get('thread_starts'), 340)
            self.assertEqual(act.get('control_process_starts_median'), 0.0)
            self.assertEqual(act.get('control_process_starts_max'), 4)
            self.assertEqual(act.get('control_thread_starts_median'), 36.0)
            self.assertEqual(act.get('control_thread_starts_max'), 169)

            top_threads = act.get('top_thread_creators', [])
            self.assertTrue(any('SearchIndexer' in t['image'] and t['count'] == 113 for t in top_threads))

            report = (out_path/'incident-analysis.md').read_text(encoding='utf-8')
            self.assertIn('Native freeze signature: Taskmgr internal error', report)
            self.assertIn('原生卡死特征：Taskmgr 内部错误', report)
            self.assertIn('0x8007139f', report)
            self.assertIn('SearchIndexer', report)

    def test_unrecorded_missing_normal(self):
        sig = A.extract_native_signature(
            [], [], {},
            {'markers': {'first_candidate': {'qpc': 5000}, 'last_normal': None}},
            None, 1000, 0, 8000,
            quality={'circular_overwrite': False}
        )
        self.assertFalse(sig['recorded'])
        self.assertEqual(sig['verdict'], 'indeterminate')
        self.assertIn('missing_normal', sig['reason'])

    def test_unrecorded_blind(self):
        sig = A.extract_native_signature(
            [], [], {},
            {'markers': {'first_candidate': {'qpc': 5000}, 'last_normal': {'qpc': 4000}}},
            None, 1000, 0, 8000,
            quality={'preceding_blind_interval': {'duration_seconds': 300}, 'circular_overwrite': False}
        )
        self.assertFalse(sig['recorded'])
        self.assertEqual(sig['verdict'], 'indeterminate')
        self.assertIn('blind', sig['reason'])

    def test_unrecorded_not_present(self):
        sig = A.extract_native_signature(
            [], [], {},
            {'markers': {'first_candidate': {'qpc': 5000}, 'last_normal': {'qpc': 4000}}},
            None, 1000, 0, 8000,
            quality={'preceding_blind_interval': None, 'circular_overwrite': False}
        )
        self.assertFalse(sig['recorded'])
        self.assertEqual(sig['verdict'], 'non_conforming')
        self.assertIn('not_present', sig['reason'])

    def test_n1_summary_update(self):
        with tempfile.TemporaryDirectory() as d:
            summary = A.update_n1_summary(d, 'incident-1', {'verdict': 'conforming', 'recorded': True, 'hresult': '0x8007139f', 'qpc': 100})
            self.assertEqual(summary['qualifying_count'], 1)
            self.assertEqual(summary['conforming_count'], 1)
            self.assertEqual(summary['non_conforming_count'], 0)
            self.assertEqual(summary['indeterminate_count'], 0)
            self.assertEqual(summary['status'], 'evaluating')

            summary2 = A.update_n1_summary(d, 'incident-2', {'verdict': 'indeterminate', 'recorded': False, 'reason': 'blind'})
            self.assertEqual(summary2['qualifying_count'], 1)
            self.assertEqual(summary2['conforming_count'], 1)
            self.assertEqual(summary2['indeterminate_count'], 1)

            summary3 = A.update_n1_summary(d, 'incident-3', {'verdict': 'non_conforming', 'recorded': False, 'reason': 'not_present'})
            self.assertEqual(summary3['qualifying_count'], 2)
            self.assertEqual(summary3['non_conforming_count'], 1)

    def test_session_records_both_providers(self):
        with tempfile.TemporaryDirectory() as d:
            sess = P.Session(d)
            self.assertEqual(sess.record['max_etl_mb'], 128)
            self.assertEqual(len(sess.record['providers']), 2)
            self.assertEqual(sess.record['providers'][0]['guid'], P.GUID)
            self.assertEqual(sess.record['providers'][1]['guid'], P.TASKMGR_GUID)


if __name__ == '__main__':
    unittest.main()
