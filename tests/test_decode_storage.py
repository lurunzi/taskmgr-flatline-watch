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


if __name__=='__main__':unittest.main()
