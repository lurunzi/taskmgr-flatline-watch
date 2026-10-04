import ctypes
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import AutomaticWatch as watch
import NativeCapture
from test_detection import graph

class CaptureFlowTests(unittest.TestCase):
    def setUp(self):
        guard=patch.object(watch.Monitor,'watch_origin')
        guard.start();self.addCleanup(guard.stop)

    def test_new_taskmgr_bypasses_previous_target_cooldown(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)), \
             patch.object(watch.time,'monotonic',return_value=1000), \
             patch.object(watch.threading,'Thread') as thread:
            monitor=watch.Monitor()
            monitor.last_capture=990
            monitor.last_capture_target=(4321,123)
            monitor.trigger({'pid':9876,'hwnd':456,'geometry':[]})
            thread.return_value.start.assert_called_once()
            evidence=json.loads((Path(monitor.last_event)/'event.json').read_text(encoding='utf-8'))
            self.assertEqual(evidence['taskmgr_pid'],9876)
            self.assertEqual(evidence['classification'],'suspected_visual_aggregate_freeze')
            self.assertEqual(monitor.last_capture_target,(9876,456))

    def test_same_target_cooldown_survives_watcher_restart_and_is_visible(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)), \
             patch.object(watch.time,'monotonic',return_value=1000), \
             patch.object(watch.threading,'Thread') as thread:
            folder=Path(directory)/'prior';folder.mkdir()
            watch.write_json(folder/'event.json',{'taskmgr_pid':4321,'window':123,'status':'dump_failed'})
            monitor=watch.Monitor()
            self.assertEqual(monitor.last_capture_target,(4321,123))
            monitor.trigger({'pid':4321,'hwnd':123,'geometry':[]})
            thread.assert_not_called()
            state=json.loads((Path(directory)/'status.json').read_text(encoding='utf-8'))
            self.assertEqual(state['state'],'cooldown')
            self.assertGreater(state['cooldown_remaining'],590)

    def test_non_taskmgr_process_is_rejected_before_procdump(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch.subprocess,'run') as run:
            result=watch.write_dump(os.getpid(),Path(directory)/'rejected.dmp')
            self.assertFalse(result['success'])
            self.assertIn('not Windows Task Manager',result['error'])
            run.assert_not_called()

    def test_visual_trigger_saves_graphs_and_requests_two_dumps(self):
        def target_pid(hwnd, pointer):
            pointer._obj.value=4321
            return 1
        # ProcDump is mocked: this tests orchestration, not actual memory dumping.
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(watch,'OUTPUT',Path(directory)), \
             patch.object(watch.time,'monotonic',return_value=1000) as clock, \
             patch.object(watch.time,'sleep'), \
             patch.object(watch,'write_dump',return_value={'success':True,'test_stub':True}) as dump, \
             patch.object(watch,'validate_full_dump',return_value={'test_stub':True}), \
             patch.object(watch,'restart_taskmgr',return_value={'status':'restarted','new_pid':9876}) as restart, \
             patch.object(NativeCapture.u,'GetWindowThreadProcessId',side_effect=target_pid), \
             patch.object(NativeCapture,'class_name',return_value='TaskManagerWindow'):
            monitor=watch.Monitor()
            for t in range(0,34,2):
                clock.return_value=1000+t
                monitor.accept({'state':'captured','pid':4321,'hwnd':123,'geometry':[[0,0,75,50]]*33,
                                'total':graph(),'cores':graph(15+t%8,wave=True)})
            self.assertIsNotNone(monitor.dump_thread)
            monitor.dump_thread.join(timeout=3)
            self.assertFalse(monitor.dump_thread.is_alive())
            self.assertEqual(dump.call_count,2)
            event=Path(monitor.last_event)
            evidence=json.loads((event/'event.json').read_text(encoding='utf-8'))
            self.assertEqual(evidence['status'],'complete')
            self.assertEqual(len(list(event.glob('*.png'))),6)
            self.assertEqual(len(evidence['dumps']),2)
            self.assertEqual(evidence['taskmgr_pid'],4321)
            restart.assert_called_once_with(4321,123)
            self.assertEqual(evidence['recovery']['status'],'restarted')
            monitor.stop()

    def test_verification_never_restarts_taskmgr(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)), \
             patch.object(watch,'restart_taskmgr') as restart:
            monitor=watch.Monitor()
            monitor.recover(Path(directory),{'classification':'installation_verification','status':'complete'})
            restart.assert_not_called()

    def test_missing_dump_prevents_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)), \
             patch.object(watch,'restart_taskmgr') as restart:
            monitor=watch.Monitor()
            evidence={'classification':'suspected_visual_aggregate_freeze','status':'complete',
                      'dumps':[{'success':True},{'success':True}],'taskmgr_pid':4321,'window':123}
            monitor.recover(Path(directory),evidence)
            restart.assert_not_called()
            self.assertEqual(evidence['recovery']['status'],'failed')

    def test_failed_second_dump_prevents_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)), \
             patch.object(watch,'restart_taskmgr') as restart:
            monitor=watch.Monitor()
            monitor.recover(Path(directory),{'classification':'suspected_visual_aggregate_freeze',
                'status':'dump_failed','dumps':[{'success':True},{'success':False}]})
            restart.assert_not_called()

    def test_background_recovery_stays_visible_over_live_samples(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)):
            monitor=watch.Monitor()
            monitor.dump_thread=unittest.mock.Mock(is_alive=lambda:True)
            monitor.recovery_result={'status':'restarting'}
            monitor.last_status_write=watch.time.monotonic()
            monitor.accept({'state':'waiting'})
            state=json.loads((Path(directory)/'status.json').read_text(encoding='utf-8'))
            self.assertEqual(state['state'],'recovering')
            self.assertEqual(state['sample_state'],'waiting')
            self.assertTrue(state['dump_running'])

    def recovery_evidence(self):
        return {'classification':'suspected_visual_aggregate_freeze','status':'complete',
                'dumps':[{'success':True},{'success':True}],'taskmgr_pid':4321,'window':123}

    def test_failed_recovery_is_retried_a_bounded_number_of_times(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)),              patch.object(watch,'validate_full_dump',return_value={}) as validate,              patch.object(watch,'restart_taskmgr',side_effect=OSError('busy')) as restart:
            monitor=watch.Monitor()
            monitor.recover(Path(directory),self.recovery_evidence())
            self.assertIsNotNone(monitor.retry)
            for _ in range(watch.RETRY_LIMIT+2):
                monitor.retry_recovery(watch.time.monotonic()+watch.RETRY_INTERVAL+1)
                if monitor.dump_thread:
                    monitor.dump_thread.join(timeout=3)
            self.assertEqual(restart.call_count,watch.RETRY_LIMIT+1)
            self.assertEqual(validate.call_count,2*(watch.RETRY_LIMIT+1))
            self.assertIsNone(monitor.retry)
            evidence=json.loads((Path(directory)/'event.json').read_text(encoding='utf-8'))
            self.assertTrue(evidence['recovery']['retry_exhausted'])

    def test_missing_cpu_page_retries_navigation_only(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)),              patch.object(watch,'validate_full_dump',return_value={}),              patch.object(watch,'restart_taskmgr',return_value={'status':'restarted_waiting_for_cpu_page','launch_pid':9876}) as restart,              patch.object(watch,'restore_cpu_page',return_value={'status':'restarted','new_pid':9876}) as navigate:
            monitor=watch.Monitor()
            monitor.recover(Path(directory),self.recovery_evidence())
            monitor.retry_recovery(watch.time.monotonic())
            navigate.assert_not_called()
            monitor.retry_recovery(watch.time.monotonic()+watch.RETRY_INTERVAL+1)
            monitor.dump_thread.join(timeout=3)
            restart.assert_called_once()
            navigate.assert_called_once_with(9876)
            self.assertEqual(monitor.capture_result,'recovered')
            self.assertIsNone(monitor.retry)

    def test_restart_only_restarts_without_graphs_or_dumps(self):
        with tempfile.TemporaryDirectory() as directory,              patch.object(watch,'OUTPUT',Path(directory)),              patch.object(watch.time,'monotonic',return_value=1000) as clock,              patch.object(watch,'write_dump') as dump,              patch.object(watch,'validate_full_dump') as validate,              patch.object(watch,'storage_available',return_value=False),              patch.object(watch,'restart_taskmgr',return_value={'status':'restarted','new_pid':9876}) as restart:
            monitor=watch.Monitor(restart_only=True)
            for t in range(0,34,2):
                clock.return_value=1000+t
                monitor.accept({'state':'captured','pid':4321,'hwnd':123,'geometry':[[0,0,75,50]]*33,
                                'total':graph(),'cores':graph(15+t%8,wave=True)})
            monitor.dump_thread.join(timeout=3)
            restart.assert_called_once_with(4321,123)
            dump.assert_not_called();validate.assert_not_called()
            event=Path(monitor.last_event)
            self.assertTrue(event.name.startswith('restart-'))
            self.assertEqual(sorted(p.name for p in event.iterdir()),['event.json'])
            record=json.loads((event/'event.json').read_text(encoding='utf-8'))
            self.assertEqual(record['classification'],'restart_only')
            self.assertEqual(record['recovery']['status'],'restarted')
            self.assertEqual(monitor.capture_result,'recovered')
            state=json.loads((Path(directory)/'status.json').read_text(encoding='utf-8'))
            self.assertEqual(state['mode'],'restart_only')
            monitor.stop()

    def test_restart_only_uses_short_same_target_cooldown(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)),              patch.object(watch.time,'monotonic',return_value=1000),              patch.object(watch.threading,'Thread') as thread:
            monitor=watch.Monitor(restart_only=True)
            monitor.last_capture_target=(4321,123)
            monitor.last_capture=1000-30
            monitor.trigger({'pid':4321,'hwnd':123,'geometry':[]})
            thread.assert_not_called()
            monitor.last_capture=1000-watch.RESTART_COOLDOWN-1
            monitor.trigger({'pid':4321,'hwnd':123,'geometry':[]})
            thread.return_value.start.assert_called_once()
            self.assertTrue(Path(monitor.last_event).name.startswith('restart-'))

    def test_failed_restart_only_is_retried_without_dump_checks(self):
        record={'classification':'restart_only','status':'complete','taskmgr_pid':4321,'window':123}
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)),              patch.object(watch,'validate_full_dump') as validate,              patch.object(watch,'restart_taskmgr',side_effect=[OSError('busy'),{'status':'restarted','new_pid':9876}]) as restart:
            monitor=watch.Monitor(restart_only=True)
            monitor.recover(Path(directory),record)
            self.assertEqual(monitor.capture_result,'recovery_failed')
            monitor.retry_recovery(watch.time.monotonic()+watch.RETRY_INTERVAL+1)
            monitor.dump_thread.join(timeout=3)
            self.assertEqual(restart.call_count,2)
            validate.assert_not_called()
            self.assertEqual(monitor.capture_result,'recovered')

