"""Tests for detecting and restoring minimized Task Manager window with SW_SHOWNOACTIVATE and A8 backoff."""
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
import FlatlineWatch as flatline_watch
import NativeCapture as cap


class MinimizedWindowTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(watch.Monitor, 'watch_origin')
        guard.start()
        self.addCleanup(guard.stop)

    def test_native_capture_detects_minimized_taskmgr(self):
        """When Task Manager is minimized, NativeCapture.sample() returns state='minimized' with hwnd and pid."""
        with patch.object(cap, 'locate', return_value=None), \
             patch.object(cap, 'find_minimized_taskmgr', return_value=(591298, 60044)):
            sample = cap.sample()
            self.assertEqual(sample['state'], 'minimized')
            self.assertEqual(sample['hwnd'], 591298)
            self.assertEqual(sample['pid'], 60044)

    def test_minimized_window_restores_immediately_when_user_idle(self):
        """When user is idle (idle >= 10s), minimized Task Manager is restored immediately via SW_SHOWNOACTIVATE."""
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_user_interacting_system', return_value=False):
            monitor = watch.Monitor()
            restored_hwnds = []
            monitor._mock_restore_minimized = lambda hwnd: restored_hwnds.append(hwnd) or True

            now = time.monotonic()
            sample = {'state': 'minimized', 'hwnd': 591298, 'pid': 60044}
            monitor.accept(sample)

            # Restored immediately
            self.assertEqual(restored_hwnds, [591298])
            self.assertEqual(monitor.state['state'], 'minimized')
            self.assertEqual(monitor.minimized_retry_count, 0)
            self.assertIsNone(monitor.next_minimized_retry_monotonic)
            self.assertIsNotNone(monitor.waiting_since_monotonic)
            self.assertEqual(monitor.current_blind_interval['reason'], 'window_minimized')

            # status.json must reflect state='minimized' and blind_seconds
            status_data = json.loads((Path(d) / 'status.json').read_text(encoding='utf-8'))
            self.assertEqual(status_data['state'], 'minimized')

    def test_minimized_window_backs_off_with_a8_when_user_interacting(self):
        """When user is interacting with system, restoration defers with A8 exponential backoff schedule."""
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_user_interacting_system', return_value=True):
            monitor = watch.Monitor()
            restored_hwnds = []
            monitor._mock_restore_minimized = lambda hwnd: restored_hwnds.append(hwnd) or True

            now = time.monotonic()
            sample = {'state': 'minimized', 'hwnd': 591298, 'pid': 60044}
            monitor.accept(sample)

            # Not restored because user is interacting
            self.assertEqual(restored_hwnds, [])
            self.assertEqual(monitor.state['state'], 'minimized')
            self.assertEqual(monitor.minimized_retry_count, 1)
            # A8 first backoff step is 120s
            self.assertIsNotNone(monitor.next_minimized_retry_monotonic)
            self.assertAlmostEqual(monitor.next_minimized_retry_monotonic - now, 120.0, delta=2.0)
            self.assertIsNotNone(monitor.next_minimized_retry_utc)

            # Status JSON exposes next_navigation_retry reflecting the backoff
            status_data = json.loads((Path(d) / 'status.json').read_text(encoding='utf-8'))
            self.assertEqual(status_data['next_navigation_retry'], monitor.next_minimized_retry_utc)

            # While backoff is active, subsequent samples do not trigger restore
            monitor.accept(sample)
            self.assertEqual(restored_hwnds, [])
            self.assertEqual(monitor.minimized_retry_count, 1)

    def test_minimized_window_restores_after_backoff_expires_when_user_becomes_idle(self):
        """After A8 backoff timer expires, once user is idle, window is restored and backoff clears."""
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)):
            monitor = watch.Monitor()
            restored_hwnds = []
            monitor._mock_restore_minimized = lambda hwnd: restored_hwnds.append(hwnd) or True

            # First tick: user active -> back off
            with patch.object(watch, 'is_user_interacting_system', return_value=True):
                now = time.monotonic()
                sample = {'state': 'minimized', 'hwnd': 591298, 'pid': 60044}
                monitor.accept(sample)
                self.assertEqual(restored_hwnds, [])
                self.assertEqual(monitor.minimized_retry_count, 1)

            # Clock advances past 120s; user becomes idle
            with patch.object(watch, 'is_user_interacting_system', return_value=False), \
                 patch.object(watch, 'is_taskmgr_process_alive', return_value=True):
                later = now + 125.0
                monitor.check_minimized_window(later)
                self.assertEqual(restored_hwnds, [591298])
                self.assertEqual(monitor.minimized_retry_count, 0)
                self.assertIsNone(monitor.next_minimized_retry_monotonic)

    def test_minimized_window_blind_interval_ends_upon_captured(self):
        """Blind interval recorded during minimization closes cleanly once window is restored and captured."""
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_user_interacting_system', return_value=False):
            monitor = watch.Monitor()
            monitor._mock_restore_minimized = lambda hwnd: True

            sample_min = {'state': 'minimized', 'hwnd': 591298, 'pid': 60044}
            monitor.accept(sample_min)
            self.assertIsNotNone(monitor.current_blind_interval)
            self.assertEqual(monitor.current_blind_interval['reason'], 'window_minimized')

            # Next sample arrives as captured
            import numpy as np
            sample_cap = {
                'state': 'captured', 'hwnd': 591298, 'pid': 60044,
                'total': np.zeros((10, 10, 3), dtype=np.uint8),
                'cores': np.zeros((10, 10, 3), dtype=np.uint8),
                'geometry': []
            }
            with patch.object(monitor.detector, 'observe', return_value={'state': 'normal', 'streak': 1, 'seconds': 0.0}):
                monitor.accept(sample_cap)

            self.assertIsNone(monitor.current_blind_interval)
            self.assertIsNone(monitor.waiting_since_monotonic)
            self.assertEqual(len(monitor.observations.blind_intervals), 1)
            blind_rec = monitor.observations.blind_intervals[0]
            self.assertEqual(blind_rec['reason'], 'window_minimized')
            self.assertEqual(blind_rec['taskmgr_pid'], 60044)


class MinimizedTrayUiTests(unittest.TestCase):
    def test_minimized_text_bilingual(self):
        """Tray UI displays '已最小化，将自动还原' in Chinese and 'Minimized; will restore automatically' in English."""
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PySide6 not available")
        app = QApplication.instance() or QApplication(sys.argv)
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'ROOT', Path(d)), \
             patch.object(watch.Monitor, 'watch_origin'):
            (Path(d) / 'assets').mkdir()
            (Path(d) / 'assets/icon.png').write_bytes(b'\x89PNG\r\n\x1a\n')
            (Path(d) / '.local').mkdir()
            w = flatline_watch.Watch()
            try:
                # Chinese
                w.language.setCurrentIndex(0)
                w.monitor.state = {
                    'state': 'minimized',
                    'seconds': 0,
                    'blind_seconds': 5,
                    'auto_open': {}
                }
                w.update_status()
                self.assertIn('已最小化，将自动还原', w.status.text())

                # English
                w.language.setCurrentIndex(1)
                w.update_status()
                self.assertIn('Minimized; will restore automatically', w.status.text())
            finally:
                w.timer.stop()
                w.monitor.stop()
                w.tray.hide()


if __name__ == '__main__':
    unittest.main()
