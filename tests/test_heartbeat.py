import json
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

os.environ['TASKMGR_WATCH_TEST'] = '1'

import IncidentAnalysis as A
from RealtimeTrace import (
    RealtimeConsumer, EVENT_FILTER_TYPE_EVENT_NAME, TASKMGR_GUID,
    EVENT_RECORD, parse_guid
)


class HeartbeatTests(unittest.TestCase):
    def test_filter_descriptor_and_payload_packing(self):
        """EVENT_FILTER_EVENT_NAME pack matches evntprov.h specification."""
        self.assertEqual(EVENT_FILTER_TYPE_EVENT_NAME, 0x80000400)
        name_bytes = b'CurrentUpdateCycle\x00FallbackError\x00'
        header_bytes = struct.pack('<QQBBH', 0, 0, 5, 1, 2)
        filter_data = header_bytes + name_bytes

        # Check unpacked header
        any_kw, all_kw, level, filter_in, count = struct.unpack('<QQBBH', header_bytes)
        self.assertEqual(any_kw, 0)
        self.assertEqual(all_kw, 0)
        self.assertEqual(level, 5)
        self.assertEqual(filter_in, 1)
        self.assertEqual(count, 2)
        self.assertEqual(len(filter_data), 20 + len(name_bytes))

    def test_realtime_consumer_parses_heartbeat_event(self):
        """RealtimeConsumer._on_event extracts CurrentUpdateCycle fields."""
        import ctypes
        consumer = RealtimeConsumer(target_pid=4242)

        # Construct raw payload for UpdateStarted
        raw_payload = b'DataModel\x00UpdateStarted\x00' + struct.pack('<Q', 1) + b'corr-id-123.0\x00'

        rec = EVENT_RECORD()
        rec.EventHeader.ProcessId = 4242
        rec.EventHeader.ThreadId = 999
        rec.EventHeader.ProviderId = parse_guid(TASKMGR_GUID)
        buf = ctypes.create_string_buffer(raw_payload)
        rec.UserData = ctypes.cast(buf, ctypes.c_void_p)
        rec.UserDataLength = len(raw_payload)

        consumer._on_event(ctypes.pointer(rec))
        hb = consumer.get_heartbeat_events()
        self.assertEqual(len(hb), 1)
        self.assertEqual(hb[0]['pid'], 4242)
        self.assertEqual(hb[0]['tid'], 999)
        self.assertEqual(hb[0]['layer'], 'DataModel')
        self.assertEqual(hb[0]['operation'], 'UpdateStarted')
        self.assertEqual(hb[0]['tab'], 1)
        self.assertEqual(hb[0]['correlation_id'], 'corr-id-123.0')

        # Construct raw payload for UpdateFinalized
        raw_payload2 = b'DataModel\x00UpdateFinalized\x00' + struct.pack('<Q', 1) + b'corr-id-123.0\x00'
        buf2 = ctypes.create_string_buffer(raw_payload2)
        rec2 = EVENT_RECORD()
        rec2.EventHeader.ProcessId = 4242
        rec2.EventHeader.ThreadId = 999
        rec2.EventHeader.ProviderId = parse_guid(TASKMGR_GUID)
        rec2.UserData = ctypes.cast(buf2, ctypes.c_void_p)
        rec2.UserDataLength = len(raw_payload2)

        consumer._on_event(ctypes.pointer(rec2))
        hb2 = consumer.get_heartbeat_events()
        self.assertEqual(len(hb2), 2)
        self.assertEqual(hb2[1]['operation'], 'UpdateFinalized')

        st = consumer.status()
        self.assertEqual(st['heartbeat_count'], 2)
        self.assertIsNotNone(st['last_heartbeat_utc'])

    def test_heartbeat_stopped_before_freeze_onset(self):
        """Incident analysis identifies when update cycles stopped before freeze."""
        freq = 10_000_000
        first_qpc = 100 * freq
        confirmed_qpc = 130 * freq
        native_qpc = 88 * freq

        # Events emitted at 1s intervals from t=80s to t=86s, then stopped
        events = []
        for sec in range(80, 87):
            t_qpc = sec * freq
            events.append({'qpc': t_qpc, 'utc': f'2026-10-26T22:00:{sec:02d}.000Z',
                           'pid': 1, 'tid': 2, 'layer': 'DataModel', 'operation': 'UpdateStarted',
                           'tab': 1, 'correlation_id': f'c{sec}'})
            events.append({'qpc': t_qpc + int(0.02 * freq), 'utc': f'2026-10-26T22:00:{sec:02d}.020Z',
                           'pid': 1, 'tid': 2, 'layer': 'DataModel', 'operation': 'UpdateFinalized',
                           'tab': 1, 'correlation_id': f'c{sec}'})

        with tempfile.TemporaryDirectory() as d:
            hb_file = Path(d) / 'heartbeat.json'
            hb_file.write_text(json.dumps({'events': events}), encoding='utf-8')

            markers = {
                'first_candidate': {'qpc': first_qpc},
                'confirmed': {'qpc': confirmed_qpc}
            }
            native_sig = {'recorded': True, 'qpc': native_qpc}

            res = A.analyze_heartbeat(hb_file, {}, markers, native_sig, freq)
            self.assertTrue(res['recorded'])
            self.assertFalse(res['continued_during_freeze'])
            self.assertEqual(res['update_started_count'], 7)
            self.assertEqual(res['update_finalized_count'], 7)
            self.assertAlmostEqual(res['interval_seconds']['median'], 1.0, places=2)

            # Last update at t=86s, expected next at t=87s
            self.assertEqual(res['first_missing_qpc'], 87 * freq)
            # Lead before first candidate (100 - 87 = 13s)
            self.assertEqual(res['lead_seconds_before_first_candidate'], 13.0)
            # Relative to native error (87 - 88 = -1.0s)
            self.assertEqual(res['timing_relative_to_native_error_seconds'], -1.0)

            # Report rendering
            text_en = A.heartbeat_text(res, zh=False)
            self.assertIn('Data layer heartbeat: CurrentUpdateCycle', text_en)
            self.assertIn('Stopped before freeze onset', text_en)
            self.assertIn('~13.0 s before first flat observation', text_en)
            self.assertIn('not a recovery criterion', text_en)

            text_zh = A.heartbeat_text(res, zh=True)
            self.assertIn('数据层心跳：CurrentUpdateCycle', text_zh)
            self.assertIn('在冻结起点前停止循环', text_zh)
            self.assertIn('约 13.0 秒', text_zh)
            self.assertIn('不作为恢复判据', text_zh)

    def test_heartbeat_continued_during_freeze(self):
        """Incident analysis identifies when update cycles continued through freeze."""
        freq = 10_000_000
        first_qpc = 100 * freq
        confirmed_qpc = 130 * freq

        events = []
        for sec in range(95, 135):
            t_qpc = sec * freq
            events.append({'qpc': t_qpc, 'utc': f'2026-10-26T22:00:{sec:02d}.000Z',
                           'pid': 1, 'tid': 2, 'layer': 'DataModel', 'operation': 'UpdateStarted',
                           'tab': 1, 'correlation_id': f'c{sec}'})
            events.append({'qpc': t_qpc + int(0.02 * freq), 'utc': f'2026-10-26T22:00:{sec:02d}.020Z',
                           'pid': 1, 'tid': 2, 'layer': 'DataModel', 'operation': 'UpdateFinalized',
                           'tab': 1, 'correlation_id': f'c{sec}'})

        with tempfile.TemporaryDirectory() as d:
            hb_file = Path(d) / 'heartbeat.json'
            hb_file.write_text(json.dumps({'events': events}), encoding='utf-8')

            markers = {
                'first_candidate': {'qpc': first_qpc},
                'confirmed': {'qpc': confirmed_qpc}
            }
            res = A.analyze_heartbeat(hb_file, {}, markers, None, freq)
            self.assertTrue(res['recorded'])
            self.assertTrue(res['continued_during_freeze'])

            text_en = A.heartbeat_text(res, zh=False)
            self.assertIn('Continued during freeze', text_en)

            text_zh = A.heartbeat_text(res, zh=True)
            self.assertIn('冻结期间持续循环', text_zh)

    def test_missing_or_empty_heartbeat(self):
        res_none = A.analyze_heartbeat(None, {}, {}, None, 10_000_000)
        self.assertFalse(res_none['recorded'])
        self.assertIn('no heartbeat file found', res_none['reason'])

        with tempfile.TemporaryDirectory() as d:
            empty_file = Path(d) / 'heartbeat.json'
            empty_file.write_text(json.dumps({'events': []}), encoding='utf-8')
            res_empty = A.analyze_heartbeat(empty_file, {}, {}, None, 10_000_000)
            self.assertFalse(res_empty['recorded'])
            self.assertIn('empty', res_empty['reason'])

            text = A.heartbeat_text(res_empty, zh=False)
            self.assertIn('Heartbeat not recorded', text)


if __name__ == '__main__':
    unittest.main()
