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

    def rank_fixture(self, folder):
        trace=folder/'process-trace';trace.mkdir()
        (folder/'origin.json').write_text(json.dumps({'instance':'I','qpc_frequency':1000}))
        (trace/'session.json').write_text(json.dumps({'status':'stopped','decode_exit':0,'qpc_frequency':1000,
            'baseline_end_qpc':1,'stopped_qpc':1000}))
        (trace/'baseline.json').write_text('[]')
        query={'query':'I:Q1','kind':'exhausted','calls':[{'received_qpc':110}],
               'entry_received_qpc':95,'return_received_qpc':120}
        (folder/'query-trace.jsonl').write_text(json.dumps(query)+'\n')
        return trace,query

    def ranked_event(self,pid,q,created=1,kind='thread_start'):
        return {'qpc':q,'kind':kind,'pid':pid,'identity':f'{pid}:{created}',
                'process':{'created':created,'image':f'{pid}.exe','parent_pid':7}}

    def test_top_three_starts_in_inclusive_lookback_not_after_or_net_growth(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);self.rank_fixture(folder)
            events=[self.ranked_event(42,q) for q in (99,100,105,110,111)]
            events += [self.ranked_event(42,107,kind='thread_stop')]
            events += [self.ranked_event(pid,106) for pid in (2,2,3,3,4,4)]
            events += [self.ranked_event(5,107,kind='process_start')]
            unknown=self.ranked_event(9,108);unknown.update(identity='9:unresolved',process=None)
            events.append(unknown);events.sort(key=lambda e:e['qpc'])
            old=folder/'process-candidates.tsv';old.write_text('historical report')
            out=folder/'new-report'
            with patch.object(C,'decode',return_value=(events,{'PerfFreq':1000},[])), \
                 patch.object(C,'identities',side_effect=lambda e,*_:e):
                result=C.correlate(folder,10,out)
            report=json.loads((out/'thread-top-delta.json').read_text())
            window=report['windows'][0];top=window['top']
            self.assertEqual([r['pid'] for r in top],[42,2,3])
            self.assertEqual([r['thread_start_count'] for r in top],[3,2,2])
            self.assertEqual(top[0]['thread_starts_per_second'],300)
            self.assertEqual(window['unresolved_thread_starts'],1)
            self.assertEqual(window['thread_start_count'],10)
            self.assertEqual(window['ranked_processes_available'],4)
            self.assertEqual(result['attribution'],'unproven')
            self.assertEqual(old.read_text(),'historical report')
            self.assertFalse((folder/'thread-top-delta.json').exists())

    def test_ranking_keeps_reused_pid_instances_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);self.rank_fixture(folder)
            events=[self.ranked_event(42,101,1),self.ranked_event(42,109,2)]
            with patch.object(C,'decode',return_value=(events,{'PerfFreq':1000},[])), \
                 patch.object(C,'identities',side_effect=lambda e,*_:e):
                C.correlate(folder)
            report=json.loads((folder/'thread-top-delta.json').read_text())
            self.assertEqual([r['identity'] for r in report['windows'][0]['top']],['42:1','42:2'])

    def test_missing_query_clock_does_not_drop_later_valid_query(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);_,query=self.rank_fixture(folder)
            bad={**query,'query':'I:Q0','calls':[]}
            (folder/'query-trace.jsonl').write_text(json.dumps(bad)+'\n'+json.dumps(query)+'\n')
            with patch.object(C,'decode',return_value=([],{'PerfFreq':1000},[])), \
                 patch.object(C,'identities',return_value=[]):
                result=C.correlate(folder)
            self.assertEqual(result['queries'],1)
            self.assertEqual(result['ranking_rows'],0)
            self.assertEqual(len(result['skipped_queries']),1)

    def test_lookback_requires_full_coverage_and_valid_receipt_order(self):
        for baseline,anchor in ((101,110),(1,94),(1,121)):
            with self.subTest(baseline=baseline,anchor=anchor),tempfile.TemporaryDirectory() as directory:
                folder=Path(directory);trace,query=self.rank_fixture(folder)
                session=json.loads((trace/'session.json').read_text());session['baseline_end_qpc']=baseline
                (trace/'session.json').write_text(json.dumps(session))
                query['calls'][-1]['received_qpc']=anchor
                (folder/'query-trace.jsonl').write_text(json.dumps(query)+'\n')
                with patch.object(C,'decode',return_value=([],{'PerfFreq':1000},[])), \
                     patch.object(C,'identities',return_value=[]):
                    result=C.correlate(folder)
                self.assertEqual(result['queries'],0)
                self.assertEqual(len(result['skipped_queries']),1)

    def test_loss_suppresses_all_named_rankings(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);self.rank_fixture(folder)
            event=self.ranked_event(42,105)
            with patch.object(C,'decode',return_value=([event],{'PerfFreq':1000},['ETW lost events or buffers'])):
                lost=C.correlate(folder,10)
            self.assertEqual(lost['queries'],0)
            import csv
            with (folder/'thread-top-delta.tsv').open() as f:
                self.assertEqual(list(csv.DictReader(f,delimiter='\t')),[])

    def test_invalid_lookback_rejected_before_reading(self):
        for lookback in (0,-1,10.01,float('nan'),float('inf')):
            with self.subTest(lookback=lookback),self.assertRaises(ValueError):
                C.correlate(Path('unused'),lookback)

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
