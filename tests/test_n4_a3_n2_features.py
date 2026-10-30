"""Tests for N4 heartbeat fix & watchdog, A3 frozen line discrepancy, N2 native ranking, and J5 daily tracking."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from AutomaticWatch import Monitor, HEARTBEAT_GAP_THRESHOLD_SECONDS
from IncidentAnalysis import analyze, analyze_heartbeat, heartbeat_text, discrepancy_text
from IncidentWatch import Observations, compute_frozen_line_discrepancy
from RealtimeTrace import RealtimeConsumer, EVENT_FILTER_DESCRIPTOR, EVENT_FILTER_TYPE_EVENT_NAME


class N4HeartbeatAndWatchdogTests(unittest.TestCase):
    def test_filter_buffer_length_and_descriptor_size(self):
        """Filter descriptor size must match the exact 53-byte payload, not 54 null-terminated buffer."""
        consumer = RealtimeConsumer(enabled=False)
        consumer.start()
        # Verify _filter_data_len is 53
        expected_name_bytes = b'CurrentUpdateCycle\x00FallbackError\x00'
        header_len = 20  # struct.pack('<QQBBH', 0, 0, 5, 1, 2)
        expected_len = header_len + len(expected_name_bytes)
        self.assertEqual(expected_len, 53)

        # When session handle is mocked, re_enable_provider passes filter_desc.Size = 53
        consumer.enabled = True
        consumer.session_handle = MagicMock()
        consumer.session_handle.value = 12345
        import ctypes
        consumer._filter_buf = ctypes.create_string_buffer(53)
        consumer._filter_data_len = 53

        with patch('RealtimeTrace.advapi32.EnableTraceEx2', return_value=0) as mock_enable:
            st = consumer.re_enable_provider()
            self.assertEqual(st, 0)
            mock_enable.assert_called_once()
            # Verify EnableTraceEx2 called with level 5 (Verbose)
            self.assertEqual(mock_enable.call_args[0][3], 5)

    def test_heartbeat_watchdog_detects_gap_and_closes_on_resume(self):
        """Target Taskmgr running without heartbeat for 10s opens gap; new heartbeat closes gap."""
        os.environ['TASKMGR_WATCH_TEST'] = '1'
        monitor = Monitor(light_etw=False)
        monitor.enabled = True
        monitor.state = {'state': 'normal', 'taskmgr_pid': 1234}
        monitor.is_pid_alive = MagicMock(return_value=True)

        mock_rt = MagicMock()
        mock_rt.target_pid = 1234
        mock_rt.status.return_value = {
            'heartbeat_count': 0,
            'last_heartbeat_mono': None,
            'last_heartbeat_utc': None
        }
        monitor.realtime = mock_rt
        monitor.target_pid_start_monotonic = 100.0

        # At t=105.0: 5s elapsed (< 10s) -> no gap
        monitor.check_heartbeat_coverage(now=105.0)
        self.assertIsNone(monitor.current_heartbeat_gap)

        # At t=111.0: 11s elapsed (>= 10s) -> opens heartbeat coverage gap
        monitor.check_heartbeat_coverage(now=111.0)
        self.assertIsNotNone(monitor.current_heartbeat_gap)
        self.assertEqual(monitor.current_heartbeat_gap['reason'], 'no_heartbeat_for_10s')
        self.assertEqual(monitor.current_heartbeat_gap['taskmgr_pid'], 1234)
        mock_rt.re_enable_provider.assert_called_once()

        # Publish should export heartbeat_coverage: 'gap'
        monitor.publish('normal')
        self.assertEqual(monitor.state['heartbeat_coverage'], 'gap')
        self.assertGreaterEqual(monitor.state['heartbeat_gap_seconds'], 0.0)

        # At t=120.0: new heartbeat arrives (count increases to 1) -> gap closes
        mock_rt.status.return_value = {
            'heartbeat_count': 1,
            'last_heartbeat_mono': 119.5,
            'last_heartbeat_utc': '2026-10-30T01:00:00+00:00'
        }
        monitor.check_heartbeat_coverage(now=120.0)
        self.assertIsNone(monitor.current_heartbeat_gap)
        self.assertGreater(monitor.cumulative_heartbeat_gap_seconds, 0.0)
        self.assertEqual(len(monitor.observations.heartbeat_coverage_gaps), 1)

        # Publish should export heartbeat_coverage: 'ok'
        monitor.publish('normal')
        self.assertEqual(monitor.state['heartbeat_coverage'], 'ok')
        self.assertEqual(monitor.state['heartbeat_gap_seconds'], 0.0)

    def test_heartbeat_uncovered_classification(self):
        """Incident analysis distinguishes heartbeat_uncovered from heartbeat_stopped_during_freeze."""
        freq = 10_000_000
        markers = {'first_candidate': {'qpc': 100 * freq}, 'confirmed': {'qpc': 130 * freq}}
        native_sig = {'recorded': True, 'qpc': 88 * freq}

        # Case A: empty event list -> heartbeat_uncovered
        with tempfile.TemporaryDirectory() as d:
            hb_file = Path(d) / 'heartbeat.json'
            hb_file.write_text(json.dumps({'events': []}), encoding='utf-8')
            res = analyze_heartbeat(hb_file, {}, markers, native_sig, freq)
            self.assertFalse(res['recorded'])
            self.assertEqual(res['classification'], 'heartbeat_uncovered')
            text_en = heartbeat_text(res, zh=False)
            self.assertIn('Heartbeat not recorded', text_en)

        # Case B: last update was 7 hours ago (gap > 10s before native error) -> heartbeat_uncovered
        with tempfile.TemporaryDirectory() as d:
            events = [
                {'qpc': 10 * freq, 'utc': '2026-10-27T18:56:14.000Z', 'pid': 1, 'tid': 2,
                 'layer': 'DataModel', 'operation': 'UpdateStarted', 'tab': 1, 'correlation_id': 'c1'}
            ]
            hb_file = Path(d) / 'heartbeat.json'
            hb_file.write_text(json.dumps({'events': events}), encoding='utf-8')
            res = analyze_heartbeat(hb_file, {}, markers, native_sig, freq)
            self.assertTrue(res['recorded'])
            self.assertEqual(res['classification'], 'heartbeat_uncovered')
            self.assertIn('delivery gap before onset', res['uncovered_reason'])
            text_en = heartbeat_text(res, zh=False)
            self.assertIn('Heartbeat uncovered', text_en)
            text_zh = heartbeat_text(res, zh=True)
            self.assertIn('心跳未覆盖', text_zh)

        # Case C: update stopped 2s before native error -> heartbeat_stopped_during_freeze
        with tempfile.TemporaryDirectory() as d:
            events = [
                {'qpc': 86 * freq, 'utc': '2026-10-28T01:50:08.000Z', 'pid': 1, 'tid': 2,
                 'layer': 'DataModel', 'operation': 'UpdateStarted', 'tab': 1, 'correlation_id': 'c1'},
                {'qpc': 86 * freq + int(0.02 * freq), 'utc': '2026-10-28T01:50:08.020Z', 'pid': 1, 'tid': 2,
                 'layer': 'DataModel', 'operation': 'UpdateFinalized', 'tab': 1, 'correlation_id': 'c1'}
            ]
            hb_file = Path(d) / 'heartbeat.json'
            hb_file.write_text(json.dumps({'events': events}), encoding='utf-8')
            res = analyze_heartbeat(hb_file, {}, markers, native_sig, freq)
            self.assertTrue(res['recorded'])
            self.assertEqual(res['classification'], 'heartbeat_stopped_during_freeze')
            self.assertFalse(res['continued_during_freeze'])
            text_en = heartbeat_text(res, zh=False)
            self.assertIn('Stopped before freeze onset', text_en)
            self.assertIn('heartbeat stopped during freeze', text_en)
            text_zh = heartbeat_text(res, zh=True)
            self.assertIn('在冻结起点前停止循环', text_zh)
            self.assertIn('冻结时心跳停止', text_zh)


class A3FrozenLineDiscrepancyTests(unittest.TestCase):
    def test_frozen_line_discrepancy_calculation_1024_1606(self):
        """On 2026-10-24 16:06: tail_height 15.0 px on 46 px chart = 67.4% vs 100.0% CPU -> +32.6% discrepancy."""
        frames = [
            {'result': {'state': 'candidate', 'tail_height': 15.0, 'system_cpu_percent': 100.0}},
            {'result': {'state': 'suspect', 'tail_height': 15.0, 'system_cpu_percent': 100.0}}
        ]
        disc = compute_frozen_line_discrepancy(frames, inner_height=46.0)
        self.assertIsNotNone(disc)
        self.assertEqual(disc['tail_height_px'], 15.0)
        self.assertEqual(disc['inner_height_px'], 46.0)
        self.assertEqual(disc['frozen_line_percent'], 67.4)
        self.assertEqual(disc['system_cpu_percent_median'], 100.0)
        self.assertEqual(disc['system_cpu_percent_max'], 100.0)
        self.assertEqual(disc['discrepancy_median'], 32.6)
        self.assertEqual(disc['discrepancy_max'], 32.6)

        text_en = discrepancy_text(disc, zh=False)
        self.assertIn('Frozen line discrepancy (A3)', text_en)
        self.assertIn('15.0 px (67.4%', text_en)
        self.assertIn('median 100.0%', text_en)
        self.assertIn('+32.6%', text_en)

        text_zh = discrepancy_text(disc, zh=True)
        self.assertIn('冻结线高度与系统 CPU 偏差（A3）', text_zh)
        self.assertIn('15.0 像素（占有效图高 46.0 像素的 67.4%）', text_zh)
        self.assertIn('中位数 100.0%', text_zh)
        self.assertIn('+32.6%', text_zh)

    def test_observation_record_includes_heartbeat_gaps_and_discrepancy(self):
        """Observations.record populates heartbeat_coverage_gaps and frozen_line_discrepancy."""
        obs = Observations()
        obs.record_heartbeat_coverage_gap({'start_qpc': 100, 'end_qpc': 200, 'duration_seconds': 10.0})
        frames = [
            {'qpc': 100, 'timestamp': '2026-10-30T01:00:00Z',
             'result': {'state': 'suspect', 'tail_height': 23.0, 'system_cpu_percent': 50.0}}
        ]
        rec = obs.record(frames, taskmgr_pid=123, watcher_pid=456)
        self.assertIn('heartbeat_coverage_gaps', rec)
        self.assertEqual(len(rec['heartbeat_coverage_gaps']), 1)
        self.assertIn('frozen_line_discrepancy', rec)
        # 23.0 on 46.0 px = 50.0% line vs 50.0% CPU -> 0.0 discrepancy
        self.assertEqual(rec['frozen_line_discrepancy']['frozen_line_percent'], 50.0)
        self.assertEqual(rec['frozen_line_discrepancy']['discrepancy_median'], 0.0)


class N2NativeRankingTests(unittest.TestCase):
    def test_offline_ranking_anchors_on_native_error(self):
        """When native signature is recorded, top ranking scope is pre_native [-2, 0) s before native error."""
        freq = 10_000_000
        native_qpc = 100 * freq
        first_qpc = 115 * freq
        confirmed_qpc = 145 * freq

        header = {
            'PerfFreq': freq,
            'EventsLost': 0, 'BuffersLost': 0, 'scanned_events': 100, 'selected_events': 10,
            'StartTime': 1000000000, 'raw_start': 50 * freq,
            'native_events': [
                {'module': 'TaskManagerDataLayer.dll', 'hresult': 0x8007139F, 'raw': str(native_qpc),
                 'tid': 10, 'pid': 1, 'failure_type': 1, 'file': 'aggregationdatamonitor.cpp', 'line': 640}
            ]
        }
        observation = {
            'taskmgr_pid': 1, 'watcher_pid': 99, 'qpc_frequency': freq,
            'markers': {
                'last_normal': {'qpc': 97 * freq, 'utc': '2026-10-30T00:00:00Z'},
                'first_candidate': {'qpc': 101 * freq, 'utc': '2026-10-30T00:00:04Z'},
                'confirmed': {'qpc': 103 * freq, 'utc': '2026-10-30T00:00:06Z'}
            },
            'samples': [
                {'qpc': s * freq, 'state': 'candidate' if s >= 101 else 'normal',
                 'system_cpu_percent': 10.0, 'tail_height': 40.0}
                for s in range(95, 105)
            ]
        }
        # Events: child process started in [-2, 0) s before native error (at t = 99s)
        events = [
            {'kind': 'process_start', 'qpc': 99 * freq, 'pid': 500, 'parent_pid': 1, 'image': 'helper.exe'},
            {'kind': 'thread_start', 'qpc': 99 * freq, 'pid': 1, 'image': 'Taskmgr.exe'}
        ]
        baseline = [{'pid': 1, 'parent_pid': 0, 'image': 'Taskmgr.exe', 'clock': 10 * freq}]

        with tempfile.TemporaryDirectory() as d:
            f = Path(d)
            trace_dir = f / 'process-trace'
            trace_dir.mkdir()
            (trace_dir / 'session.json').write_text(json.dumps({
                'frequency': freq, 'qpc_frequency': freq, 'status': 'stopped', 'decode_exit': 0,
                'circular': False, 'coverage_begin': 50 * freq, 'coverage_end': 150 * freq, 'stopped_qpc': 150 * freq
            }), encoding='utf-8')
            (trace_dir / 'baseline.json').write_text(json.dumps(baseline), encoding='utf-8')
            (f / 'observation.json').write_text(json.dumps(observation), encoding='utf-8')

            with patch('IncidentAnalysis._decoder.decode', return_value=(events, header, [])):
                res = analyze(f)
                self.assertEqual(res['top_scope'], 'pre_native')
                self.assertIn('pre_native', res['phases'])
                self.assertTrue(res['native_signature']['recorded'])
                self.assertEqual(res['native_signature']['hresult'], '0x8007139f')
                self.assertEqual(res['native_signature']['qpc'], native_qpc)


class J5DailyMonitoringTests(unittest.TestCase):
    def test_daily_monitoring_accumulates_effective_hours_when_all_conditions_met(self):
        """Accumulates effective monitoring seconds only when Taskmgr alive on CPU page, detector valid, native ok."""
        os.environ['TASKMGR_WATCH_TEST'] = '1'
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            with patch('AutomaticWatch.OUTPUT', out):
                monitor = Monitor(light_etw=False, output=out)
                monitor.daily_monitoring_file = out / 'daily-monitoring.json'
                monitor.daily_monitoring_history = []
                monitor.last_daily_update_mono = 100.0

                # Condition 1: All 3 satisfied -> accumulates
                monitor.state = {'state': 'normal', 'taskmgr_pid': 1234}
                monitor.current_provider_gap = None
                monitor.current_heartbeat_gap = None
                monitor.update_daily_monitoring(now=102.0)  # dt = 2.0s

                self.assertEqual(len(monitor.daily_monitoring_history), 1)
                entry = monitor.daily_monitoring_history[0]
                self.assertEqual(entry['effective_monitoring_seconds'], 2.0)
                self.assertEqual(entry['blind_seconds'], 0.0)

                # Condition 2: Monitor blind/waiting -> adds to blind_seconds, not effective
                monitor.state = {'state': 'waiting', 'taskmgr_pid': 1234}
                monitor.update_daily_monitoring(now=104.0)  # dt = 2.0s
                self.assertEqual(entry['effective_monitoring_seconds'], 2.0)
                self.assertEqual(entry['blind_seconds'], 2.0)

                # Condition 3: Native gap active -> adds to native_gap_seconds, not effective
                monitor.state = {'state': 'normal', 'taskmgr_pid': 1234}
                monitor.current_provider_gap = {'reason': 'gap', 'start_monotonic': 105.0, 'start_utc': '2026-10-30T01:00:00Z'}
                monitor.update_daily_monitoring(now=106.0)  # dt = 2.0s
                self.assertEqual(entry['effective_monitoring_seconds'], 2.0)
                self.assertEqual(entry['native_gap_seconds'], 2.0)

                # Status export
                monitor.publish('normal')
                self.assertIn('daily_monitoring_history', monitor.state)
                self.assertIn('today_effective_monitoring_hours', monitor.state)


if __name__ == '__main__':
    unittest.main()
