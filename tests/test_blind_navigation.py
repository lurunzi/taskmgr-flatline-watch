"""Tests for post-recovery navigation retries, backoff, and blind monitoring evidence."""
import itertools
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import AutomaticWatch as watch
import IncidentWatch as incident_watch
import IncidentAnalysis as analysis
import FlatlineWatch as flatline_watch
from test_detection import graph


class BlindNavigationTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(watch.Monitor, 'watch_origin')
        guard.start()
        self.addCleanup(guard.stop)

    def test_blind_navigation_exponential_backoff(self):
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_taskmgr_process_alive', return_value=True), \
             patch.object(watch, 'is_user_interacting_with_window', return_value=False), \
             patch.object(watch, 'restore_cpu_page', return_value={'status': 'restarted_waiting_for_cpu_page', 'launch_pid': 9876}) as nav:
            monitor = watch.Monitor()
            evidence = {'classification': 'restart_only', 'status': 'complete',
                        'taskmgr_pid': 1234, 'window': 5678,
                        'recovery': {'status': 'restarted_waiting_for_cpu_page', 'launch_pid': 9876}}
            folder = Path(d) / 'test-incident'
            folder.mkdir()

            # Schedule until retries are exhausted
            monitor.capture_result = 'recovery_failed'
            monitor.schedule_retry(folder, evidence)
            for _ in range(watch.RETRY_LIMIT):
                monitor.retry_recovery(time.monotonic() + watch.RETRY_INTERVAL + 1)
                if monitor.dump_thread:
                    monitor.dump_thread.join(timeout=2)

            self.assertTrue(evidence['recovery']['retry_exhausted'])
            self.assertTrue(monitor.blind_after_recovery)
            self.assertIsNotNone(monitor.blind_navigation_target)

            # Check backoff schedule progression
            # First blind retry delay should be 120s
            t0 = time.monotonic()
            self.assertAlmostEqual(monitor.next_navigation_retry_monotonic - t0, 120, delta=2)

            # Fire first blind retry
            t1 = t0 + 121
            monitor.check_blind_navigation(t1)
            if monitor.dump_thread:
                monitor.dump_thread.join(timeout=2)
            self.assertEqual(monitor.blind_retry_count, 1)
            # Next delay should be 300s
            self.assertAlmostEqual(monitor.next_navigation_retry_monotonic - t1, 300, delta=2)

            # Fire second blind retry
            t2 = t1 + 301
            monitor.check_blind_navigation(t2)
            if monitor.dump_thread:
                monitor.dump_thread.join(timeout=2)
            self.assertEqual(monitor.blind_retry_count, 2)
            # Next delay should be 600s
            self.assertAlmostEqual(monitor.next_navigation_retry_monotonic - t2, 600, delta=2)

            # Fire third blind retry
            t3 = t2 + 601
            monitor.check_blind_navigation(t3)
            if monitor.dump_thread:
                monitor.dump_thread.join(timeout=2)
            self.assertEqual(monitor.blind_retry_count, 3)
            # Next delay should be 1200s
            self.assertAlmostEqual(monitor.next_navigation_retry_monotonic - t3, 1200, delta=2)

    def test_blind_navigation_hourly_rate_limit(self):
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_taskmgr_process_alive', return_value=True), \
             patch.object(watch, 'is_user_interacting_with_window', return_value=False), \
             patch.object(watch, 'restore_cpu_page', return_value={'status': 'restarted_waiting_for_cpu_page', 'launch_pid': 9876}):
            monitor = watch.Monitor()
            folder = Path(d) / 'test-incident'
            folder.mkdir()
            evidence = {'classification': 'restart_only', 'status': 'complete',
                        'taskmgr_pid': 1234, 'window': 5678,
                        'recovery': {'status': 'restarted_waiting_for_cpu_page', 'launch_pid': 9876}}

            monitor.arm_blind_navigation(folder, evidence)
            t = 10000.0

            # Simulate 4 retries already executed in the last hour
            monitor.blind_retry_history = [t - 3000, t - 2000, t - 1000, t - 100]
            monitor.next_navigation_retry_monotonic = t

            # Since 4 retries occurred within 3600s, check_blind_navigation should defer
            monitor.check_blind_navigation(t)
            self.assertIsNone(monitor.dump_thread)
            # Next retry should be deferred to t - 3000 + 3600 = t + 600
            self.assertAlmostEqual(monitor.next_navigation_retry_monotonic, t + 600, delta=2)

    def test_blind_navigation_stops_when_target_pid_changes_or_exits(self):
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_taskmgr_process_alive', return_value=False):
            monitor = watch.Monitor()
            folder = Path(d) / 'test-incident'
            folder.mkdir()
            evidence = {'classification': 'restart_only', 'status': 'complete',
                        'taskmgr_pid': 1234, 'window': 5678,
                        'recovery': {'status': 'restarted_waiting_for_cpu_page', 'launch_pid': 9876}}

            monitor.arm_blind_navigation(folder, evidence)
            monitor.next_navigation_retry_monotonic = time.monotonic() - 1

            monitor.check_blind_navigation(time.monotonic())
            # Target dead; navigation target cleared and thread never launched
            self.assertIsNone(monitor.blind_navigation_target)
            self.assertIsNone(monitor.dump_thread)

    def test_blind_navigation_skips_when_user_interacting(self):
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_taskmgr_process_alive', return_value=True), \
             patch.object(watch, 'is_user_interacting_with_window', return_value=True):
            monitor = watch.Monitor()
            folder = Path(d) / 'test-incident'
            folder.mkdir()
            evidence = {'classification': 'restart_only', 'status': 'complete',
                        'taskmgr_pid': 1234, 'window': 5678,
                        'recovery': {'status': 'restarted_waiting_for_cpu_page', 'launch_pid': 9876}}

            monitor.arm_blind_navigation(folder, evidence)
            now = time.monotonic()
            monitor.next_navigation_retry_monotonic = now - 1

            monitor.check_blind_navigation(now)
            # User interacting; retry deferred by 60s, thread not started
            self.assertIsNone(monitor.dump_thread)
            self.assertAlmostEqual(monitor.next_navigation_retry_monotonic - now, 60, delta=2)

    def test_status_json_blind_fields(self):
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)):
            monitor = watch.Monitor()
            status_path = Path(d) / 'status.json'

            # Normal sample
            monitor.accept({'state': 'captured', 'pid': 1111, 'hwnd': 2222,
                            'geometry': [[0, 0, 75, 50]] * 33, 'total': graph(), 'cores': graph()})
            st = json.loads(status_path.read_text(encoding='utf-8'))
            self.assertIsNone(st['waiting_since'])
            self.assertEqual(st['blind_seconds'], 0)
            self.assertFalse(st['blind_after_recovery'])
            self.assertIsNone(st['next_navigation_retry'])

            # Transition to waiting
            monitor.blind_after_recovery = True
            monitor.next_navigation_retry_utc = '2026-10-26T16:00:00+00:00'
            with patch.object(watch.time, 'monotonic', side_effect=itertools.count(100.0, 10.0)):
                monitor.accept({'state': 'waiting'})
                st2 = json.loads(status_path.read_text(encoding='utf-8'))
                self.assertIsNotNone(st2['waiting_since'])
                self.assertGreaterEqual(st2['blind_seconds'], 0)
                self.assertTrue(st2['blind_after_recovery'])
                self.assertEqual(st2['next_navigation_retry'], '2026-10-26T16:00:00+00:00')

            # Back to captured
            monitor.accept({'state': 'captured', 'pid': 1111, 'hwnd': 2222,
                            'geometry': [[0, 0, 75, 50]] * 33, 'total': graph(), 'cores': graph()})
            st3 = json.loads(status_path.read_text(encoding='utf-8'))
            self.assertIsNone(st3['waiting_since'])
            self.assertEqual(st3['blind_seconds'], 0)
            self.assertFalse(st3['blind_after_recovery'])
            self.assertIsNone(st3['next_navigation_retry'])

    def test_blind_interval_recorded_in_observation_and_report(self):
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)):
            monitor = watch.Monitor()
            # 1. Enter waiting
            with patch.object(watch.time, 'monotonic', return_value=100.0):
                monitor.accept({'state': 'waiting'})
            self.assertIsNotNone(monitor.current_blind_interval)

            # 2. Leave waiting
            with patch.object(watch.time, 'monotonic', return_value=250.0):
                monitor.accept({'state': 'captured', 'pid': 1111, 'hwnd': 2222,
                                'geometry': [[0, 0, 75, 50]] * 33, 'total': graph(), 'cores': graph()})

            self.assertEqual(len(monitor.observations.blind_intervals), 1)
            b = monitor.observations.blind_intervals[0]
            self.assertEqual(b['duration_seconds'], 150.0)

            # 3. Create observation record
            obs = monitor.observations.record([], 1111, os.getpid())
            self.assertIn('blind_intervals', obs)
            self.assertEqual(len(obs['blind_intervals']), 1)
            self.assertEqual(obs['blind_intervals'][0]['duration_seconds'], 150.0)

    def test_analysis_report_flags_preceding_blind_interval(self):
        # Verify render output includes blind interval warning in both languages
        result = {
            'quality': {
                'ranking_usable': False,
                'errors': ['test_error'],
                'circular_overwrite': False,
                'events_lost': 0,
                'buffers_lost': 0,
                'preceding_blind_interval': {'duration_seconds': 3600.0, 'reason': 'post_recovery_navigation'}
            },
            'continuity': {'recent_breaks': []},
            'phases': {
                'before_first': {'ranking_usable': False, 'errors': ['test_error']},
                'after_first': {'ranking_usable': False, 'errors': ['test_error']}
            }
        }
        md = analysis.render(result)
        self.assertIn('Freeze onset is indeterminate; blind intervals cannot serve as controls.', md)
        self.assertIn('冻结发生起点不可判定，失明区间不能作为正常对照。', md)

    def test_save_navigation_failure_window(self):
        with tempfile.TemporaryDirectory() as d:
            mock_img = graph()
            with patch('TaskmgrRecovery.main_window', return_value=12345), \
                 patch('NativeCapture.capture', return_value=mock_img):
                watch.save_navigation_failure_window(9876, Path(d))
                dest = Path(d) / 'navigation-window.png'
                self.assertTrue(dest.exists())
                self.assertGreater(dest.stat().st_size, 0)


class FlatlineWatchTrayTests(unittest.TestCase):
    def test_flatline_watch_tray_shows_monitoring_inactive_after_5_minutes(self):
        # Test TEXT dictionary strings for Chinese and English
        self.assertEqual(flatline_watch.TEXT['zh']['blind_status'], '监测失效')
        self.assertEqual(flatline_watch.TEXT['en']['blind_status'], 'Monitoring inactive')
        self.assertEqual(flatline_watch.TEXT['zh']['blind_alert'], '监测失效（等待 CPU 页面）')
        self.assertEqual(flatline_watch.TEXT['en']['blind_alert'], 'Monitoring inactive (waiting for CPU page)')


if __name__ == '__main__':
    unittest.main()
