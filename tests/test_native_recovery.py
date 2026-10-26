import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import AutomaticWatch as watch
from RealtimeTrace import RealtimeConsumer, TASKMGR_GUID, SIGNATURE_HRESULT_BYTES, SIGNATURE_MODULE_BYTES
from test_detection import graph

class FakeRealtimeConsumer:
    def __init__(self, target_pid=None):
        self.target_pid = target_pid
        self.pending_native_error = None
        self.active = True
        self.started = False
        self.stopped = False

    def set_target_pid(self, pid):
        if self.target_pid != pid:
            self.target_pid = pid
            self.pending_native_error = None

    def start(self):
        self.started = True
        self.active = True

    def stop(self, timeout=3.0):
        self.stopped = True
        self.active = False

    def poll_native_error(self):
        return dict(self.pending_native_error) if self.pending_native_error else None

    def clear_native_error(self):
        err = self.pending_native_error
        self.pending_native_error = None
        return err

    def status(self):
        return {'state': 'running' if self.active else 'stopped', 'session': 'fake-session',
                'has_pending_error': bool(self.pending_native_error), 'target_pid': self.target_pid}

    def simulate_native_error(self, pid=None, hresult='0x8007139F', module='TaskManagerDataLayer.dll',
                              timestamp_monotonic=None, thread_id=1234):
        self.pending_native_error = {
            'hresult': hresult,
            'module': module,
            'taskmgr_pid': pid or self.target_pid,
            'thread_id': thread_id,
            'timestamp_utc': '2026-10-26T21:00:00.000000+00:00',
            'timestamp_qpc': 1000000000,
            'timestamp_monotonic': time.monotonic() if timestamp_monotonic is None else timestamp_monotonic
        }


class NativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(watch.Monitor, 'watch_origin')
        guard.start()
        self.addCleanup(guard.stop)

    def _create_monitor(self, target_pid=4321):
        monitor = watch.Monitor(restart_only=True)
        monitor.light_etw = True
        mock_rec = MagicMock()
        mock_rec.status.return_value = {'state': 'running'}
        monitor.recorder = mock_rec
        fake_rt = FakeRealtimeConsumer(target_pid=target_pid)
        monitor.realtime = fake_rt
        return monitor, fake_rt

    def test_native_signal_plus_flat_triggers_fast_recovery(self):
        """Native signal + 10s of flat total chart triggers early recovery before 30s."""
        with tempfile.TemporaryDirectory() as d, \
             patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch.time, 'monotonic', return_value=1000.0) as clock, \
             patch.object(watch.threading, 'Thread') as mock_thread:
            monitor, fake_rt = self._create_monitor(target_pid=4321)

            flat_total = graph(15, wave=False)
            cores_moving = graph(20, wave=True)

            # Frame at t=1000: initial observation
            clock.return_value = 1000.0
            monitor.accept({'state': 'captured', 'pid': 4321, 'hwnd': 100, 'geometry': [[0,0,75,50]]*33,
                            'total': flat_total, 'cores': cores_moving})

            # Native signal arrives at t=1000.0
            fake_rt.simulate_native_error(pid=4321, timestamp_monotonic=1000.0)

            # Advance in 2-second steps until t=1008: flat for 8s (not yet 10s)
            for t in (1002.0, 1004.0, 1006.0, 1008.0):
                clock.return_value = t
                monitor.accept({'state': 'captured', 'pid': 4321, 'hwnd': 100, 'geometry': [[0,0,75,50]]*33,
                                'total': flat_total, 'cores': graph(int(t) % 10 + 15, wave=True)})
                mock_thread.assert_not_called()

            # At t=1010.0: flat for 10s since native error -> triggers fast recovery!
            clock.return_value = 1010.0
            monitor.accept({'state': 'captured', 'pid': 4321, 'hwnd': 100, 'geometry': [[0,0,75,50]]*33,
                            'total': flat_total, 'cores': graph(25, wave=True)})

            mock_thread.assert_called_once()
            event_path = Path(monitor.last_event) / 'event.json'
            event = json.loads(event_path.read_text(encoding='utf-8'))
            self.assertEqual(event['trigger_kind'], 'native_fast')
            self.assertEqual(event['confirm_seconds'], 10.0)
            self.assertEqual(event['visual_corroboration_seconds'], 10.0)
            self.assertEqual(event['native_event_time'], '2026-10-26T21:00:00.000000+00:00')

    def test_native_signal_plus_not_flat_does_not_recover(self):
        """Native signal without 10s of flat total chart does not trigger recovery."""
        with tempfile.TemporaryDirectory() as d, \
             patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch.time, 'monotonic', return_value=1000.0) as clock, \
             patch.object(watch.threading, 'Thread') as mock_thread:
            monitor, fake_rt = self._create_monitor(target_pid=4321)

            # Native signal arrives at t=1000
            fake_rt.simulate_native_error(pid=4321, timestamp_monotonic=1000.0)

            # Chart is fluctuating (wave=True, changing height)
            for t in range(1000, 1030, 2):
                clock.return_value = float(t)
                fluctuating_total = graph((t % 10) + 10, wave=True)
                monitor.accept({'state': 'captured', 'pid': 4321, 'hwnd': 100, 'geometry': [[0,0,75,50]]*33,
                                'total': fluctuating_total, 'cores': graph(20, wave=True)})

            # Recovery should NOT have triggered
            mock_thread.assert_not_called()
            # Pending native error should have been cleared after timeout
            self.assertIsNone(fake_rt.poll_native_error())
            # Observation should have recorded uncorroborated event
            self.assertGreaterEqual(len(monitor.observations.uncorroborated_native_errors), 1)
            self.assertEqual(monitor.observations.uncorroborated_native_errors[0]['reason'], 'chart_not_flat_for_10s')

    def test_other_pid_native_error_ignored(self):
        """Native error from another Taskmgr PID does not trigger fast recovery."""
        with tempfile.TemporaryDirectory() as d, \
             patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch.time, 'monotonic', return_value=1000.0) as clock, \
             patch.object(watch.threading, 'Thread') as mock_thread:
            monitor, fake_rt = self._create_monitor(target_pid=4321)

            flat_total = graph(15, wave=False)
            cores_moving = graph(20, wave=True)

            # Native error on PID 9999 (not current 4321)
            fake_rt.simulate_native_error(pid=9999, timestamp_monotonic=1000.0)

            for t in (1000.0, 1002.0, 1004.0, 1006.0, 1008.0, 1010.0):
                clock.return_value = t
                monitor.accept({'state': 'captured', 'pid': 4321, 'hwnd': 100, 'geometry': [[0,0,75,50]]*33,
                                'total': flat_total, 'cores': cores_moving})

            mock_thread.assert_not_called()

    def test_visual_fallback_path(self):
        """When no native signal occurs, 30s visual flatline triggers visual path."""
        with tempfile.TemporaryDirectory() as d, \
             patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch.time, 'monotonic', return_value=1000.0) as clock, \
             patch.object(watch.threading, 'Thread') as mock_thread:
            monitor, fake_rt = self._create_monitor(target_pid=4321)

            flat_total = graph(15, wave=False)
            cores_moving = graph(20, wave=True)

            # 32 seconds of flatline (no native error)
            for t in range(0, 34, 2):
                clock.return_value = 1000.0 + t
                monitor.accept({'state': 'captured', 'pid': 4321, 'hwnd': 100, 'geometry': [[0,0,75,50]]*33,
                                'total': flat_total, 'cores': graph((t % 8) + 15, wave=True)})

            mock_thread.assert_called_once()
            event_path = Path(monitor.last_event) / 'event.json'
            event = json.loads(event_path.read_text(encoding='utf-8'))
            self.assertEqual(event['trigger_kind'], 'visual')
            self.assertEqual(event['confirm_seconds'], 30)

    def test_cooldown_and_duplicate_events_only_recovers_once(self):
        """Native signal respects cooldown on the same target and only recovers once."""
        with tempfile.TemporaryDirectory() as d, \
             patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch.time, 'monotonic', return_value=1000.0) as clock, \
             patch.object(watch.threading, 'Thread') as mock_thread:
            monitor, fake_rt = self._create_monitor(target_pid=4321)

            flat_total = graph(15, wave=False)
            cores_moving = graph(20, wave=True)

            # First fast recovery at t=1010
            fake_rt.simulate_native_error(pid=4321, timestamp_monotonic=1000.0)
            for t in range(0, 12, 2):
                clock.return_value = 1000.0 + t
                monitor.accept({'state': 'captured', 'pid': 4321, 'hwnd': 100, 'geometry': [[0,0,75,50]]*33,
                                'total': flat_total, 'cores': cores_moving})

            self.assertEqual(mock_thread.call_count, 1)

            # Simulate recovery thread finished
            monitor.dump_thread = None

            # Another native error arrives within cooldown period at t=1020
            clock.return_value = 1020.0
            fake_rt.simulate_native_error(pid=4321, timestamp_monotonic=1020.0)

            # Flat for 10s until t=1030
            for t in range(20, 32, 2):
                clock.return_value = 1000.0 + t
                monitor.accept({'state': 'captured', 'pid': 4321, 'hwnd': 100, 'geometry': [[0,0,75,50]]*33,
                                'total': flat_total, 'cores': cores_moving})

            # Still only 1 call to restart! Cooldown protected.
            self.assertEqual(mock_thread.call_count, 1)



    def test_other_hresult_native_error_ignored(self):
        """Native error with another HRESULT (e.g. 0x8000FFFF) does not trigger fast recovery."""
        with tempfile.TemporaryDirectory() as d, \
             patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch.time, 'monotonic', return_value=1000.0) as clock, \
             patch.object(watch.threading, 'Thread') as mock_thread:
            monitor, fake_rt = self._create_monitor(target_pid=4321)

            flat_total = graph(15, wave=False)
            cores_moving = graph(20, wave=True)

            # Different HRESULT
            fake_rt.simulate_native_error(pid=4321, hresult='0x8000FFFF', timestamp_monotonic=1000.0)

            for t in (1000.0, 1002.0, 1004.0, 1006.0, 1008.0, 1010.0):
                clock.return_value = t
                monitor.accept({'state': 'captured', 'pid': 4321, 'hwnd': 100, 'geometry': [[0,0,75,50]]*33,
                                'total': flat_total, 'cores': cores_moving})

            # Fast recovery must not trigger for wrong HRESULT
            mock_thread.assert_not_called()

    def test_realtime_consumer_event_filtering(self):
        """RealtimeConsumer._on_event accepts only matching PID, GUID, 0x8007139F and module."""
        import ctypes
        from RealtimeTrace import EVENT_RECORD, EVENT_HEADER, GUID, parse_guid
        consumer = RealtimeConsumer(target_pid=5555)

        # Helper to construct dummy EVENT_RECORD
        def make_record(pid, guid_str, user_bytes):
            rec = EVENT_RECORD()
            rec.EventHeader.ProcessId = pid
            rec.EventHeader.ProviderId = parse_guid(guid_str)
            if user_bytes:
                buf = ctypes.create_string_buffer(user_bytes)
                rec.UserData = ctypes.cast(buf, ctypes.c_void_p)
                rec.UserDataLength = len(user_bytes)
            else:
                rec.UserData = None
                rec.UserDataLength = 0
            rec._buf = user_bytes # prevent GC
            return rec

        matching_payload = b'\x00\x00\x00\x00' + SIGNATURE_HRESULT_BYTES + b'...' + SIGNATURE_MODULE_BYTES + b'\x00'
        wrong_payload = b'\x00\x00\x00\x00\x00\x00\x00\x00other_data'

        # 1. Matching event
        rec1 = make_record(5555, TASKMGR_GUID, matching_payload)
        consumer._on_event(ctypes.pointer(rec1))
        self.assertIsNotNone(consumer.poll_native_error())
        self.assertEqual(consumer.poll_native_error()['taskmgr_pid'], 5555)
        consumer.clear_native_error()

        # 2. Wrong PID
        rec2 = make_record(9999, TASKMGR_GUID, matching_payload)
        consumer._on_event(ctypes.pointer(rec2))
        self.assertIsNone(consumer.poll_native_error())

        # 3. Wrong GUID
        rec3 = make_record(5555, '{11111111-2222-3333-4444-555555555555}', matching_payload)
        consumer._on_event(ctypes.pointer(rec3))
        self.assertIsNone(consumer.poll_native_error())

        # 4. Wrong payload (missing 0x8007139F)
        rec4 = make_record(5555, TASKMGR_GUID, wrong_payload)
        consumer._on_event(ctypes.pointer(rec4))
        self.assertIsNone(consumer.poll_native_error())

if __name__ == '__main__':
    unittest.main()
