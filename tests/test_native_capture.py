import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import NativeCapture as capture


class LocateTests(unittest.TestCase):
    def locate_grid(self, widths, cpu_count=32, iconic=False):
        # The incident's 8-column layout has one extra pixel in two columns.
        window, total, memory = 1, 2, 3
        cores = list(range(10, 10+len(widths)))
        rectangles = {total: (3460, 341, 75, 50), memory: (3460, 416, 75, 50)}
        rectangles.update({handle: (3791+i%8*108, 407+i//8*138, width, 132)
                           for i, (handle, width) in enumerate(zip(cores, widths))})

        def enumerate_windows(parent=None):
            return [window] if parent is None else [memory, *reversed(cores), total]

        def set_pid(hwnd, result):
            result._obj.value = 14232

        with patch.object(capture, 'enumerate_windows', side_effect=enumerate_windows), \
             patch.object(capture, 'class_name', side_effect=lambda h:
                          'TaskManagerWindow' if h == window else 'CvChartWindow'), \
             patch.object(capture, 'rectangle', side_effect=rectangles.__getitem__), \
             patch.object(capture.u, 'IsIconic', return_value=iconic), \
             patch.object(capture.u, 'IsWindowVisible', return_value=True), \
             patch.object(capture.u, 'GetWindowThreadProcessId', side_effect=set_pid), \
             patch.object(capture.os, 'cpu_count', return_value=cpu_count):
            return capture.locate()

    def test_live_101_102_pixel_grid_is_recognised_and_sorted(self):
        found = self.locate_grid([101, 101, 101, 102, 101, 101, 101, 102]*4)
        self.assertIsNotNone(found)
        self.assertEqual(found[:2], (1, 14232))
        self.assertEqual(found[2][0], 2)
        self.assertEqual([h for h, _ in found[3]], list(range(10, 42)))

    def test_adjacent_widths_do_not_depend_on_rounding_bucket(self):
        for width in range(25, 145):
            with self.subTest(width=width):
                self.assertIsNotNone(self.locate_grid([width, width+1]*16))

    def test_uniform_grid_and_eighty_percent_agreement_are_retained(self):
        self.assertIsNotNone(self.locate_grid([101]*32))
        self.assertIsNotNone(self.locate_grid([101]*4+[140], cpu_count=5))
        self.assertIsNone(self.locate_grid([101]*3+[140]*2, cpu_count=5))

    def test_disjoint_widths_are_rejected(self):
        for widths in ([101]*24+[103]*8, [101]*16+[160]*16):
            with self.subTest(widths=widths):
                self.assertIsNone(self.locate_grid(widths))

    def test_incomplete_extra_single_and_minimised_grids_are_rejected(self):
        for count in (0, 1, 31, 33):
            with self.subTest(count=count):
                self.assertIsNone(self.locate_grid([101]*count))
        self.assertIsNone(self.locate_grid([101], cpu_count=1))
        self.assertIsNone(self.locate_grid([101]*32, iconic=True))


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
