"""Unit tests for RealtimeConsumer resilience, locking discipline, and auto-recovery."""
import ctypes
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

os.environ['TASKMGR_WATCH_TEST'] = '1'

from RealtimeTrace import RealtimeConsumer, EVENT_RECORD, parse_guid, TASKMGR_GUID
from AutomaticWatch import Monitor


class RealtimeConsumerResilienceTests(unittest.TestCase):
    def test_same_pid_repeated_set_does_not_re_enable_provider(self):
        """Calling set_target_pid with the same PID is a no-op and does not call re_enable_provider."""
        consumer = RealtimeConsumer(target_pid=1234, enabled=False)
        consumer.active = True
        consumer.session_handle = ctypes.c_uint64(42)

        with patch.object(consumer, 're_enable_provider') as mock_re_enable:
            # Set to same PID 1234 multiple times
            consumer.set_target_pid(1234)
            consumer.set_target_pid(1234)
            consumer.set_target_pid(1234)
            mock_re_enable.assert_not_called()

            # Even when setting to a new PID, re_enable_provider is not called directly
            # by set_target_pid; watchdog handles backoff re-enabling
            consumer.set_target_pid(5678)
            mock_re_enable.assert_not_called()
            self.assertEqual(consumer.target_pid, 5678)

    def test_logfile_and_callback_ref_persisted_on_instance(self):
        """start() keeps _logfile and _callback_ref on self to prevent GC corruption."""
        consumer = RealtimeConsumer(target_pid=1234, enabled=False)
        self.assertIsNone(consumer._logfile)
        self.assertIsNone(consumer._callback_ref)

        with patch('RealtimeTrace.advapi32') as mock_advapi:
            mock_advapi.StartTraceW.return_value = 0
            mock_advapi.EnableTraceEx2.return_value = 0
            mock_advapi.OpenTraceW.return_value = 0x1234
            mock_advapi.ControlTraceW.return_value = 0
            mock_advapi.CloseTrace.return_value = 0

            # Temporarily bypass test_environment check for start()
            with patch('RealtimeTrace.is_test_environment', return_value=False), \
                 patch('threading.Thread'):
                consumer.enabled = True
                consumer.start()
                self.assertIsNotNone(consumer._logfile)
                self.assertIsNotNone(consumer._callback_ref)
                self.assertEqual(consumer.status()['state'], 'running')

                consumer.stop()
                self.assertIsNone(consumer._logfile)
                self.assertEqual(consumer.status()['state'], 'stopped')

    def test_re_enable_provider_does_not_hold_lock_during_win32_call(self):
        """re_enable_provider releases self.lock before calling advapi32.EnableTraceEx2."""
        consumer = RealtimeConsumer(target_pid=1234, enabled=False)
        consumer.enabled = True
        consumer.session_handle = ctypes.c_uint64(999)

        lock_was_held = None

        def fake_enable_trace(*args, **kwargs):
            nonlocal lock_was_held
            # Verify whether consumer.lock can be acquired immediately (meaning it is NOT held)
            acquired = consumer.lock.acquire(blocking=False)
            if acquired:
                lock_was_held = False
                consumer.lock.release()
            else:
                lock_was_held = True
            return 0

        with patch('RealtimeTrace.advapi32.EnableTraceEx2', side_effect=fake_enable_trace):
            consumer.re_enable_provider()

        self.assertFalse(lock_was_held, "EnableTraceEx2 was called while self.lock was still held!")

    def test_concurrent_callbacks_and_re_enable_do_not_deadlock(self):
        """High-frequency record_provider_event and re_enable_provider across threads do not deadlock."""
        consumer = RealtimeConsumer(target_pid=1234, enabled=False)
        consumer.enabled = True
        consumer.session_handle = ctypes.c_uint64(777)

        stop_event = threading.Event()
        errors = []

        def worker_callbacks():
            try:
                for _ in range(500):
                    if stop_event.is_set():
                        break
                    consumer.record_provider_event(mono_val=time.monotonic())
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def worker_re_enable():
            try:
                with patch('RealtimeTrace.advapi32.EnableTraceEx2', return_value=0):
                    for _ in range(100):
                        if stop_event.is_set():
                            break
                        consumer.re_enable_provider()
                        time.sleep(0.003)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker_callbacks)
        t2 = threading.Thread(target=worker_re_enable)

        t1.start()
        t2.start()

        t1.join(timeout=5.0)
        t2.join(timeout=5.0)
        stop_event.set()

        self.assertFalse(t1.is_alive(), "worker_callbacks thread deadlocked!")
        self.assertFalse(t2.is_alive(), "worker_re_enable thread deadlocked!")
        self.assertEqual(errors, [])
        self.assertGreater(consumer.status()['provider_event_count'], 0)

    def test_thread_exit_detected_and_reported_as_failed(self):
        """When the consumer thread exits, status() immediately reflects state='failed'."""
        consumer = RealtimeConsumer(target_pid=1234, enabled=False)
        consumer.active = True
        consumer.state = {'state': 'running', 'session': consumer.name}

        # A dead dummy thread
        dead_thread = threading.Thread(target=lambda: None)
        dead_thread.start()
        dead_thread.join()

        consumer.thread = dead_thread
        st = consumer.status()

        self.assertEqual(st['state'], 'failed')
        self.assertFalse(st['thread_alive'])
        self.assertIn('error', st)

    def test_status_reports_callback_metrics_and_records_callback_errors(self):
        """status() truthfully exposes thread_alive, last_callback_utc, and callback_error_count."""
        consumer = RealtimeConsumer(target_pid=4321, enabled=False)
        st_init = consumer.status()
        self.assertIn('thread_alive', st_init)
        self.assertIn('last_callback_utc', st_init)
        self.assertIn('callback_error_count', st_init)
        self.assertEqual(st_init['callback_error_count'], 0)
        self.assertIsNone(st_init['last_callback_utc'])

        # Injecting an invalid memory record causes exception in _on_event
        # which must increment callback_error_count rather than crashing
        consumer._on_event(ctypes.c_void_p(0))
        st_after = consumer.status()
        self.assertEqual(st_after['callback_error_count'], 1)
        self.assertIsNotNone(st_after['last_callback_utc'])

    def test_auto_recovery_rebuilds_failed_consumer_session(self):
        """AutomaticWatch watchdog detects failed realtime consumer and recreates the session."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            monitor = Monitor(light_etw=False, output=d)
            monitor.light_etw = True
            fake_realtime = RealtimeConsumer(target_pid=1234, enabled=False)
            fake_realtime.state = {'state': 'failed', 'session': 'TaskmgrRealtime-Live', 'error': 'simulated failure'}
            monitor.realtime = fake_realtime
            monitor.state = {'taskmgr_pid': 1234, 'state': 'normal'}

            restarted = False
            def mock_restart():
                nonlocal restarted
                restarted = True
                fake_realtime.state = {'state': 'running', 'session': 'TaskmgrRealtime-Live'}

            fake_realtime.restart = mock_restart

            # 1. Check tick auto-recreation
            monitor.tick()
            self.assertTrue(restarted)

            # 2. Check re_enable_native_providers auto-recreation
            restarted = False
            fake_realtime.state = {'state': 'failed', 'session': 'TaskmgrRealtime-Live'}
            monitor.re_enable_native_providers()
            self.assertTrue(restarted)

            # 3. Check check_native_coverage auto-recreation
            restarted = False
            fake_realtime.state = {'state': 'failed', 'session': 'TaskmgrRealtime-Live'}
            monitor.check_native_coverage(now=time.monotonic())
            self.assertTrue(restarted)


if __name__ == '__main__':
    unittest.main()
