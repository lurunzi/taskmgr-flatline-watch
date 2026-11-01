import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import ProcessTrace as P
from IncidentAnalysis import _decoder as D


class DecodeStorageTests(unittest.TestCase):
    def xml(self):
        fields=dict(PerfFreq=1000,StartTime=10000000,EndTime=11000000,EventsLost=0,
                    BuffersLost=0,BuffersWritten=1,BufferSize=65536,MaxFileSize=64,ReservedFlags=1)
        def event(provider,kind,clock,data):
            return (f'<Event xmlns="{D.NS["e"]}"><System><Provider Guid="{provider}"/>'
                    f'<EventID>{kind}</EventID><TimeCreated RawTime="{clock}"/></System><EventData>'+
                    ''.join(f'<Data Name="{k}">{v}</Data>' for k,v in data.items())+'</EventData></Event>')
        return ('<Events>'+event('header',0,100,fields)+
                event(D.PROVIDER,3,110,dict(ProcessID=42,ThreadID=1))+
                event(D.PROVIDER,1,150,dict(ProcessID=42,ParentProcessID=7,CreateTime='2026-10-13T12:00:00Z',ImageName='child.exe'))+
                event(D.PROVIDER,3,220,dict(ProcessID=42,ThreadID=2))+
                event(D.PROVIDER,2,300,dict(ProcessID=42,ParentProcessID=7,CreateTime='2026-10-13T12:00:00Z',ImageName='child.exe'))+
                '</Events>').encode()

    def test_compact_decode_is_lossless_and_keeps_original_etl(self):
        with tempfile.TemporaryDirectory() as d:
            session=P.Session(d);raw=session.folder/'process.etl';raw.write_bytes(b'original')
            payload=self.xml()
            def launch(args,**kwargs):
                Path(args[args.index('-o')+1]).write_bytes(payload)
                return Mock(poll=Mock(return_value=0),returncode=0)
            with patch.object(P.subprocess,'Popen',side_effect=launch):session.decode(compact=True)
            self.assertEqual(gzip.decompress((session.folder/'events.xml.gz').read_bytes()),payload)
            self.assertFalse((session.folder/'events.decode.xml').exists())
            self.assertEqual(raw.read_bytes(),b'original')
            self.assertEqual(session.record['decode_exit'],0)

    def test_finished_child_still_cannot_bypass_output_bound(self):
        with tempfile.TemporaryDirectory() as d:
            session=P.Session(d)
            def launch(args,**kwargs):
                Path(args[args.index('-o')+1]).write_bytes(b'x'*33)
                return Mock(poll=Mock(return_value=0),returncode=0)
            with patch.object(P,'COMPACT_XML_LIMIT',32),patch.object(P.subprocess,'Popen',side_effect=launch):
                with self.assertRaisesRegex(RuntimeError,'bound exceeded'):session.decode(compact=True)
            self.assertIsNone(session.record['decode_exit'])
            self.assertTrue((session.folder/'events.decode.xml').exists())

    def test_existing_compact_attempt_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            session=P.Session(d);p=session.folder/'events.decode.xml';p.write_bytes(b'old attempt')
            with patch.object(P.subprocess,'Popen') as child:
                with self.assertRaisesRegex(RuntimeError,'output exists'):session.decode(compact=True)
                child.assert_not_called()
            self.assertEqual(p.read_bytes(),b'old attempt')

    def test_thread_window_keeps_global_lifetimes_and_actual_trace_start(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'events.xml.gz';p.write_bytes(gzip.compress(self.xml()))
            events,header,errors=D.decode(p,thread_window=(200,250))
            self.assertEqual([(e['kind'],e['qpc']) for e in events],
                             [('process_start',150),('thread_start',220),('process_stop',300)])
            self.assertEqual(header['retained_first_qpc'],110)
            self.assertEqual(header['retained_last_qpc'],300)
            self.assertEqual(header['scanned_events'],4)
            self.assertEqual(errors,[])

    def test_truncated_compressed_trace_never_returns_partial_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'events.xml.gz';p.write_bytes(gzip.compress(self.xml())[:-12])
            with self.assertRaises((EOFError,OSError)):D.decode(p,thread_window=(200,250))

    def test_json_decode_keeps_thread_window_and_header(self):
        with tempfile.TemporaryDirectory() as d:
            header = {'PerfFreq': 1000, 'raw_start': 100, 'native_events': [{'hresult': '0x8007139f'}]}
            evts = [
                {'qpc': 110, 'kind': 'thread_start', 'pid': 42, 'tid': 1},
                {'qpc': 150, 'kind': 'process_start', 'pid': 42, 'tid': 1, 'parent_pid': 7, 'image': 'child.exe'},
                {'qpc': 220, 'kind': 'thread_start', 'pid': 42, 'tid': 2},
                {'qpc': 300, 'kind': 'process_stop', 'pid': 42, 'tid': 1, 'image': 'child.exe'}
            ]
            data = json.dumps({'header': header, 'events': evts, 'errors': []})
            p = Path(d)/'events.json.gz'
            p.write_bytes(gzip.compress(data.encode('utf-8')))
            events, hdr, errs = D.decode(p, thread_window=(200, 250))
            self.assertEqual([(e['kind'], e['qpc']) for e in events],
                             [('process_start', 150), ('thread_start', 220), ('process_stop', 300)])
            self.assertEqual(hdr['raw_start'], 100)
            self.assertEqual(hdr['native_events'][0]['hresult'], '0x8007139f')
            self.assertEqual(errs, [])

    def test_direct_decode_success_creates_events_json_gz(self):
        with tempfile.TemporaryDirectory() as d:
            session = P.Session(d)
            raw = session.folder/'process.etl'
            raw.write_bytes(b'simulated etl content')
            fake_header = {'PerfFreq': 1000, 'raw_start': 100, 'native_events': []}
            fake_events = [{'qpc': 150, 'kind': 'process_start', 'pid': 42, 'tid': 1}]
            with patch.object(P, 'decode_etl_file', return_value=(fake_header, fake_events, [])):
                session.decode(compact=True)
            out_file = session.folder/'events.json.gz'
            self.assertTrue(out_file.exists())
            self.assertEqual(session.record['decode'], 'complete')
            self.assertEqual(session.record['decode_file'], 'events.json.gz')
            self.assertEqual(session.record['decode_exit'], 0)
            payload = json.loads(gzip.decompress(out_file.read_bytes()))
            self.assertEqual(payload['header']['PerfFreq'], 1000)
            self.assertEqual(len(payload['events']), 1)

    def test_direct_decode_bound_exceeded_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as d:
            session = P.Session(d)
            raw = session.folder/'process.etl'
            raw.write_bytes(b'simulated etl content')
            with patch.object(P, 'decode_etl_file', side_effect=RuntimeError('ETW decoder bound exceeded; ETL retained')):
                with self.assertRaisesRegex(RuntimeError, 'bound exceeded'):
                    session.decode(compact=True)
            self.assertEqual(session.record['decode'], 'incomplete: decoder time/output bound')

    def test_parse_fallback_error_sample_fixture(self):
        import base64
        fixture_path = Path(__file__).resolve().parent / 'fixtures' / 'sample_fallback_events.json'
        self.assertTrue(fixture_path.exists(), "Sample fallback events fixture must exist")
        items = json.loads(fixture_path.read_text(encoding='utf-8'))
        self.assertEqual(len(items), 5)

        expected = [
            {'file': '', 'line': 0, 'failure_type': 2, 'module': 'TaskManagerDataLayer.dll', 'hresult': 2147947423, 'message': '', 'thread_id': 26532},
            {'file': '', 'line': 0, 'failure_type': 2, 'module': 'TaskManagerDataLayer.dll', 'hresult': 2147947423, 'message': '', 'thread_id': 26532},
            {'file': 'base\\diagnosis\\pdui\\atm\\datamonitors\\aggregationdatamonitor.cpp', 'line': 640, 'failure_type': 1, 'module': 'TaskManagerDataLayer.dll', 'hresult': 2147947423, 'message': 'winrt::hresult_error: The group or resource is not in the correct state to perform the requested operation.', 'thread_id': 26532},
            {'file': 'base\\diagnosis\\pdui\\atm\\datamonitors\\aggregationdatamonitor.cpp', 'line': 159, 'failure_type': 1, 'module': 'TaskManagerDataLayer.dll', 'hresult': 2147947423, 'message': '', 'thread_id': 26532},
            {'file': 'base\\diagnosis\\pdui\\atm\\core\\Core.cpp', 'line': 407, 'failure_type': 0, 'module': 'TaskManagerDataLayer.dll', 'hresult': 2147947423, 'message': '', 'thread_id': 26532},
        ]

        for item, exp in zip(items, expected):
            raw = base64.b64decode(item['raw_b64'])
            parsed = P.parse_fallback_error(None, raw)
            for k, v in exp.items():
                self.assertEqual(parsed.get(k), v, f"Field mismatch for {k}: {parsed.get(k)} != {v}")

    def test_probe_etl_regression_against_tracerpt_xml(self):
        probe_etl = Path(r'D:\TaskmgrFreezeCaptures\wil-probe-20261025\wil-20261031-1550-to-20261101-1500.etl')
        xml_path = Path(r'D:\TaskmgrFreezeCaptures\wil-probe-20261025\decode-1031-to-1101\events.xml')
        if not probe_etl.exists() or not xml_path.exists():
            self.skipTest("Probe ETL or tracerpt XML not present on this machine")

        res = P.decode_etl_file(probe_etl)
        self.assertIsNotNone(res)
        header, _, _ = res
        native_events = header.get('native_events', [])
        self.assertEqual(len(native_events), 25)

        for group_idx in range(5):
            group = native_events[group_idx*5 : (group_idx+1)*5]
            self.assertEqual(group[0]['file'], '')
            self.assertEqual(group[0]['line'], 0)
            self.assertEqual(group[0]['failure_type'], 2)

            self.assertEqual(group[1]['file'], '')
            self.assertEqual(group[1]['line'], 0)
            self.assertEqual(group[1]['failure_type'], 2)

            self.assertEqual(group[2]['file'], 'base\\diagnosis\\pdui\\atm\\datamonitors\\aggregationdatamonitor.cpp')
            self.assertEqual(group[2]['line'], 640)
            self.assertEqual(group[2]['failure_type'], 1)

            self.assertEqual(group[3]['file'], 'base\\diagnosis\\pdui\\atm\\datamonitors\\aggregationdatamonitor.cpp')
            self.assertEqual(group[3]['line'], 159)
            self.assertEqual(group[3]['failure_type'], 1)

            self.assertEqual(group[4]['file'], 'base\\diagnosis\\pdui\\atm\\core\\Core.cpp')
            self.assertEqual(group[4]['line'], 407)
            self.assertEqual(group[4]['failure_type'], 0)


if __name__=='__main__':unittest.main()