class OriginCaptureTests(unittest.TestCase):
    class FakeProcess:
        def __init__(self, output, code=0):
            self.stdout=io.BytesIO(output);self.returncode=code;self.pid=1
        def wait(self):return self.returncode

    def test_matching_debug_string_dump_is_validated_and_recorded(self):
        import OriginCapture
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);(folder/'Taskmgr.exe_261001_040000.dmp').write_bytes(b'x')
            output=('[04:00:00]Debug String: \nX 80070005\n[04:00:01]Debug String: \nY 8007139F\n').encode('utf-16-le')
            origin=OriginCapture.OriginWatch(Path(directory)/'procdump.exe',folder,lambda p:{'ok':True},watch.write_json)
            record={'debug_strings':0}
            with patch.object(origin,'extract_stacks') as stacks:
                origin.follow(42,self.FakeProcess(output),folder,record)
            self.assertEqual(record['status'],'captured')
            self.assertEqual(record['debug_strings'],2)
            stacks.assert_called_once()
            self.assertIn('8007139F',(folder/'procdump.txt').read_text(encoding='utf-8'))

    def test_no_match_keeps_summary_without_dump(self):
        import OriginCapture
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            origin=OriginCapture.OriginWatch(folder/'procdump.exe',folder,lambda p:{},watch.write_json)
            record={'debug_strings':0}
            origin.follow(42,self.FakeProcess(b'Process exited'),folder,record)
            self.assertEqual(record['status'],'no_match')

    def test_stop_detaches_with_cancel_and_never_terminates(self):
        import OriginCapture
        with tempfile.TemporaryDirectory() as directory, patch.object(OriginCapture.subprocess,'run') as run:
            origin=OriginCapture.OriginWatch(Path(directory)/'procdump.exe',Path(directory),lambda p:{},watch.write_json)
            process=unittest.mock.Mock()
            origin.sessions[42]=(process,Path(directory),{})
            origin.attempted.add(42)
            with patch.object(OriginCapture.time,'monotonic',side_effect=[0,0,100]):
                origin.cancel_all()
            self.assertEqual(run.call_args[0][0][1:],['-cancel','42'])
            process.terminate.assert_not_called();process.kill.assert_not_called()
            self.assertNotIn(42,origin.attempted)

    def test_each_taskmgr_instance_is_attached_once(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)),              patch.object(watch,'require_taskmgr'), patch.object(watch,'storage_available',return_value=True):
            monitor=watch.Monitor()
            with patch.object(monitor.origin,'attach',side_effect=lambda pid:monitor.origin.attempted.add(pid)) as attach:
                monitor.watch_origin(10);monitor.watch_origin(10);monitor.watch_origin(11)
            self.assertEqual([c.args[0] for c in attach.call_args_list],[10,11])

    def test_restart_only_never_attaches_and_detaches_existing(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch,'OUTPUT',Path(directory)),              patch.object(watch,'require_taskmgr'), patch.object(watch,'storage_available',return_value=True):
            monitor=watch.Monitor(restart_only=True)
            with patch.object(monitor.origin,'attach') as attach:
                monitor.watch_origin(10)
            attach.assert_not_called()
            monitor.restart_only=False
            with patch.object(monitor.origin,'cancel_all') as cancel, patch.object(watch.threading,'Thread') as thread:
                monitor.set_restart_only(True)
                self.assertIs(thread.call_args.kwargs['target'],cancel)
                thread.return_value.start.assert_called_once()

if __name__=='__main__':unittest.main()
