"""Tests proving that test isolation guards prevent unit tests from touching the real Task Manager."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import AutomaticWatch as watch
import TaskmgrRecovery as recovery
import NativeCapture as capture
from isolated_test_case import IsolatedTestCase

class TestIsolationGuards(IsolatedTestCase):
    def test_launch_taskmgr_raises_isolation_violation(self):
        with self.assertRaises(RuntimeError) as ctx:
            recovery.launch_taskmgr()
        self.assertIn("TEST ISOLATION VIOLATION", str(ctx.exception))
        self.assertIn("launch real Taskmgr", str(ctx.exception))

    def test_restore_cpu_page_raises_isolation_violation(self):
        with self.assertRaises(RuntimeError) as ctx:
            recovery.restore_cpu_page(12345)
        self.assertIn("TEST ISOLATION VIOLATION", str(ctx.exception))
        self.assertIn("navigate real Taskmgr", str(ctx.exception))

    def test_restart_taskmgr_raises_isolation_violation(self):
        with self.assertRaises(RuntimeError) as ctx:
            recovery.restart_taskmgr(12345, 67890)
        self.assertIn("TEST ISOLATION VIOLATION", str(ctx.exception))
        self.assertIn("restart real Taskmgr", str(ctx.exception))

    def test_main_window_returns_none_without_touching_host_windows(self):
        self.assertIsNone(recovery.main_window(12345))

    def test_native_capture_raises_isolation_violation(self):
        with self.assertRaises(RuntimeError) as ctx:
            capture.capture(12345)
        self.assertIn("TEST ISOLATION VIOLATION", str(ctx.exception))
        self.assertIn("capture real window", str(ctx.exception))

    def test_native_enumerate_windows_and_taskmgr_pids_return_empty(self):
        self.assertEqual(capture.enumerate_windows(), [])
        self.assertEqual(capture.taskmgr_pids(), set())
        self.assertEqual(watch.taskmgr_pids(), set())

    def test_monitor_get_taskmgr_pids_uses_mock_or_empty(self):
        monitor = watch.Monitor()
        self.assertEqual(monitor.get_taskmgr_pids(), set())
        monitor._mock_taskmgr_pids = {99999}
        self.assertEqual(monitor.get_taskmgr_pids(), {99999})

    def test_system_probes_use_safe_mocks_without_touching_win32(self):
        self.assertTrue(watch.is_desktop_ready())
        watch.Monitor._mock_desktop_ready = False
        self.assertFalse(watch.is_desktop_ready())

        self.assertFalse(watch.is_user_interacting_system())
        watch.Monitor._mock_user_interacting_system = True
        self.assertTrue(watch.is_user_interacting_system())

        self.assertFalse(watch.is_user_interacting_with_window(12345))
        watch.Monitor._mock_user_interacting_window = True
        self.assertTrue(watch.is_user_interacting_with_window(12345))

        self.assertTrue(watch.is_taskmgr_process_alive(12345))
        watch.Monitor._mock_taskmgr_alive = False
        self.assertFalse(watch.is_taskmgr_process_alive(12345))

    def test_require_taskmgr_rejects_non_taskmgr(self):
        with self.assertRaises(RuntimeError) as ctx:
            watch.require_taskmgr(os.getpid())
        self.assertIn('Target is not Windows Task Manager', str(ctx.exception))

if __name__ == '__main__':
    unittest.main()
