"""Unit tests for kernel buffer churn delta attribution."""
import unittest
import struct
from ChurnAttribution import (
    parse_process_snapshot,
    diff_snapshots,
    query_process_snapshot,
    THREAD_INFO_SIZE
)


class ChurnAttributionTests(unittest.TestCase):
    def test_live_query_process_snapshot_succeeds(self):
        status, snap, retlen = query_process_snapshot(buffer_size=1024*1024*4)
        self.assertEqual(status, 0)
        self.assertGreater(retlen, 100000)
        self.assertGreater(len(snap), 10)

    def test_parse_process_snapshot_with_synthetic_bytes(self):
        # Create a synthetic buffer with 2 processes
        # Process 1: PID 100, 2 threads, step 0x100 + 2*80 = 416 (0x1A0)
        # Process 2: PID 200, 4 threads, step 0 (terminator)
        step1 = 0x100 + 2 * THREAD_INFO_SIZE
        buf = bytearray(step1 + 0x100 + 4 * THREAD_INFO_SIZE)

        # Proc 1
        struct.pack_into('<II', buf, 0, step1, 2)
        struct.pack_into('<Q', buf, 0x20, 1000) # create_time
        struct.pack_into('<Q', buf, 0x50, 100)  # PID

        # Proc 2
        off2 = step1
        struct.pack_into('<II', buf, off2, 0, 4) # step 0
        struct.pack_into('<Q', buf, off2 + 0x20, 2000)
        struct.pack_into('<Q', buf, off2 + 0x50, 200)

        snap = parse_process_snapshot(bytes(buf))
        self.assertEqual(len(snap), 2)
        self.assertIn(100, snap)
        self.assertIn(200, snap)
        self.assertEqual(snap[100]['threads'], 2)
        self.assertEqual(snap[200]['threads'], 4)

    def test_diff_snapshots_single_dominant_source(self):
        snap1 = {
            100: {'pid': 100, 'name': 'target.exe', 'threads': 2, 'step': 416},
            200: {'pid': 200, 'name': 'other.exe', 'threads': 4, 'step': 576},
        }
        # snap2: PID 100 grew from 2 to 22 threads (+20 threads = 1600B)
        # PID 200 unchanged
        snap2 = {
            100: {'pid': 100, 'name': 'target.exe', 'threads': 22, 'step': 416 + 20*80},
            200: {'pid': 200, 'name': 'other.exe', 'threads': 4, 'step': 576},
        }
        res = diff_snapshots(snap1, snap2, 1000, 2600)
        self.assertEqual(res['positive_expansion_bytes'], 1600)
        self.assertEqual(res['pattern'], 'single_dominant_source')
        self.assertEqual(res['cr1'], 1.0)
        self.assertEqual(len(res['top_contributors']), 1)
        self.assertEqual(res['top_contributors'][0]['pid'], 100)
        self.assertEqual(res['top_contributors'][0]['thread_delta'], 20)

    def test_diff_snapshots_diffused_system_entropy(self):
        # 5 processes each grow by 1 thread (80B each = 400B total)
        snap1 = {i: {'pid': i, 'name': f'p{i}.exe', 'threads': 2, 'step': 416} for i in range(1, 6)}
        snap2 = {i: {'pid': i, 'name': f'p{i}.exe', 'threads': 3, 'step': 496} for i in range(1, 6)}

        res = diff_snapshots(snap1, snap2, 2000, 2400)
        self.assertEqual(res['positive_expansion_bytes'], 400)
        self.assertEqual(res['pattern'], 'diffused_system_entropy')
        self.assertLess(res['cr1'], 0.70)
        self.assertLess(res['cr3'], 0.70)
        self.assertEqual(len(res['top_contributors']), 5)

    def test_diff_snapshots_new_and_exited_process(self):
        snap1 = {
            100: {'pid': 100, 'name': 'exiting.exe', 'threads': 2, 'step': 416},
        }
        snap2 = {
            200: {'pid': 200, 'name': 'starting.exe', 'threads': 3, 'step': 496},
        }
        res = diff_snapshots(snap1, snap2, 1000, 1080)
        self.assertEqual(res['positive_expansion_bytes'], 496)
        self.assertEqual(res['top_contributors'][0]['pid'], 200)
        self.assertEqual(res['top_contributors'][0]['type'], 'new_process')


if __name__ == '__main__':
    unittest.main()
