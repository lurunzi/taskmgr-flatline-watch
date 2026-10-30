import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import IncidentAnalysis as IA
from FlatlineDetection import FlatlineDetector, empty_chart


def make_empty_chart(dark=False):
    img = np.full((50, 75, 3), 32 if dark else 255, dtype=np.uint8)
    img[0, :, :] = img[-1, :, :] = 128
    img[:, 0, :] = img[:, -1, :] = 128
    return img


def make_graph(level=20, wave=False):
    img = np.full((50, 75, 3), 255, dtype=np.uint8)
    heights = [level + (int(6 * np.sin(x * 0.6)) if wave else 0) for x in range(75)]
    for x, y in enumerate(heights):
        img[y:, x] = (240, 220, 180)
        img[y, x] = (190, 135, 0)
    return img


class IdleFloorRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = ROOT / 'tests' / 'fixtures' / 'real_incidents_regression.json'
        with open(fixture_path, 'r', encoding='utf-8') as f:
            cls.fixtures = json.load(f)['incidents']

    def test_fixture_file_size_is_lightweight(self):
        fixture_path = ROOT / 'tests' / 'fixtures' / 'real_incidents_regression.json'
        size_kb = fixture_path.stat().st_size / 1024.0
        self.assertLess(size_kb, 200.0, f"Fixture size {size_kb:.1f} KB exceeds 200 KB limit")

    def test_20261027_1815_idle_floor_false_positive_does_not_recover(self):
        data = self.fixtures['restart-20261027-181505-106754']
        self.assertTrue(data['is_false_positive'])
        detector = FlatlineDetector(seconds=30)
        empty = make_empty_chart()

        results = []
        for t, cpu in enumerate(data['system_cpu_samples'] * 4):  # 40 steps, 80 seconds
            now = float(t * 2)
            cores = make_graph(15 + (t % 8), wave=True)
            res = detector.observe(
                now,
                empty,
                cores,
                system_cpu_percent=cpu,
                native_corroborated=data['native_corroborated'],
            )
            results.append(res)

        # Under new logic, uncorroborated idle floor stays uncorroborated and never fires alert
        for r in results:
            self.assertEqual(r['state'], 'idle_floor_uncorroborated')
            self.assertEqual(r['seconds'], 0)
            self.assertFalse(r['alert'])
            self.assertEqual(r['reason'], 'idle_floor_uncorroborated')
        self.assertFalse(any(r['alert'] for r in results))

    def test_20261029_1711_idle_floor_false_positive_does_not_recover(self):
        data = self.fixtures['restart-20261029-171121-203731']
        self.assertTrue(data['is_false_positive'])
        detector = FlatlineDetector(seconds=30)
        empty = make_empty_chart()

        results = []
        for t, cpu in enumerate(data['system_cpu_samples'] * 4):
            now = float(t * 2)
            cores = make_graph(15 + (t % 8), wave=True)
            res = detector.observe(
                now,
                empty,
                cores,
                system_cpu_percent=cpu,
                native_corroborated=data['native_corroborated'],
            )
            results.append(res)

        for r in results:
            self.assertEqual(r['state'], 'idle_floor_uncorroborated')
            self.assertEqual(r['seconds'], 0)
            self.assertFalse(r['alert'])
        self.assertFalse(any(r['alert'] for r in results))

    def test_20261026_1644_real_freeze_recovers_successfully(self):
        data = self.fixtures['restart-20261026-164452-116086']
        self.assertFalse(data['is_false_positive'])
        detector = FlatlineDetector(seconds=30)
        flat = make_graph(45)  # level 45 gives tail_height 43.0

        results = []
        for t in range(20):  # 0 to 38 seconds
            now = float(t * 2)
            cores = make_graph(15 + (t % 8), wave=True)
            cpu = data['system_cpu_samples'][t % len(data['system_cpu_samples'])]
            res = detector.observe(
                now,
                flat,
                cores,
                system_cpu_percent=cpu,
                native_corroborated=data['native_corroborated'],
            )
            results.append(res)

        alert_times = [r['seconds'] for r in results if r['alert']]
        self.assertTrue(len(alert_times) >= 1)
        self.assertTrue(any(r['state'] == 'suspect' for r in results))

    def test_20261028_real_freezes_corroborate_and_recover(self):
        for inc_id in [
            'restart-20261028-015010-677396',
            'restart-20261028-015122-707452',
            'restart-20261028-020507-905926',
            'restart-20261028-194748-689068',
        ]:
            with self.subTest(incident=inc_id):
                data = self.fixtures[inc_id]
                self.assertFalse(data['is_false_positive'])
                detector = FlatlineDetector(seconds=30)
                # Map tail_height to chart level (level = height + 2)
                level = int(data['tail_height'] + 2)
                flat = make_graph(level)

                results = []
                for t in range(20):
                    now = float(t * 2)
                    cores = make_graph(15 + (t % 8), wave=True)
                    cpu = data['system_cpu_samples'][t % len(data['system_cpu_samples'])]
                    res = detector.observe(
                        now,
                        flat,
                        cores,
                        system_cpu_percent=cpu,
                        native_corroborated=data['native_corroborated'],
                    )
                    results.append(res)

                # All 4 real incidents must advance past candidate and trigger alert at >= 30s
                alerts = [r for r in results if r['alert']]
                self.assertEqual(len(alerts), 1, f"Incident {inc_id} did not alert once")
                self.assertEqual(alerts[0]['state'], 'suspect')

    def test_record_outside_flatline_increments_count_and_persists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            summary_dir = Path(tmpdir)
            native_err = {
                'timestamp_utc': '2026-10-30T01:00:00+00:00',
                'timestamp_qpc': 1234567890,
                'taskmgr_pid': 4321,
                'thread_id': 9876,
                'hresult': '0x8007139f',
                'module': 'TaskManagerDataLayer.dll',
            }

            s1 = IA.record_outside_flatline(summary_dir, native_err, 'chart_not_flat_for_10s')
            self.assertEqual(s1['outside_flatlines_count'], 1)
            self.assertEqual(len(s1['outside_flatlines']), 1)
            self.assertEqual(s1['outside_flatlines'][0]['taskmgr_pid'], 4321)
            self.assertEqual(s1['outside_flatlines'][0]['reason'], 'chart_not_flat_for_10s')

            # Second outside flatline
            native_err2 = dict(native_err, timestamp_qpc=1234567999)
            s2 = IA.record_outside_flatline(summary_dir, native_err2, 'chart_not_flat_for_10s')
            self.assertEqual(s2['outside_flatlines_count'], 2)
            self.assertEqual(len(s2['outside_flatlines']), 2)

            # Verify reload from disk
            loaded = json.loads((summary_dir / 'n1-summary.json').read_text(encoding='utf-8'))
            self.assertEqual(loaded['outside_flatlines_count'], 2)

    def test_automatic_watch_outside_flatline_timeout_records_event(self):
        os.environ['TASKMGR_WATCH_TEST'] = '1'
        import AutomaticWatch as AW

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir)
            with patch('AutomaticWatch.OUTPUT', out_path), \
                 patch('AutomaticWatch.record_outside_flatline') as mock_record:
                watch = AW.Monitor(
                    query_trace=False,
                    restart_only=True,
                    light_etw=False,
                )
                watch.detector = FlatlineDetector(seconds=30)
                watch.realtime = MagicMock()
                watch.realtime.status.return_value = {
                    'state': 'running',
                    'session': 'test',
                    'target_pid': 1234,
                    'provider_event_count': 0,
                    'last_provider_event_utc': None,
                    'last_provider_event_qpc': None,
                }

                now = 100.0
                native_err = {
                    'timestamp_monotonic': 70.0,  # 30s ago (> 25s timeout)
                    'timestamp_utc': '2026-10-30T01:00:00+00:00',
                    'timestamp_qpc': 50000000,
                    'taskmgr_pid': 1234,
                    'thread_id': 5678,
                    'hresult': '0x8007139f',
                    'module': 'TaskManagerDataLayer.dll',
                }
                watch.realtime.poll_native_error.return_value = native_err

                # Feed a sample with a normal moving wave chart (no flatline)
                wave_chart = make_graph(20, wave=True)
                cores = make_graph(15, wave=True)
                sample = {
                    'state': 'captured',
                    'pid': 1234,
                    'hwnd': 9999,
                    'total': wave_chart,
                    'cores': cores,
                    'geometry': [None] * 33,
                    'system_cpu_percent': 25.0,
                }

                with patch('time.monotonic', return_value=now):
                    watch.accept(sample)

                # Verify clear_native_error was called and record_outside_flatline was invoked
                watch.realtime.clear_native_error.assert_called_once()
                mock_record.assert_called_once_with(out_path, native_err, 'chart_not_flat_for_10s')

    def test_realtime_consumer_qpc_utc_delay_compensation(self):
        import ctypes
        import RealtimeTrace as RT

        # Simulate EVENT_HEADER where TimeStamp is QPC tick 1.35s earlier than callback time
        freq = 10_000_000  # 10 MHz
        event_qpc = 1_000_000_000
        delivery_qpc = event_qpc + int(1.35 * freq)  # 1.35 seconds later

        with patch('RealtimeTrace.frequency', return_value=freq), \
             patch('RealtimeTrace.qpc', return_value=delivery_qpc), \
             patch('RealtimeTrace.datetime') as mock_dt, \
             patch('RealtimeTrace.time.monotonic', return_value=50.0):

            # Callback delivery time
            callback_utc = datetime(2026, 10, 30, 1, 0, 1, 350000, tzinfo=timezone.utc)
            mock_dt.now.return_value = callback_utc
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.timezone = timezone

            consumer = RT.RealtimeConsumer(target_pid=1234)
            consumer.enabled = False  # Avoid real ETW start

            payload = (b'prefix_' + RT.SIGNATURE_HRESULT_BYTES +
                       b'_mid_' + RT.SIGNATURE_MODULE_BYTES + b'_suffix')
            raw_buf = ctypes.create_string_buffer(payload)

            class Header:
                ProviderId = RT.parse_guid(RT.TASKMGR_GUID)
                ProcessId = 1234
                ThreadId = 5678
                TimeStamp = event_qpc

            class Record:
                EventHeader = Header()
                UserData = ctypes.addressof(raw_buf)
                UserDataLength = len(payload)

            rec = Record()
            consumer._on_event(rec)

            err = consumer.poll_native_error()
            self.assertIsNotNone(err)
            self.assertEqual(err['timestamp_qpc'], event_qpc)
            # The UTC timestamp must be compensated back by 1.35s to 01:00:00.000000
            self.assertEqual(err['timestamp_utc'], '2026-10-30T01:00:00+00:00')
            self.assertAlmostEqual(err['timestamp_monotonic'], 50.0 - 1.35, places=3)

    def test_protected_test_paths_raise_isolation_violation(self):
        import AutomaticWatch as AW
        # In test environment, writes to production evidence or repo .local must be rejected
        with self.assertRaises(RuntimeError) as ctx1:
            AW.assert_not_protected_test_path(Path('D:/TaskmgrFreezeCaptures/status.json'))
        self.assertIn('TEST ISOLATION VIOLATION', str(ctx1.exception))

        with self.assertRaises(RuntimeError) as ctx2:
            AW.assert_not_protected_test_path(AW.REPO_LOCAL / 'status.json')
        self.assertIn('TEST ISOLATION VIOLATION', str(ctx2.exception))

        with self.assertRaises(RuntimeError) as ctx3:
            AW.write_json(Path('D:/TaskmgrFreezeCaptures/target-dll-baseline.json'), {'polluted': True})
        self.assertIn('TEST ISOLATION VIOLATION', str(ctx3.exception))

    def test_realtime_consumer_filetime_parsing_aligns_utc(self):
        import ctypes
        from datetime import timedelta
        import RealtimeTrace as RT

        target_dt = datetime(2026, 10, 30, 14, 5, 41, 881880, tzinfo=timezone.utc)
        epoch_1601 = datetime(1601, 1, 1, tzinfo=timezone.utc)
        ft_us = int((target_dt - epoch_1601).total_seconds() * 1_000_000)
        filetime_100ns = ft_us * 10

        consumer = RT.RealtimeConsumer(target_pid=62476)
        consumer.enabled = False

        payload = (b'prefix_' + RT.SIGNATURE_HRESULT_BYTES +
                   b'_mid_' + RT.SIGNATURE_MODULE_BYTES + b'_suffix')
        raw_buf = ctypes.create_string_buffer(payload)

        class Header:
            ProviderId = RT.parse_guid(RT.TASKMGR_GUID)
            ProcessId = 62476
            ThreadId = 39412
            TimeStamp = filetime_100ns

        class Record:
            EventHeader = Header()
            UserData = ctypes.addressof(raw_buf)
            UserDataLength = len(payload)

        rec = Record()
        consumer._on_event(rec)

        err = consumer.poll_native_error()
        self.assertIsNotNone(err)
        # Direct FILETIME conversion accurately aligns UTC without delivery lag
        self.assertEqual(err['timestamp_utc'], '2026-10-30T14:05:41.881880+00:00')

    def test_incident_analysis_lead_seconds_field_and_utc_alignment(self):
        # Test using the actual 14:06 incident if available on disk
        incident_path = Path('D:/TaskmgrFreezeCaptures/restart-20261030-140608-487205')
        if not incident_path.exists():
            self.skipTest('Incident folder not found on disk')

        obs = json.loads((incident_path / 'observation.json').read_text(encoding='utf-8'))
        event = json.loads((incident_path / 'event.json').read_text(encoding='utf-8'))

        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d)
            # Run analyze into a temporary directory
            res = IA.analyze(incident_path, output=out_dir)
            native_sig = res['native_signature']
            self.assertIsNotNone(native_sig)
            self.assertIn('lead_seconds', native_sig)
            lead_sec = native_sig['lead_seconds']
            self.assertIsNotNone(lead_sec)
            self.assertGreaterEqual(lead_sec, 14.0)
            self.assertLessEqual(lead_sec, 16.0)
            self.assertEqual(lead_sec, native_sig['lead_seconds_before_first_candidate'])


if __name__ == '__main__':
    unittest.main()
