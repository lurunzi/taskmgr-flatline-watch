import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import AutomaticWatch as watch
import OriginCapture
import QueryTrace

class Feed:
    """Drive Session.handle_line without a debugger."""
    def __init__(self, folder):
        self.session=QueryTrace.Session('cdb.exe',4321,folder,watch.write_json,lambda s:None)
    def lines(self,*lines):
        for line in lines:self.session.handle_line(line)
        return self.session

class QueryTraceTests(unittest.TestCase):
    def test_classifies_normal_third_success_and_exhausted_ghost(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            session=Feed(folder).lines(
                'QT VERIFIED',
                'QT E 10','QT N 10 c0000004 0 2000','QT N 10 0 2000 2000','QT X 10 2 1000 3000','QT R 10 0',
                'QT E 10','QT N 10 c0000004 0 2000','QT N 10 c0000004 2000 3000','QT N 10 0 3000 2000',
                'QT X 10 3 1000 4000','QT R 10 8007000e',
                'QT E 11','QT N 11 c0000004 0 1b2cb8','QT N 11 c0000004 1b3000 1b3e60','QT N 11 c0000004 1b4000 1b4198',
                'QT X 11 4 5000 1b9000','QT T 11 293 1b3948 0 0','QT R 11 0')
            self.assertEqual(session.record['status'],'tracing')
            self.assertEqual(session.record['query_counts'],{'normal':1,'third_success':1,'exhausted;ghost':1})
            events=[json.loads(l) for l in (folder/'query-trace.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual([e['kind'] for e in events],['third_success','exhausted'])
            self.assertEqual(events[0]['hr'],'0x8007000e')
            exhausted=events[1]
            self.assertTrue(exhausted['ghost'])
            self.assertEqual([c['status'] for c in exhausted['calls']],['0xc0000004']*3)
            self.assertEqual(exhausted['tail'],{'records':0x293,'offset':0x1b3948,'pid':0,'create_time':0})

    def test_exhausted_tail_with_real_identity_is_not_a_ghost(self):
        with tempfile.TemporaryDirectory() as directory:
            session=Feed(Path(directory)).lines('QT E 7','QT N 7 c0000004 0 10','QT N 7 c0000004 10 20',
                'QT N 7 c0000004 20 30','QT X 7 4 0 30','QT T 7 248 189cd8 13c50 1dd5418f35d5c7a','QT R 7 0')
            self.assertEqual(session.record['query_counts'],{'exhausted':1})

    def test_threads_are_tracked_separately_and_other_output_is_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            session=Feed(folder).lines('QT E 1','QT E 2','QT N 2 0 2000 2000','QT N 1 c0000004 0 2000',
                'TaskManagerDataLayer.dll!X: LogHr(1) tid(1) 8007000E Not enough memory','QT R 2 0',
                'QT N 1 0 2000 2000','QT R 1 0')
            self.assertEqual(session.record['query_counts'],{'normal':2})
            self.assertIn('8007000E',(folder/'cdb.log').read_text(encoding='utf-8'))

    def test_module_mismatch_and_aggregation_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            session=Feed(Path(directory)).lines('QT MISMATCH')
            self.assertEqual(session.record['status'],'module_mismatch')
            session=Feed(Path(directory)).session
            with patch.object(session,'detach') as detach, patch.object(QueryTrace.threading,'Thread') as thread:
                session.handle_line('QT A')
                self.assertEqual(session.record['status'],'aggregation_detected')
                session.handle_line('QT D')
                self.assertEqual(thread.call_args.kwargs['target'],detach)

    def test_scripts_use_forward_slashes_and_one_command_per_dump_line(self):
        with tempfile.TemporaryDirectory(prefix='qt') as directory:
            folder=Path(directory)
            if ' ' in str(folder.resolve()):
                self.skipTest('temporary directory contains a space')
            init=QueryTrace.write_scripts(folder).read_text(encoding='ascii')
            self.assertNotIn('\\',init.replace('\\n',''))
            for rva in ('0x46158','0x461ec','0x46325','0x4638c'):
                self.assertIn(f'bp TaskManagerDataLayer+{rva}',init)
            self.assertIn('out:*8007139F*',init)
            self.assertIn('.echo QT MISMATCH; qd',init)
            aggregation=(folder/'cdb/aggregation.cdb').read_text(encoding='ascii').splitlines()
            dump=[l for l in aggregation if l.startswith('.dump')]
            self.assertEqual(len(dump),1)
            self.assertEqual(aggregation[aggregation.index(dump[0])+1],'.echo QT D')
            self.assertLess(aggregation.index('bc *'),aggregation.index(dump[0]))

    def test_script_folder_with_space_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                QueryTrace.write_scripts(Path(directory)/'has space')

    def fake_cdb(self,session,reading):
        """reading: cdb consumes stdin at once (at a prompt); otherwise only after a break-in."""
        session.ACK_FAST=session.ACK_SLOW=.05
        session.process=unittest.mock.Mock();session.process.poll.return_value=None
        sent=[]
        def acknowledge():
            for marker in ('G','Q'):
                if f'QT {marker}' in ''.join(sent):session.markers[marker].set()
        def write(data):
            sent.append(data.decode())
            if reading:acknowledge()
        session.process.stdin.write.side_effect=write
        return sent,acknowledge

    def test_running_target_gets_one_break_in_per_stage_and_is_never_terminated(self):
        with tempfile.TemporaryDirectory() as directory:
            session=Feed(Path(directory)).session
            sent,acknowledge=self.fake_cdb(session,reading=False)
            with patch.object(QueryTrace,'break_in',side_effect=lambda pid:acknowledge()) as brk, \
                 patch.object(QueryTrace.time,'sleep'):
                session.detach()
            self.assertEqual(''.join(sent),'bc *\nsxi out\n.echo QT G\ng\n.echo QT Q\nqd\n')
            self.assertEqual(brk.call_count,2)
            session.process.terminate.assert_not_called();session.process.kill.assert_not_called()
            session.detach()
            self.assertEqual(brk.call_count,2)

    def test_stale_prompt_does_not_skip_the_break_in(self):
        with tempfile.TemporaryDirectory() as directory:
            session=Feed(Path(directory)).session
            sent,acknowledge=self.fake_cdb(session,reading=False)
            session.at_prompt=True  # prompt text left over after `g`
            with patch.object(QueryTrace,'break_in',side_effect=lambda pid:acknowledge()) as brk, \
                 patch.object(QueryTrace.time,'sleep'):
                session.detach()
            self.assertEqual(brk.call_count,2)

    def test_cdb_reading_input_gets_no_break_in(self):
        with tempfile.TemporaryDirectory() as directory:
            session=Feed(Path(directory)).session
            self.fake_cdb(session,reading=True)
            with patch.object(QueryTrace,'break_in') as brk, patch.object(QueryTrace.time,'sleep'):
                session.detach()
            brk.assert_not_called()

    def test_unconfirmed_first_stage_never_sends_quit_or_a_second_break_in(self):
        with tempfile.TemporaryDirectory() as directory:
            session=Feed(Path(directory)).session
            sent,_=self.fake_cdb(session,reading=False)
            with patch.object(QueryTrace,'break_in') as brk, patch.object(QueryTrace.time,'sleep'):
                session.detach()
            self.assertEqual(brk.call_count,1)
            self.assertNotIn('qd',''.join(sent))
            self.assertIn('stalled',session.record['detach'])
            session.process.terminate.assert_not_called();session.process.kill.assert_not_called()

    def test_marker_lines_set_handshake_events(self):
        with tempfile.TemporaryDirectory() as directory:
            # Observed cdb output: pipe input is not echoed, so prompts share the line.
            session=Feed(Path(directory)).lines('0:004> 0:004> 0:004> QT G')
            self.assertTrue(session.markers['G'].is_set());self.assertFalse(session.markers['Q'].is_set())
            session=Feed(Path(directory)).lines('0:004> .echo QT VERIFIED')
            self.assertEqual(session.record['status'],'attaching')

    def test_watchdog_detaches_an_unexpected_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            session=Feed(Path(directory)).session
            session.process=unittest.mock.Mock();session.process.poll.return_value=None
            session.at_prompt=True;session.prompt_since=0
            with patch.object(QueryTrace.time,'sleep'), patch.object(QueryTrace.time,'monotonic',return_value=100), \
                 patch.object(session,'detach',side_effect=lambda:setattr(session,'stopping',True)) as detach:
                session.watchdog()
            detach.assert_called_once()
            self.assertIn('unexpected_break',session.record)

class OriginTraceModeTests(unittest.TestCase):
    def origin(self,directory,trace=True):
        procdump=Path(directory)/'procdump.exe';procdump.write_bytes(b'')
        return OriginCapture.OriginWatch(procdump,Path(directory),lambda p:{'ok':True},watch.write_json,trace)

    def test_trace_mode_uses_cdb_instead_of_procdump(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(OriginCapture,'find_cdb',return_value='cdb.exe'), \
             patch.object(QueryTrace,'dll_matches',return_value=True), patch.object(QueryTrace.Session,'start') as start, \
             patch.object(OriginCapture.subprocess,'Popen') as popen:
            origin=self.origin(directory)
            self.assertTrue(origin.attach(42))
            start.assert_called_once();popen.assert_not_called()
            self.assertIsInstance(origin.sessions[42],QueryTrace.Session)
            self.assertTrue(origin.sessions[42].folder.name.endswith('-42-trace'))

    def test_changed_dll_falls_back_to_procdump_and_records_why(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(OriginCapture,'find_cdb',return_value='cdb.exe'), \
             patch.object(QueryTrace,'dll_matches',return_value=False), patch.object(QueryTrace.Session,'start') as start, \
             patch.object(OriginCapture.subprocess,'Popen') as popen, patch.object(OriginCapture.threading,'Thread'):
            origin=self.origin(directory)
            origin.attach(42)
            start.assert_not_called();popen.assert_called_once()
            record=json.loads(next(Path(directory).glob('origin-*/origin.json')).read_text(encoding='utf-8'))
            self.assertEqual(record['query_trace_unavailable'],'dll_hash_mismatch')

    def test_in_memory_mismatch_falls_back_after_cdb_exits(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(OriginCapture.subprocess,'Popen') as popen, \
             patch.object(OriginCapture.threading,'Thread'):
            origin=self.origin(directory)
            session=QueryTrace.Session('cdb.exe',42,Path(directory),watch.write_json,origin.trace_finished)
            session.record['status']='module_mismatch'
            origin.trace_finished(session)
            popen.assert_called_once()

    def test_trace_dump_is_validated_and_stacks_extracted(self):
        with tempfile.TemporaryDirectory() as directory:
            origin=self.origin(directory)
            folder=Path(directory)/'t';folder.mkdir();(folder/'Taskmgr-origin.dmp').write_bytes(b'x')
            session=QueryTrace.Session('cdb.exe',42,folder,watch.write_json,origin.trace_finished)
            session.record['status']='ended'
            with patch.object(origin,'extract_stacks') as stacks:
                origin.trace_finished(session)
            self.assertEqual(session.record['status'],'captured')
            stacks.assert_called_once()

    def test_finished_old_session_does_not_drop_newer_session(self):
        with tempfile.TemporaryDirectory() as directory:
            origin=self.origin(directory)
            old=QueryTrace.Session('cdb.exe',42,Path(directory),watch.write_json,origin.trace_finished)
            new=QueryTrace.Session('cdb.exe',42,Path(directory),watch.write_json,origin.trace_finished)
            old.record['status']='cancelled';origin.sessions[42]=new
            origin.trace_finished(old)
            self.assertIs(origin.sessions[42],new)

    def test_cancel_detaches_trace_sessions(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(OriginCapture.subprocess,'run') as run:
            origin=self.origin(directory)
            session=unittest.mock.Mock(spec=QueryTrace.Session);session.record={}
            origin.sessions[42]=session;origin.attempted.add(42)
            with patch.object(OriginCapture.time,'monotonic',side_effect=[0,100]):
                origin.cancel_all()
            session.detach.assert_called_once();run.assert_not_called()
            self.assertTrue(session.record['cancelled']);self.assertNotIn(42,origin.attempted)

    def test_monitor_reports_trace_mode(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)):
            monitor=watch.Monitor(query_trace=True)
            monitor.publish('waiting')
            state=json.loads((Path(directory)/'status.json').read_text(encoding='utf-8'))
            self.assertEqual(state['origin_mode'],'query_trace')

if __name__=='__main__':unittest.main()
