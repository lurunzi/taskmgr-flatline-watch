import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import NativeCapture as capture


class SystemCpuTests(unittest.TestCase):
    def sequence(self, times, counters):
        sampler=capture.SystemCpuSampler()
        with patch.object(capture.time,'monotonic',side_effect=times), \
             patch.object(capture,'system_times',side_effect=counters):
            return [sampler.sample() for _ in times]

    def test_kernel_includes_idle_and_first_read_is_only_a_baseline(self):
        self.assertEqual(self.sequence([0,2],[(100,200,50),(180,280,70)]),[None,20.0])

    def test_failed_read_and_long_gap_cannot_reuse_old_activity(self):
        self.assertEqual(self.sequence([0,2,4,6,14,16],
            [(0,0,0),None,(100,200,50),(180,280,70),(200,300,70),(200,310,70)]),
            [None,None,None,20.0,None,100.0])

    def test_invalid_or_nonadvancing_counters_are_unavailable(self):
        for final in ((100,200,50),(99,210,60),(300,210,60),(101,199,70)):
            self.assertEqual(self.sequence([0,2],[(100,200,50),final]),[None,None])

    def test_more_than_one_processor_group_is_not_presented_as_system_total(self):
        with patch.object(capture.os,'cpu_count',return_value=128), \
             patch.object(capture.k,'GetSystemTimes') as query:
            self.assertIsNone(capture.system_times())
            query.assert_not_called()


if __name__=='__main__': unittest.main()
