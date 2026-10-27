"""Tests for auto-launching Task Manager on boot and restoring CPU page with A8 backoff (A10)."""
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


class AutoCpuPageTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(watch.Monitor, 'watch_origin')
        guard.start()
        self.addCleanup(guard.stop)

    def test_boot_auto_open_when_no_instance(self):
        """Monitor starts on boot with no Taskmgr running -> auto opens Taskmgr once desktop ready."""
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_desktop_ready', return_value=True), \
             patch.object(watch, 'launch_taskmgr', return_value={'status': 'restarted', 'launch_pid': 1234}) as mock_launch:
            monitor = watch.Monitor()
            monitor._mock_taskmgr_pids = []
            now = time.monotonic()
            # First tick initializes boot check
            monitor.tick()
            self.assertTrue(monitor.boot_auto_open_checked)
            self.assertFalse(monitor.taskmgr_was_running)
            self.assertIsNotNone(monitor.next_auto_open_monotonic)
            self.assertEqual(monitor.auto_open_reason, 'boot_startup')

            # Advance clock past scheduled launch delay
            monitor.check_auto_open(now + 5.0)
            if monitor.dump_thread:
                monitor.dump_thread.join(timeout=2)

            mock_launch.assert_called_once()
            self.assertEqual(monitor.auto_open_count, 1)
            self.assertEqual(monitor.auto_open_last_result, 'restarted')
            self.assertTrue(monitor.taskmgr_was_running)
            self.assertIsNone(monitor.next_auto_open_monotonic)

    def test_boot_auto_open_waits_for_desktop_ready(self):
        """Boot auto open does not launch if desktop is locked or explorer not running."""
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_desktop_ready', side_effect=[False, True]), \
             patch.object(watch, 'launch_taskmgr', return_value={'status': 'restarted', 'launch_pid': 1234}) as mock_launch:
            monitor = watch.Monitor()
            monitor._mock_taskmgr_pids = []
            now = time.monotonic()
            monitor.tick()
            self.assertIsNotNone(monitor.next_auto_open_monotonic)

            # First attempt: desktop not ready
            monitor.check_auto_open(now + 5.0)
            mock_launch.assert_not_called()
            self.assertIsNotNone(monitor.next_auto_open_monotonic)

            # Second attempt: desktop ready
            monitor.check_auto_open(now + 6.0)
            if monitor.dump_thread:
                monitor.dump_thread.join(timeout=2)
            mock_launch.assert_called_once()

    def test_auto_open_skips_when_user_interacting_system(self):
        """Auto open defers by 60s if user is actively interacting with the system."""
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_desktop_ready', return_value=True), \
             patch.object(watch, 'is_user_interacting_system', return_value=True), \
             patch.object(watch, 'launch_taskmgr') as mock_launch:
            monitor = watch.Monitor()
            monitor._mock_taskmgr_pids = []
            now = time.monotonic()
            monitor.boot_auto_open_checked = True
            monitor.taskmgr_was_running = False
            monitor.next_auto_open_monotonic = now - 1

            monitor.check_auto_open(now)
            mock_launch.assert_not_called()
            self.assertIsNone(monitor.dump_thread)
            self.assertAlmostEqual(monitor.next_auto_open_monotonic - now, 60.0, delta=2)

    def test_user_closed_taskmgr_waits_10_minutes_cooldown(self):
        """When Taskmgr was running and exits, monitor waits 10 minutes before auto-opening."""
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_desktop_ready', return_value=True), \
             patch.object(watch, 'launch_taskmgr', return_value={'status': 'restarted', 'launch_pid': 4321}) as mock_launch:
            monitor = watch.Monitor()
            monitor.boot_auto_open_checked = True
            monitor.taskmgr_was_running = True
            monitor._mock_taskmgr_pids = []  # Taskmgr exited

            now = time.monotonic()
            monitor.check_auto_open(now)

            self.assertFalse(monitor.taskmgr_was_running)
            self.assertEqual(monitor.auto_open_reason, 'user_closed')
            self.assertAlmostEqual(monitor.next_auto_open_monotonic - now, 600.0, delta=2)
            mock_launch.assert_not_called()

            # Status JSON shows not running message
            monitor.publish('waiting')
            status = json.loads((Path(d) / 'status.json').read_text(encoding='utf-8'))
            self.assertIn('任务管理器未运行', status['auto_open']['status_text_zh'])
            self.assertIn('Task Manager not running', status['auto_open']['status_text_en'])

            # If user reopens Taskmgr during cooldown, timer is cleared
            monitor._mock_taskmgr_pids = [8888]
            monitor.check_auto_open(now + 100)
            self.assertTrue(monitor.taskmgr_was_running)
            self.assertIsNone(monitor.next_auto_open_monotonic)
            self.assertIsNone(monitor.auto_open_reason)

    def test_existing_instance_in_waiting_navigates_after_2_minutes(self):
        """Any Taskmgr instance stuck in waiting > 2 minutes is navigated to CPU page."""
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_user_interacting_with_window', return_value=False), \
             patch.object(watch, 'is_taskmgr_process_alive', return_value=True), \
             patch.object(watch, 'restore_cpu_page', return_value={'status': 'restarted', 'new_pid': 51284}) as mock_nav:
            monitor = watch.Monitor()
            monitor._mock_taskmgr_pids = [51284]

            now = time.monotonic()
            monitor.accept({'state': 'waiting', 'pid': 51284})

            self.assertIsNotNone(monitor.blind_navigation_target)
            self.assertEqual(monitor.blind_navigation_target['target_pid'], 51284)
            self.assertAlmostEqual(monitor.next_navigation_retry_monotonic - now, 120.0, delta=2)

            # Before 2 minutes: does not navigate
            monitor.check_blind_navigation(now + 60.0)
            mock_nav.assert_not_called()

            # At 2 minutes: navigates
            monitor.check_blind_navigation(now + 121.0)
            if monitor.dump_thread:
                monitor.dump_thread.join(timeout=2)

            mock_nav.assert_called_once_with(51284)
            self.assertEqual(monitor.auto_navigation_count, 1)
            self.assertEqual(monitor.auto_navigation_last_result, 'restarted')
            self.assertIsNone(monitor.blind_navigation_target)

    def test_navigation_skips_when_user_interacting_with_window(self):
        """Existing instance navigation skips and defers 60s when user is interacting with Taskmgr."""
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_user_interacting_with_window', return_value=True), \
             patch.object(watch, 'is_taskmgr_process_alive', return_value=True), \
             patch.object(watch, 'restore_cpu_page') as mock_nav:
            monitor = watch.Monitor()
            now = time.monotonic()
            monitor.arm_blind_navigation(target_pid=51284, now=now)
            monitor.next_navigation_retry_monotonic = now - 1

            monitor.check_blind_navigation(now)
            mock_nav.assert_not_called()
            self.assertIsNone(monitor.dump_thread)
            self.assertAlmostEqual(monitor.next_navigation_retry_monotonic - now, 60.0, delta=2)

    def test_navigation_failure_backoff_and_hourly_limit(self):
        """Failed navigation attempts back off (120, 300, 600, 1200, 1800) and cap at 4/hour."""
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)), \
             patch.object(watch, 'is_user_interacting_with_window', return_value=False), \
             patch.object(watch, 'is_taskmgr_process_alive', return_value=True), \
             patch.object(watch, 'restore_cpu_page', return_value={'status': 'restarted_waiting_for_cpu_page'}):
            monitor = watch.Monitor()
            now = time.monotonic()
            monitor.arm_blind_navigation(target_pid=51284, now=now)

            # Fire first attempt
            t1 = now + 121
            monitor.check_blind_navigation(t1)
            if monitor.dump_thread:
                monitor.dump_thread.join(timeout=2)
            self.assertEqual(monitor.blind_retry_count, 1)
            self.assertAlmostEqual(monitor.next_navigation_retry_monotonic - t1, 300.0, delta=2)

            # Fire second attempt
            t2 = t1 + 301
            monitor.check_blind_navigation(t2)
            if monitor.dump_thread:
                monitor.dump_thread.join(timeout=2)
            self.assertEqual(monitor.blind_retry_count, 2)
            self.assertAlmostEqual(monitor.next_navigation_retry_monotonic - t2, 600.0, delta=2)

            # Hourly limit deferral: simulate 4 retries already executed
            t = 10000.0
            monitor.blind_retry_history = [t - 3000, t - 2000, t - 1000, t - 100]
            monitor.next_navigation_retry_monotonic = t
            monitor.check_blind_navigation(t)
            self.assertAlmostEqual(monitor.next_navigation_retry_monotonic, t + 600.0, delta=2)

    def test_status_json_contains_auto_open_and_auto_navigation(self):
        """Status JSON includes auto_open and auto_navigation blocks."""
        with tempfile.TemporaryDirectory() as d, patch.object(watch, 'OUTPUT', Path(d)):
            monitor = watch.Monitor()
            monitor.auto_open_count = 2
            monitor.auto_open_last_result = 'restarted'
            monitor.auto_navigation_count = 3
            monitor.auto_navigation_last_result = 'restarted'

            monitor.publish('normal')
            data = json.loads((Path(d) / 'status.json').read_text(encoding='utf-8'))
            self.assertIn('auto_open', data)
            self.assertEqual(data['auto_open']['count'], 2)
            self.assertEqual(data['auto_open']['last_result'], 'restarted')
            self.assertIn('auto_navigation', data)
            self.assertEqual(data['auto_navigation']['count'], 3)
            self.assertEqual(data['auto_navigation']['last_result'], 'restarted')


class TrayUiAutoOpenTests(unittest.TestCase):
    def test_flatline_watch_tray_shows_not_running_text(self):
        """Tray and widget status label display localized not running message."""
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
                # Set Chinese language
                w.language.setCurrentIndex(0)
                w.monitor.state = {
                    'state': 'waiting',
                    'seconds': 0,
                    'blind_seconds': 10,
                    'auto_open': {
                        'status_text_zh': '任务管理器未运行，将于 17:30:00 自动打开',
                        'status_text_en': 'Task Manager not running; will open automatically at 17:30:00',
                    }
                }
                w.update_status()
                self.assertIn('任务管理器未运行，将于 17:30:00 自动打开', w.status.text())
                self.assertIn('任务管理器未运行，将于 17:30:00 自动打开', w.tray.toolTip())

                # Switch to English
                w.language.setCurrentIndex(1)
                w.update_status()
                self.assertIn('Task Manager not running; will open automatically at 17:30:00', w.status.text())
                self.assertIn('Task Manager not running; will open automatically at 17:30:00', w.tray.toolTip())
            finally:
                w.timer.stop()
                w.monitor.stop()
                w.tray.hide()


if __name__ == '__main__':
    unittest.main()
