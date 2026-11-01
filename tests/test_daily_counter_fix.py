"""Unit tests covering the daily counters fix (J20):
1. Confirmed flatlines recorded in daily monitoring
2. Native 0x8007139F deduplication (1s merge per PID) and event counts
3. Goal 2 tracker 1s merge and event counts
4. Effective monitoring definition consistency (minimized is not effective)
5. Multi-threaded concurrency safety
6. Idempotent backfill functionality
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

test_dir = Path(__file__).resolve().parent
repo_root = test_dir.parent

sys.path.insert(0, str(repo_root / "src"))
sys.path.insert(0, str(test_dir))

from isolated_test_case import IsolatedTestCase
from AutomaticWatch import Monitor
from GoalAutomation import GoalAutomationManager, Goal2Tracker, is_monitor_effective

# Import backfill tool dynamically
backfill_path = repo_root / "tools" / "backfill-daily-counters.py"
spec = importlib.util.spec_from_file_location("backfill_daily_counters", backfill_path)
backfill_daily_counters = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill_daily_counters)


class DailyCounterFixTests(IsolatedTestCase):

    def test_confirmed_flatline_increments_daily_history(self):
        """Verifies trigger() and restart_now() increment today's flatlines_confirmed."""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            with patch("AutomaticWatch.OUTPUT", out):
                monitor = Monitor(light_etw=False, output=out)
                monitor.daily_monitoring_file = out / "daily-monitoring.json"
                monitor.daily_monitoring_history = []

                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

                # Initially no entries
                self.assertEqual(len(monitor.daily_monitoring_history), 0)

                # Record confirmed flatline via helper
                monitor._record_confirmed_flatline_daily()
                self.assertEqual(len(monitor.daily_monitoring_history), 1)
                self.assertEqual(monitor.daily_monitoring_history[0]["flatlines_confirmed"], 1)

                # Second flatline on same day
                monitor._record_confirmed_flatline_daily()
                self.assertEqual(len(monitor.daily_monitoring_history), 1)
                self.assertEqual(monitor.daily_monitoring_history[0]["flatlines_confirmed"], 2)

    def test_cross_day_first_entry_creation(self):
        """Verifies creating today's entry on first flatline or native error preserves initial fields."""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            with patch("AutomaticWatch.OUTPUT", out):
                monitor = Monitor(light_etw=False, output=out)
                monitor.daily_monitoring_file = out / "daily-monitoring.json"
                monitor.daily_monitoring_history = []

                # Native error creates entry
                monitor._record_native_error_daily({
                    "taskmgr_pid": 1234,
                    "timestamp_monotonic": 100.0,
                    "timestamp_utc": "2026-11-01T20:00:00Z"
                })

                self.assertEqual(len(monitor.daily_monitoring_history), 1)
                entry = monitor.daily_monitoring_history[0]
                self.assertEqual(entry["native_0x8007139f_count"], 1)
                self.assertEqual(entry["native_0x8007139f_events"], 1)
                self.assertEqual(entry["flatlines_confirmed"], 0)
                self.assertEqual(entry["wall_seconds"], 0.0)
                self.assertEqual(entry["effective"], 0.0)
                self.assertEqual(entry["blind"], 0.0)

    def test_native_error_1s_merging_automatic_watch(self):
        """Verifies multiple 0x8007139F events within 1s for same PID merge into 1 count but accumulate events."""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            with patch("AutomaticWatch.OUTPUT", out):
                monitor = Monitor(light_etw=False, output=out)
                monitor.daily_monitoring_file = out / "daily-monitoring.json"
                monitor.daily_monitoring_history = []

                # 5 burst events within 10ms for PID 1000
                t0 = 500.0
                for i in range(5):
                    monitor._record_native_error_daily({
                        "taskmgr_pid": 1000,
                        "timestamp_monotonic": t0 + (i * 0.002),
                        "timestamp_utc": "2026-11-01T20:00:00Z"
                    })

                entry = monitor.daily_monitoring_history[0]
                self.assertEqual(entry["native_0x8007139f_events"], 5)
                self.assertEqual(entry["native_0x8007139f_count"], 1)

                # Event for a DIFFERENT PID (PID 2000) within the same second: counts as separate incident
                monitor._record_native_error_daily({
                    "taskmgr_pid": 2000,
                    "timestamp_monotonic": t0 + 0.1,
                    "timestamp_utc": "2026-11-01T20:00:00Z"
                })
                self.assertEqual(entry["native_0x8007139f_events"], 6)
                self.assertEqual(entry["native_0x8007139f_count"], 2)

                # Event for PID 1000 after 1.5s: counts as new incident
                monitor._record_native_error_daily({
                    "taskmgr_pid": 1000,
                    "timestamp_monotonic": t0 + 1.5,
                    "timestamp_utc": "2026-11-01T20:00:01Z"
                })
                self.assertEqual(entry["native_0x8007139f_events"], 7)
                self.assertEqual(entry["native_0x8007139f_count"], 3)

    def test_goal2_tracker_1s_merging(self):
        """Verifies Goal2Tracker merges events within 1s into single count and records raw events."""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            tracker = Goal2Tracker(out)
            tracker.start_counting({"sha256": "target_hash", "file_version": "1.0"})

            self.assertEqual(tracker.state["status"], "accumulating")
            self.assertEqual(tracker.state["aggregation_errors_0x8007139F_count"], 0)
            self.assertEqual(tracker.state["aggregation_errors_0x8007139F_events"], 0)

            # 5 burst events within 10ms for PID 1000
            t0 = 1000.0
            for i in range(5):
                tracker.record_aggregation_error({
                    "taskmgr_pid": 1000,
                    "timestamp_monotonic": t0 + (i * 0.002),
                    "hresult": "0x8007139F"
                })

            self.assertEqual(tracker.state["status"], "goal2_failed")
            self.assertEqual(tracker.state["aggregation_errors_0x8007139F_events"], 5)
            self.assertEqual(tracker.state["aggregation_errors_0x8007139F_count"], 1)

            # Event after 2.0s
            tracker.record_aggregation_error({
                "taskmgr_pid": 1000,
                "timestamp_monotonic": t0 + 2.0,
                "hresult": "0x8007139F"
            })
            self.assertEqual(tracker.state["aggregation_errors_0x8007139F_events"], 6)
            self.assertEqual(tracker.state["aggregation_errors_0x8007139F_count"], 2)

    def test_two_threads_concurrency_no_data_loss(self):
        """Verifies concurrent ETW error recordings and main thread updates do not corrupt or lose counts."""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            with patch("AutomaticWatch.OUTPUT", out):
                monitor = Monitor(light_etw=False, output=out)
                monitor.daily_monitoring_file = out / "daily-monitoring.json"
                monitor.daily_monitoring_history = []
                monitor.last_daily_update_mono = 100.0

                errors_to_fire = 60
                flatlines_to_fire = 20

                def etw_thread_worker():
                    for i in range(errors_to_fire):
                        # Use a timestamp that advances by 1.1s each time so all 60 are separate incidents
                        monitor._record_native_error_daily({
                            "taskmgr_pid": 9999,
                            "timestamp_monotonic": 100.0 + (i * 1.5),
                            "timestamp_utc": "2026-11-01T20:00:00Z"
                        })
                        time.sleep(0.001)

                def main_thread_worker():
                    for i in range(flatlines_to_fire):
                        monitor._record_confirmed_flatline_daily()
                        monitor.update_daily_monitoring(now=200.0 + i)
                        monitor.publish("normal")
                        time.sleep(0.001)

                t1 = threading.Thread(target=etw_thread_worker)
                t2 = threading.Thread(target=main_thread_worker)
                t1.start()
                t2.start()
                t1.join()
                t2.join()

                self.assertEqual(len(monitor.daily_monitoring_history), 1)
                entry = monitor.daily_monitoring_history[0]
                self.assertEqual(entry["native_0x8007139f_events"], errors_to_fire)
                self.assertEqual(entry["native_0x8007139f_count"], errors_to_fire)
                self.assertEqual(entry["flatlines_confirmed"], flatlines_to_fire)

    def test_minimized_not_effective_consistent(self):
        """Verifies minimized state is treated as non-effective in both is_monitor_effective and update_daily_monitoring."""
        # 1. Test standalone is_monitor_effective helper
        mock_monitor = MagicMock()
        mock_monitor.state = {"state": "minimized", "taskmgr_pid": 1234}
        mock_monitor.current_provider_gap = None
        # remove is_effective attribute to test state-based evaluation
        del mock_monitor.is_effective

        self.assertFalse(is_monitor_effective(mock_monitor))

        # Normal state is effective
        mock_monitor.state = {"state": "normal", "taskmgr_pid": 1234}
        self.assertTrue(is_monitor_effective(mock_monitor))

        # 2. Test GoalAutomationManager on_process_start when minimized
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            manager = GoalAutomationManager(out, monitor=mock_monitor)
            manager.tracker.start_counting({"sha256": "h"})

            # When minimized, process starts burst must NOT increment high_load_windows
            mock_monitor.state = {"state": "minimized", "taskmgr_pid": 1234}
            t0 = 100.0
            for _ in range(60):
                manager.on_process_start(t0 + 0.1)

            self.assertEqual(manager.tracker.state["high_load_windows_count"], 0)

            # When normal, process starts burst DOES increment high_load_windows
            mock_monitor.state = {"state": "normal", "taskmgr_pid": 1234}
            t1 = t0 + 10.0
            for _ in range(60):
                manager.on_process_start(t1)

            self.assertEqual(manager.tracker.state["high_load_windows_count"], 1)

        # 3. Test update_daily_monitoring accumulates to blind, not effective, when minimized
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            with patch("AutomaticWatch.OUTPUT", out):
                mon = Monitor(light_etw=False, output=out)
                mon.daily_monitoring_file = out / "daily-monitoring.json"
                mon.daily_monitoring_history = []
                mon.last_daily_update_mono = 100.0

                mon.state = {"state": "minimized", "taskmgr_pid": 1234}
                mon.current_provider_gap = None
                mon.update_daily_monitoring(now=103.0)  # dt = 3.0s

                entry = mon.daily_monitoring_history[0]
                self.assertEqual(entry["effective"], 0.0)
                self.assertEqual(entry["blind"], 3.0)

    def test_backfill_daily_counters_idempotent(self):
        """Verifies tools/backfill-daily-counters.py calculates accurate counts and is idempotent."""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)

            # Create existing daily-monitoring.json
            daily_data = [
                {
                    "date": "2026-10-30",
                    "effective_monitoring_hours": 10.0,
                    "flatlines_confirmed": 0,
                    "native_0x8007139f_count": 0,
                    "note": "统计口径有误，数值偏低"
                },
                {
                    "date": "2026-10-31",
                    "effective_monitoring_hours": 15.0,
                    "flatlines_confirmed": 0,
                    "native_0x8007139f_count": 0,
                    "note": "统计口径有误，数值偏低；心跳污染"
                }
            ]
            daily_file = out / "daily-monitoring.json"
            daily_file.write_text(json.dumps(daily_data, indent=2), encoding="utf-8")

            # Create restart incident folders
            # 1 on 10-30 with native_event_time
            r1 = out / "restart-20261030-140608-487205"
            r1.mkdir()
            (r1 / "event.json").write_text(json.dumps({
                "timestamp": "2026-10-30T14:06:08.500000+00:00",
                "native_event_time": "2026-10-30T14:05:43.000000+00:00"
            }), encoding="utf-8")

            # 2 on 10-31 (one with native_event_time, one with incident-analysis.json native_signature)
            r2 = out / "restart-20261031-193909-000213"
            r2.mkdir()
            (r2 / "event.json").write_text(json.dumps({
                "timestamp": "2026-10-31T19:39:09.000000+00:00",
                "native_event_time": "2026-10-31T19:38:44.000000+00:00"
            }), encoding="utf-8")

            r3 = out / "restart-20261031-224928-818179"
            r3.mkdir()
            (r3 / "event.json").write_text(json.dumps({
                "timestamp": "2026-10-31T22:49:28.000000+00:00"
            }), encoding="utf-8")
            (r3 / "incident-analysis.json").write_text(json.dumps({
                "native_signature": {"hresult": "0x8007139F"}
            }), encoding="utf-8")

            # A reanalysis folder that should be excluded
            r3_re = out / "restart-20261031-224928-818179-reanalysis"
            r3_re.mkdir()
            (r3_re / "event.json").write_text(json.dumps({
                "timestamp": "2026-10-31T22:49:28.000000+00:00"
            }), encoding="utf-8")

            # Run backfill first time
            res1 = backfill_daily_counters.backfill_daily_monitoring(out, backfill_time="2026-11-01T22:30:00Z")
            self.assertEqual(res1[0]["flatlines_confirmed"], 1)
            self.assertEqual(res1[0]["native_0x8007139f_count"], 1)
            self.assertIsNone(res1[0]["native_0x8007139f_events"])
            self.assertEqual(res1[0]["note"], "冻结与原生错误计数已回填")
            self.assertEqual(res1[0]["backfilled"], "2026-11-01T22:30:00Z counter fix")

            self.assertEqual(res1[1]["flatlines_confirmed"], 2)
            self.assertEqual(res1[1]["native_0x8007139f_count"], 2)
            self.assertIsNone(res1[1]["native_0x8007139f_events"])
            self.assertIn("冻结与原生错误计数已回填", res1[1]["note"])

            # Run backfill second time (idempotent)
            res2 = backfill_daily_counters.backfill_daily_monitoring(out, backfill_time="2026-11-01T22:30:00Z")
            self.assertEqual(res1, res2)


if __name__ == "__main__":
    unittest.main()
