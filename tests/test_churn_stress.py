import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location('churn_stress', Path(__file__).resolve().parents[1]/'tools/churn-stress.py')
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)


class ChurnStressTests(unittest.TestCase):
    def test_plan_alternates_and_starts_quiet(self):
        plan = cs.block_plan(4, 10, 'quiet')
        self.assertEqual([kind for _, kind, _, _ in plan], ['quiet', 'load', 'quiet', 'load'])
        self.assertEqual([offset for _, _, offset, _ in plan], [0, 600, 1200, 1800])

    def test_preflight_rejects_unhealthy_watcher(self):
        self.assertEqual(cs.preflight({'state': 'normal', 'dump_running': False, 'native_coverage': 'ok',
                                       'realtime_consumer': {'state': 'running'}}), [])
        problems = cs.preflight({'state': 'waiting', 'dump_running': True, 'native_coverage': 'gap',
                                 'realtime_consumer': {'state': 'failed'}})
        self.assertEqual(len(problems), 4)
        self.assertEqual(cs.preflight({}), ['status.json unreadable'])

    def test_folder_time_parses_restart_names_only(self):
        self.assertEqual(cs.folder_time('restart-20261101-115033-713453'),
                         datetime(2026, 11, 1, 11, 50, 33, tzinfo=timezone.utc))
        self.assertIsNone(cs.folder_time('analysis-retry2-restart-x'))
        self.assertIsNone(cs.folder_time('restart-bad-name-1'))

    def test_attribution_by_block_and_outside(self):
        start = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
        blocks = cs.block_plan(2, 10, 'quiet')
        incidents = [('a', start + timedelta(minutes=3), {}), ('b', start + timedelta(minutes=12), {'trigger_kind': 'native_fast'}),
                     ('c', start + timedelta(minutes=25), {})]
        counts, outside = cs.attribute(incidents, blocks, start)
        self.assertEqual([i['incident'] for i in counts[0]], ['a'])
        self.assertEqual(counts[1][0]['trigger_kind'], 'native_fast')
        self.assertEqual(outside, ['c'])


if __name__ == '__main__':
    unittest.main()
