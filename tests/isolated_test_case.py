"""Base test case providing safe default mock isolation for all unit tests."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import AutomaticWatch
import TaskmgrRecovery
import NativeCapture

class IsolatedTestCase(unittest.TestCase):
    """Test case with default mock isolation for system probes and actions."""

    def setUp(self):
        super().setUp()
        os.environ['TASKMGR_WATCH_TEST'] = '1'
        AutomaticWatch.Monitor._mock_desktop_ready = True
        AutomaticWatch.Monitor._mock_user_interacting_system = False
        AutomaticWatch.Monitor._mock_user_interacting_window = False
        AutomaticWatch.Monitor._mock_taskmgr_alive = True

    def tearDown(self):
        AutomaticWatch.Monitor._mock_desktop_ready = True
        AutomaticWatch.Monitor._mock_user_interacting_system = False
        AutomaticWatch.Monitor._mock_user_interacting_window = False
        AutomaticWatch.Monitor._mock_taskmgr_alive = True
        super().tearDown()
