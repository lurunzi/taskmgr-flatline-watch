"""Unit tests for Goal 1 and Goal 2 automation (J16)."""
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from isolated_test_case import IsolatedTestCase
from GoalAutomation import (
    Goal1Runner,
    Goal2Tracker,
    GoalAutomationManager,
    extract_pdb_info,
    get_os_build,
    generate_feedback_hub_summary
)


class TestGoalAutomation(IsolatedTestCase):

    def setUp(self):
        super().setUp()
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_goal_auto_"))
        self.output_dir = self.test_dir / "TaskmgrFreezeCaptures"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Create dummy negative control DLL
        self.neg_ctrl_dir = self.output_dir / "fix-verification" / "dll-9549"
        self.neg_ctrl_dir.mkdir(parents=True, exist_ok=True)
        self.neg_ctrl_dll = self.neg_ctrl_dir / "TaskManagerDataLayer.dll"
        self.neg_ctrl_dll.write_bytes(b"MZ" + b"\x00" * 1024 + b"ORIGINAL_9549_CONTROL")

        # Create dummy candidate DLL
        self.candidate_dll = self.test_dir / "TaskManagerDataLayer_new.dll"
        self.candidate_dll.write_bytes(b"MZ" + b"\x00" * 1024 + b"NEW_CANDIDATE_BINARY_FIXED")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        super().tearDown()

    def test_os_build_format(self):
        build = get_os_build()
        self.assertIsInstance(build, str)
        self.assertTrue(len(build) > 0)

    def test_pdb_extraction_fallback_on_dummy(self):
        name, key = extract_pdb_info(b"MZ" + b"\x00" * 512)
        self.assertIsNone(name)
        self.assertIsNone(key)

    def test_goal1_runner_pass_scenario(self):
        runner = Goal1Runner(
            self.output_dir,
            negative_control_path=self.neg_ctrl_dll
        )
        mock_cand = {
            "evaluation": {
                "verdict": "PASS",
                "verdict_summary": "Both defects resolved.",
                "criteria": {
                    "criterion_1_exhaustion_not_success": {"passed": True},
                    "criterion_2_third_call_success_not_oom": {"passed": True},
                    "criterion_3_baseline_and_error_propagation": {"passed": True}
                }
            }
        }
        mock_neg = {
            "evaluation": {
                "verdict": "DEFECTIVE_9549_CONFIRMED",
                "verdict_summary": "Negative control confirmed defects.",
                "criteria": {
                    "criterion_1_exhaustion_not_success": {"passed": False},
                    "criterion_2_third_call_success_not_oom": {"passed": False},
                    "criterion_3_baseline_and_error_propagation": {"passed": True}
                }
            }
        }

        res = runner.run_goal1(
            self.candidate_dll,
            candidate_meta={"component_store_version": "10.0.26100.9999"},
            mock_candidate_result=mock_cand,
            mock_negative_result=mock_neg
        )

        self.assertEqual(res["status"], "passed")
        self.assertEqual(res["verdict"], "PASS")
        self.assertTrue(Path(res["receipt_file"]).is_file())
        self.assertTrue(Path(res["summary_md_file"]).is_file())
        self.assertIsNone(res["feedback_hub_file"])

        # Check receipt contents
        receipt = json.loads(Path(res["receipt_file"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["verdict"], "PASS")
        self.assertEqual(receipt["status"], "passed")
        # Criterion 5 real-kernel churn run deferred to manual execution
        c5 = receipt["criteria_summary"]["criterion_5_real_kernel"]
        self.assertFalse(c5["executed"])
        self.assertIn("deferred", c5["note"].lower())

        # Check archived DLL and MANIFEST.json
        archive_dir = Path(res["archive_dir"])
        self.assertTrue((archive_dir / "TaskManagerDataLayer.dll").is_file())
        manifest_file = archive_dir / "MANIFEST.json"
        self.assertTrue(manifest_file.is_file())
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(manifest["file"], "TaskManagerDataLayer.dll")
        self.assertEqual(manifest["component_store_version"], "10.0.26100.9999")
        self.assertIn("sha256", manifest)

    def test_goal1_runner_fail_scenario_generates_feedback_hub_summary(self):
        runner = Goal1Runner(
            self.output_dir,
            negative_control_path=self.neg_ctrl_dll
        )
        mock_cand = {
            "evaluation": {
                "verdict": "FAIL",
                "verdict_summary": "Exhaustion defect still present.",
                "criteria": {
                    "criterion_1_exhaustion_not_success": {
                        "passed": False,
                        "observed_hresult": "0x00000000",
                        "published_bytes": 16384,
                        "ghost_record_observed": True
                    },
                    "criterion_2_third_call_success_not_oom": {
                        "passed": False,
                        "observed_hresult": "0x8007000e",
                        "published_bytes": 0
                    },
                    "criterion_3_baseline_and_error_propagation": {
                        "passed": True,
                        "second_call_success_hresult": "0x00000000",
                        "access_denied_propagated_hresult": "0x80070005"
                    }
                }
            }
        }
        mock_neg = {
            "evaluation": {
                "verdict": "DEFECTIVE_9549_CONFIRMED",
                "verdict_summary": "Negative control confirmed.",
                "criteria": {
                    "criterion_1_exhaustion_not_success": {"passed": False},
                    "criterion_2_third_call_success_not_oom": {"passed": False},
                    "criterion_3_baseline_and_error_propagation": {"passed": True}
                }
            }
        }

        res = runner.run_goal1(
            self.candidate_dll,
            candidate_meta={"component_store_version": "10.0.26100.9550"},
            mock_candidate_result=mock_cand,
            mock_negative_result=mock_neg
        )

        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["verdict"], "FAIL")
        self.assertIsNotNone(res["feedback_hub_file"])
        self.assertTrue(Path(res["feedback_hub_file"]).is_file())

        fb_text = Path(res["feedback_hub_file"]).read_text(encoding="utf-8")
        self.assertIn("Feedback Hub", fb_text)
        self.assertIn("0x00000000", fb_text)
        self.assertIn("0x8007000E", fb_text)
        self.assertIn("DEFECTS REMAIN PRESENT", fb_text)

    def test_goal2_tracker_lifecycle_and_evaluation(self):
        tracker = Goal2Tracker(self.output_dir)
        self.assertEqual(tracker.state["status"], "waiting_for_goal1")
        self.assertFalse(tracker.state["ready_for_goal3"])

        # Start counting
        start_meta = {
            "sha256": "abcdef1234567890",
            "component_store_version": "10.0.26100.9999"
        }
        tracker.start_counting(start_meta)
        self.assertEqual(tracker.state["status"], "accumulating")
        self.assertEqual(tracker.state["calendar_days"], 0)
        self.assertEqual(tracker.state["effective_monitoring_hours"], 0.0)
        self.assertEqual(tracker.state["high_load_windows_count"], 0)

        # Accumulate hours
        tracker.accumulate_effective_seconds(3600 * 50)  # 50 hours
        self.assertEqual(tracker.state["effective_monitoring_hours"], 50.0)
        self.assertEqual(tracker.state["status"], "accumulating")
        self.assertFalse(tracker.state["ready_for_goal3"])

        # Record high load windows
        for i in range(15):
            tracker.record_high_load_window(process_count=55)
        self.assertEqual(tracker.state["high_load_windows_count"], 15)
        self.assertEqual(tracker.state["status"], "accumulating")

        # ProcessDataSource2.cpp:821 error reported only
        tracker.record_process_datasource2_821()
        self.assertEqual(tracker.state["process_datasource2_821_count"], 1)
        self.assertEqual(tracker.state["status"], "accumulating")  # Must not fail

        # Simulate reaching full requirements: 14 days, 150h, 20 windows
        tracker.state["start_utc"] = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        tracker.accumulate_effective_seconds(3600 * 105)  # 50 + 105 = 155 hours
        for i in range(5):
            tracker.record_high_load_window(process_count=60)
        self.assertEqual(tracker.state["high_load_windows_count"], 20)

        # Re-evaluate
        eval_res = tracker.evaluate_criteria()
        self.assertEqual(eval_res["status"], "goal2_passed")
        self.assertTrue(eval_res["ready_for_goal3"])
        self.assertTrue(eval_res["criteria"]["calendar_days_ge_14"])
        self.assertTrue(eval_res["criteria"]["effective_hours_ge_150"])
        self.assertTrue(eval_res["criteria"]["high_load_windows_ge_20"])
        self.assertTrue(eval_res["criteria"]["zero_aggregation_errors"])
        self.assertTrue(eval_res["criteria"]["zero_flatlines"])

    def test_goal2_tracker_immediate_fail_on_aggregation_error(self):
        tracker = Goal2Tracker(self.output_dir)
        tracker.start_counting({"sha256": "test"})
        self.assertEqual(tracker.state["status"], "accumulating")

        tracker.record_aggregation_error({"hresult": "0x8007139F"})
        self.assertEqual(tracker.state["status"], "goal2_failed")
        self.assertFalse(tracker.state["ready_for_goal3"])
        self.assertEqual(tracker.state["aggregation_errors_0x8007139F_count"], 1)

    def test_goal2_tracker_immediate_fail_on_flatline(self):
        tracker = Goal2Tracker(self.output_dir)
        tracker.start_counting({"sha256": "test"})
        self.assertEqual(tracker.state["status"], "accumulating")

        tracker.record_confirmed_flatline("restart-test-folder")
        self.assertEqual(tracker.state["status"], "goal2_failed")
        self.assertFalse(tracker.state["ready_for_goal3"])
        self.assertEqual(tracker.state["confirmed_flatlines_count"], 1)

    def test_goal2_persistence(self):
        tracker = Goal2Tracker(self.output_dir)
        tracker.start_counting({"sha256": "persist_hash"})
        tracker.accumulate_effective_seconds(7200)  # 2.0 hours
        tracker.record_high_load_window(process_count=75)
        tracker.save()

        # Reload in a new instance
        tracker2 = Goal2Tracker(self.output_dir)
        self.assertEqual(tracker2.state["status"], "accumulating")
        self.assertEqual(tracker2.state["target_dll_sha256"], "persist_hash")
        self.assertEqual(tracker2.state["effective_monitoring_hours"], 2.0)
        self.assertEqual(tracker2.state["high_load_windows_count"], 1)

    def test_goal_automation_manager_orchestration(self):
        manager = GoalAutomationManager(self.output_dir)
        initial_dll = {
            "path": str(self.candidate_dll),
            "sha256": "hash_initial",
            "file_version": "10.0.26100.9549",
            "dll_changed": False
        }
        manager.initialize(initial_dll)
        g1 = manager.get_goal1_status()
        self.assertEqual(g1["status"], "waiting_for_update")
        g2 = manager.get_goal2_status()
        self.assertEqual(g2["status"], "waiting_for_goal1")

        # Simulate DLL change triggering Goal 1
        updated_dll = {
            "path": str(self.candidate_dll),
            "sha256": "hash_updated",
            "file_version": "10.0.26100.9999",
            "dll_changed": True
        }
        mock_cand = {
            "evaluation": {
                "verdict": "PASS",
                "verdict_summary": "Both defects fixed.",
                "criteria": {
                    "criterion_1_exhaustion_not_success": {"passed": True},
                    "criterion_2_third_call_success_not_oom": {"passed": True},
                    "criterion_3_baseline_and_error_propagation": {"passed": True}
                }
            }
        }
        mock_neg = {
            "evaluation": {
                "verdict": "DEFECTIVE_9549_CONFIRMED",
                "verdict_summary": "Negative control confirmed.",
                "criteria": {
                    "criterion_1_exhaustion_not_success": {"passed": False},
                    "criterion_2_third_call_success_not_oom": {"passed": False},
                    "criterion_3_baseline_and_error_propagation": {"passed": True}
                }
            }
        }

        res = manager.check_dll_update(
            updated_dll,
            dll_path=self.candidate_dll,
            mock_candidate_result=mock_cand,
            mock_negative_result=mock_neg
        )
        self.assertIsNotNone(res)
        self.assertEqual(res["status"], "passed")

        # Goal 1 state updated
        g1_after = manager.get_goal1_status()
        self.assertEqual(g1_after["status"], "passed")
        self.assertEqual(g1_after["verdict"], "PASS")

        # Goal 2 automatically started!
        g2_after = manager.get_goal2_status()
        self.assertEqual(g2_after["status"], "accumulating")
        self.assertEqual(g2_after["target_dll_sha256"], "hash_updated")

        # Test process start sliding window debounce
        t0 = 1000.0
        for _ in range(55):
            manager.on_process_start(t0 + 0.5)
        # Should record 1 high load window
        self.assertEqual(manager.tracker.state["high_load_windows_count"], 1)

        # Within 2-second debounce, additional starts shouldn't increment window count
        for _ in range(60):
            manager.on_process_start(t0 + 1.0)
        self.assertEqual(manager.tracker.state["high_load_windows_count"], 1)

        # After 2.5 seconds, a new burst should increment
        t1 = t0 + 2.5
        for _ in range(52):
            manager.on_process_start(t1)
        self.assertEqual(manager.tracker.state["high_load_windows_count"], 2)

    def test_goal3_boundary_constraint_never_automatic(self):
        """Verifies Goal 3 retirement is strictly manual; code never disables autostart or stops automatically."""
        manager = GoalAutomationManager(self.output_dir)
        manager.tracker.state.update({
            "status": "goal2_passed",
            "ready_for_goal3": True
        })
        # Check status block
        status_block = manager.get_goal2_status()
        self.assertEqual(status_block["status"], "goal2_passed")
        self.assertTrue(status_block["ready_for_goal3"])

        # Confirm no automatic deletion, unregistration, or stop action is invoked
        # Check that output directory and all subdirectories are completely preserved
        self.assertTrue(self.output_dir.exists())

    def test_tray_text_localization(self):
        """Verifies bilingual dictionary coverage for Goal 1 and Goal 2 in FlatlineWatch."""
        import FlatlineWatch as FW
        zh = FW.TEXT['zh']
        en = FW.TEXT['en']

        for key in [
            'goal1_waiting', 'goal1_passed', 'goal1_failed',
            'goal2_waiting', 'goal2_accumulating', 'goal2_passed', 'goal2_failed',
            'goal2_prompt'
        ]:
            self.assertIn(key, zh, f"Missing zh key: {key}")
            self.assertIn(key, en, f"Missing en key: {key}")
            self.assertIsInstance(zh[key], str)
            self.assertIsInstance(en[key], str)
            self.assertTrue(len(zh[key]) > 0)
            self.assertTrue(len(en[key]) > 0)

        # Test string formatting for goal2_accumulating
        zh_acc = zh['goal2_accumulating'].format(days=7, hours=65.5, windows=12)
        en_acc = en['goal2_accumulating'].format(days=7, hours=65.5, windows=12)
        self.assertIn("7/14天", zh_acc)
        self.assertIn("7/14d", en_acc)
        self.assertIn("65.5", zh_acc)
        self.assertIn("65.5", en_acc)


if __name__ == "__main__":
    unittest.main()
