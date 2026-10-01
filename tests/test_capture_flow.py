import ctypes
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import AutomaticWatch as watch
import NativeCapture
from test_detection import graph

class CaptureFlowTests(unittest.TestCase):
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

if __name__=='__main__':unittest.main()
