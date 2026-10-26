import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

os.environ['TASKMGR_WATCH_TEST'] = '1'

import AutomaticWatch as watch
import IncidentAnalysis as analysis
import FlatlineWatch as flatline_watch
from RealtimeTrace import RealtimeConsumer, TASKMGR_GUID, re_enable_session_provider

class FakeRealtime:
    def __init__(self, target_pid=None):
        self.target_pid = target_pid
        self.provider_event_count = 0
        self.last_provider_event_utc = None
        self.last_provider_event_qpc = None
        self.last_provider_event_mono = None
        self.pending_native_error = None
        self.re_enable_calls = 0

    def set_target_pid(self, pid):
        if self.target_pid != pid:
            self.target_pid = pid
            self.provider_event_count = 0
            if pid is not None:
                self.last_provider_event_mono = time.monotonic()
                self.last_provider_event_utc = '2026-10-26T22:00:00+00:00'
                self.last_provider_event_qpc = 1000000
            else:
                self.last_provider_event_mono = None

    def record_provider_event(self, qpc=None, utc=None, mono=None):
        self.provider_event_count += 1
        self.last_provider_event_mono = time.monotonic() if mono is None else mono
        self.last_provider_event_utc = '2026-10-26T22:00:00+00:00' if utc is None else utc
        self.last_provider_event_qpc = 1000000 if qpc is None else qpc

    def re_enable_provider(self):
        self.re_enable_calls += 1
        return 0

    def poll_native_error(self):
        return None

    def clear_native_error(self):
        return None

    def get_heartbeat_events(self):
        return []

    def status(self):
        return {
            'state': 'running',
            'session': 'fake-realtime',
            'target_pid': self.target_pid,
            'provider_event_count': self.provider_event_count,
            'last_provider_event_utc': self.last_provider_event_utc,
            'last_provider_event_qpc': self.last_provider_event_qpc,
            'last_provider_event_mono': self.last_provider_event_mono,
            'heartbeat_count': 0
        }


class NativeWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(watch.Monitor, 'watch_origin')
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def _create_monitor(self, target_pid=1234):
        monitor = watch.Monitor(restart_only=True)
        monitor.light_etw = True
        fake_rec = MagicMock()
        fake_rec.status.return_value = {'state': 'running', 'session': 'TaskmgrCorrelation-test'}
        monitor.recorder = fake_rec
        fake_rt = FakeRealtime(target_pid=target_pid)
        monitor.realtime = fake_rt
        monitor._mock_pid_alive = True
        return monitor, fake_rt, fake_rec

    def test_watchdog_gap_start_and_end(self):
        """Detects 15s without provider events for target PID, then closes gap on event resume."""
        with tempfile.TemporaryDirectory() as d, \
             patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch.time, 'monotonic', return_value=100.0) as mock_clock:
            monitor, fake_rt, _ = self._create_monitor(target_pid=1234)
            fake_rt.record_provider_event(qpc=1000, mono=100.0)
            monitor.last_known_event_count = fake_rt.provider_event_count

            # At t=110 (10s elapsed): no gap yet
            mock_clock.return_value = 110.0
            monitor.check_native_coverage(110.0)
            monitor.publish('normal')
            self.assertEqual(monitor.state['native_coverage'], 'ok')
            self.assertIsNone(monitor.current_provider_gap)

            # At t=116 (16s elapsed): gap declared!
            mock_clock.return_value = 116.0
            monitor.check_native_coverage(116.0)
            monitor.publish('normal')
            self.assertEqual(monitor.state['native_coverage'], 'gap')
            self.assertIsNotNone(monitor.current_provider_gap)
            self.assertEqual(monitor.current_provider_gap['taskmgr_pid'], 1234)
            self.assertGreaterEqual(monitor.state['native_coverage_gap_seconds'], 16.0)

            # At t=125 (new provider event arrives): gap ends!
            mock_clock.return_value = 125.0
            fake_rt.record_provider_event(qpc=2000, mono=125.0)
            monitor.check_native_coverage(125.0)
            monitor.publish('normal')
            self.assertEqual(monitor.state['native_coverage'], 'ok')
            self.assertIsNone(monitor.current_provider_gap)
            self.assertEqual(len(monitor.observations.native_coverage_gaps), 1)

            completed_gap = monitor.observations.native_coverage_gaps[0]
            self.assertEqual(completed_gap['taskmgr_pid'], 1234)
            self.assertEqual(completed_gap['duration_seconds'], 25.0)
            self.assertEqual(completed_gap['end_qpc'], 2000)

    def test_watchdog_re_enable_calls_and_backoff(self):
        """Re-enables provider idempotently and backs off when silence persists."""
        with tempfile.TemporaryDirectory() as d, \
             patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 're_enable_session_provider') as mock_re_enable_session, \
             patch.object(watch.time, 'monotonic', return_value=100.0) as mock_clock:
            monitor, fake_rt, fake_rec = self._create_monitor(target_pid=1234)
            fake_rt.record_provider_event(qpc=1000, mono=100.0)
            monitor.last_known_event_count = fake_rt.provider_event_count

            # Silence reaches 15s -> First re-enable attempt
            mock_clock.return_value = 115.5
            monitor.check_native_coverage(115.5)
            self.assertEqual(fake_rt.re_enable_calls, 1)
            mock_re_enable_session.assert_called_with('TaskmgrCorrelation-test', level=2)

            # Silence continues: at t=118 (only 2.5s later), no retry yet due to 5s backoff
            mock_clock.return_value = 118.0
            monitor.check_native_coverage(118.0)
            self.assertEqual(fake_rt.re_enable_calls, 1)

            # At t=121 (5.5s after first attempt): Second attempt (5s backoff fired)
            mock_clock.return_value = 121.0
            monitor.check_native_coverage(121.0)
            self.assertEqual(fake_rt.re_enable_calls, 2)

            # Next backoff is 10s: at t=128 (7s later), no retry
            mock_clock.return_value = 128.0
            monitor.check_native_coverage(128.0)
            self.assertEqual(fake_rt.re_enable_calls, 2)

            # At t=132 (11s later): Third attempt (10s backoff fired)
            mock_clock.return_value = 132.0
            monitor.check_native_coverage(132.0)
            self.assertEqual(fake_rt.re_enable_calls, 3)

    def test_target_pid_exit_is_not_a_gap(self):
        """When target PID exits, lack of provider events is expected and never a gap."""
        with tempfile.TemporaryDirectory() as d, \
             patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch.time, 'monotonic', return_value=100.0) as mock_clock:
            monitor, fake_rt, _ = self._create_monitor(target_pid=1234)
            fake_rt.record_provider_event(qpc=1000, mono=100.0)
            monitor.last_known_event_count = fake_rt.provider_event_count

            # Target PID exits
            monitor._mock_pid_alive = False

            # Even 60s later, no gap is started
            mock_clock.return_value = 160.0
            monitor.check_native_coverage(160.0)
            monitor.publish('normal')
            self.assertEqual(monitor.state['native_coverage'], 'ok')
            self.assertIsNone(monitor.current_provider_gap)
            self.assertEqual(len(monitor.observations.native_coverage_gaps), 0)

    def test_target_pid_exit_clears_active_gap(self):
        """If a gap was in progress and the process exits, it is cleared without recording a gap."""
        with tempfile.TemporaryDirectory() as d, \
             patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch.time, 'monotonic', return_value=100.0) as mock_clock:
            monitor, fake_rt, _ = self._create_monitor(target_pid=1234)
            fake_rt.record_provider_event(qpc=1000, mono=100.0)
            monitor.last_known_event_count = fake_rt.provider_event_count

            # Declare gap at 120s
            mock_clock.return_value = 120.0
            monitor.check_native_coverage(120.0)
            self.assertIsNotNone(monitor.current_provider_gap)

            # Process terminates
            monitor._mock_pid_alive = False
            mock_clock.return_value = 122.0
            monitor.check_native_coverage(122.0)
            self.assertIsNone(monitor.current_provider_gap)
            self.assertEqual(len(monitor.observations.native_coverage_gaps), 0)

    def test_n2_verdict_provider_gap_during_gap(self):
        """N2 verdict is indeterminate with reason provider_gap when onset falls in gap."""
        freq = 10000000
        # Gap spans QPC 10000 to 20000
        obs = {
            'markers': {
                'first_candidate': {'qpc': 15000, 'utc': '2026-10-26T22:00:01+00:00'},
                'last_normal': {'qpc': 8000, 'utc': '2026-10-26T22:00:00+00:00'}
            },
            'native_coverage_gaps': [{
                'start_qpc': 10000,
                'end_qpc': 20000,
                'duration_seconds': 1.0,
                'taskmgr_pid': 1234,
                'reason': 'no_provider_events_for_15s'
            }]
        }
        sig = analysis.extract_native_signature([], [], {}, obs, None, freq, 0, 50000)
        self.assertFalse(sig['recorded'])
        self.assertEqual(sig['verdict'], 'indeterminate')
        self.assertIn('provider_gap', sig['reason'])

    def test_n2_verdict_provider_gap_within_10s_after_gap(self):
        """N2 verdict is indeterminate when onset is within 10s after gap ended."""
        freq = 10000000
        gap_end = 20000
        # 5 seconds after gap ended
        first_qpc = gap_end + 5 * freq
        obs = {
            'markers': {
                'first_candidate': {'qpc': first_qpc, 'utc': '2026-10-26T22:00:05+00:00'},
                'last_normal': {'qpc': first_qpc - 1000, 'utc': '2026-10-26T22:00:04+00:00'}
            },
            'native_coverage_gaps': [{
                'start_qpc': 10000,
                'end_qpc': gap_end,
                'duration_seconds': 1.0,
                'taskmgr_pid': 1234,
                'reason': 'no_provider_events_for_15s'
            }]
        }
        sig = analysis.extract_native_signature([], [], {}, obs, None, freq, 0, first_qpc + 1000)
        self.assertFalse(sig['recorded'])
        self.assertEqual(sig['verdict'], 'indeterminate')
        self.assertIn('provider_gap', sig['reason'])

    def test_n2_verdict_not_provider_gap_beyond_10s(self):
        """When onset is >10s after gap ended, gap does not trigger provider_gap."""
        freq = 10000000
        gap_end = 20000
        # 15 seconds after gap ended (beyond 10s window)
        first_qpc = gap_end + 15 * freq
        obs = {
            'markers': {
                'first_candidate': {'qpc': first_qpc, 'utc': '2026-10-26T22:00:15+00:00'},
                'last_normal': {'qpc': first_qpc - 1000, 'utc': '2026-10-26T22:00:14+00:00'}
            },
            'native_coverage_gaps': [{
                'start_qpc': 10000,
                'end_qpc': gap_end,
                'duration_seconds': 1.0,
                'taskmgr_pid': 1234,
                'reason': 'no_provider_events_for_15s'
            }]
        }
        # Provider delivered an event in the 10s pre-candidate window
        ev = [{'raw': str(first_qpc - 1 * freq), 'module': 'Taskmgr.exe', 'hresult': 0}]
        sig = analysis.extract_native_signature(ev, [], {}, obs, None, freq, 0, first_qpc + 1000)
        self.assertFalse(sig['recorded'])
        self.assertNotEqual(sig['verdict'], 'indeterminate')
        self.assertEqual(sig['verdict'], 'non_conforming')

    def test_flatline_watch_tray_and_status_native_gap_text(self):
        """TEXT dictionary has Chinese and English translations for native gap interruption."""
        self.assertEqual(flatline_watch.TEXT['zh']['native_gap_status'], '原生记录中断')
        self.assertEqual(flatline_watch.TEXT['en']['native_gap_status'], 'Native recording interrupted')
        self.assertEqual(flatline_watch.TEXT['zh']['native_gap_alert'], '原生记录中断')
        self.assertEqual(flatline_watch.TEXT['en']['native_gap_alert'], 'Native recording interrupted')


if __name__ == '__main__':
    unittest.main()
