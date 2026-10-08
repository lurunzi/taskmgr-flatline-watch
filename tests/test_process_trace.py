import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import ProcessTrace as P
spec = importlib.util.spec_from_file_location('correlate',ROOT/'tools/correlate-process-trace.py')
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)


class ProcessTraceTests(unittest.TestCase):
    def test_filetime_preserves_submicroseconds_and_offsets(self):
        self.assertEqual(C.ticks('2026-10-08T13:00:55.3968469Z'),134359380553968469)
        self.assertEqual(C.ticks('2026-10-08T14:00:55.3968469+01:00'),134359380553968469)

    def test_pid_reuse_uses_lifetime_not_name_or_pid_alone(self):
        h={'raw_start':100,'StartTime':1000,'PerfFreq':10_000_000}
        def event(kind,q,created=None):
            return dict(kind=kind,qpc=q,pid=42,tid=1,create_time=created,image='same.exe',parent_pid=7,sequence=None)
        events=[event('process_start',100,1000),event('thread_start',110),event('process_stop',120,1000),
                event('thread_start',125,1000),event('process_start',130,1030),event('thread_start',140)]
        C.identities(events,[],h)
        self.assertEqual(events[1]['identity'],'42:1000')
        self.assertEqual(events[3]['identity'],'42:unresolved')
        self.assertEqual(events[5]['identity'],'42:1030')

    def test_failed_start_keeps_owned_session_for_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            session=P.Session(directory)
            session.command=Mock(side_effect=RuntimeError('timeout'))
            with self.assertRaises(RuntimeError): session.start()
            self.assertTrue(session.active)
            session.command=Mock()
            session.stop()
            self.assertFalse(session.active)
            self.assertIn(session.name,session.command.call_args.args[0])

    def test_stop_retries_only_its_owned_session(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(P.time,'sleep'):
            session=P.Session(directory);session.active=True
            session.command=Mock(side_effect=[RuntimeError('transient'),None])
            session.stop()
            self.assertFalse(session.active)
            self.assertEqual(session.command.call_count,2)
            self.assertTrue(all(session.name in c.args[0] for c in session.command.call_args_list))

    def test_raw_clock_and_payload_pid_win_over_rendered_time_and_header_pid(self):
        ns=C.NS['e']
        fields={'PerfFreq':1000,'StartTime':10000000,'EndTime':11000000,'EventsLost':2,
                'BuffersLost':0,'BuffersWritten':16,'BufferSize':65536,'MaxFileSize':1,'ReservedFlags':1}
        data=''.join(f'<Data Name="{k}">{v}</Data>' for k,v in fields.items())
        xml=f'''<Events><Event xmlns="{ns}"><System><Provider Guid="header"/>
        <EventID>0</EventID><TimeCreated RawTime="100" SystemTime="bogus"/></System><EventData>{data}</EventData></Event>
        <Event xmlns="{ns}"><System><Provider Guid="{C.PROVIDER}"/><EventID>3</EventID>
        <TimeCreated RawTime="123" SystemTime="bogus"/><Execution ProcessID="999"/></System>
        <EventData><Data Name="ProcessID">42</Data><Data Name="ThreadID">17</Data></EventData></Event></Events>'''
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'events.xml';p.write_text(xml)
            events,_,errors=C.decode(p)
        self.assertEqual((events[0]['qpc'],events[0]['pid']),(123,42))
        self.assertIn('ETW lost events or buffers',errors)
        self.assertIn('circular trace may have overwritten earlier buffers',errors)

    def test_margin_is_not_inside_window_or_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory); trace=folder/'process-trace';trace.mkdir()
            (folder/'origin.json').write_text(json.dumps({'instance':'I','qpc_frequency':1000}))
            (trace/'session.json').write_text(json.dumps({'status':'stopped','decode_exit':0,'baseline_end_qpc':0,'stopped_qpc':1000}))
            (trace/'baseline.json').write_text('[]')
            (folder/'query-trace.jsonl').write_text(json.dumps({'query':'I:Q1','kind':'exhausted','calls':[],
                'entry_received_qpc':100,'return_received_qpc':120})+'\n')
            event={'qpc':95,'kind':'thread_start','pid':42,'identity':'42:unresolved','process':None}
            h={'PerfFreq':1000}
            with patch.object(C,'decode',return_value=([event],h,[])),patch.object(C,'identities',side_effect=lambda e,*_:e):
                result=C.correlate(folder,10)
            self.assertEqual(result['attribution'],'unproven')
            import csv
            with (folder/'process-candidates.tsv').open() as f:
                row=next(csv.DictReader(f,delimiter='\t'))
            self.assertEqual(row['inside_receipt_window'],'0')
            self.assertEqual(row['margin_only'],'1')
            with patch.object(C,'decode',return_value=([event],h,['ETW lost events or buffers'])):
                lost=C.correlate(folder,10)
            self.assertEqual(lost['queries'],0)
            with (folder/'process-candidates.tsv').open() as f:
                self.assertEqual(list(csv.DictReader(f,delimiter='\t')),[])

    def test_debugger_cleanup_error_still_stops_owned_etw(self):
        spec=importlib.util.spec_from_file_location('capture',ROOT/'tools/capture-address-chain.py')
        runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'capture'
            trace=Mock(name='owned_etw');trace.name='owned';trace.active=False
            debugger=Mock();debugger.process.poll.return_value=0
            debugger.start.side_effect=RuntimeError('simulated start failure')
            debugger.memory.debugger.side_effect=RuntimeError('simulated process handle failure')
            original_read=Path.read_text
            def read(path,*args,**kwargs):
                if str(path).replace('\\','/')=='D:/TaskmgrFreezeCaptures/status.json':
                    return json.dumps({'state':'stopped','watcher_pid':123,'dump_running':False})
                return original_read(path,*args,**kwargs)
            with patch.object(sys,'argv',['capture','--pid','123','--minutes','1','--process-events','--output',str(output)]), \
                 patch.object(runner.ctypes.windll.shell32,'IsUserAnAdmin',return_value=True), \
                 patch.object(runner,'require_taskmgr'),patch.object(runner,'storage_available',return_value=True), \
                 patch.object(runner,'find_cdb',return_value='unused'),patch.object(Path,'read_text',read), \
                 patch.object(runner.AddressChain,'Memory'),patch.object(runner.AddressChain,'Session',return_value=debugger), \
                 patch.object(runner.ProcessTrace,'Session',return_value=trace):
                runner.main()
            trace.stop.assert_called_once();trace.decode.assert_called_once()
            state=json.loads((output/'capture.json').read_text())
            self.assertIn('handle failure',state['cleanup_error'])


if __name__=='__main__': unittest.main()
