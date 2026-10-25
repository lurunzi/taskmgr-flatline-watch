import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import AutomaticWatch as W
import IncidentWatch as IW
from IncidentWatch import Observations, OfflineProcessor


class IncidentWatchTests(unittest.TestCase):
    def frame(self,t,state):
        return {'qpc':t*1000,'monotonic':t,'timestamp':f't{t}','result':{'state':state},'images':{}}

    def test_visual_markers_reset_after_invalid_or_gap(self):
        o=Observations()
        o.observe(self.frame(1,'normal'));o.observe(self.frame(3,'candidate'));o.observe(self.frame(5,'suspect'))
        self.assertEqual(o.last_normal['qpc'],1000)
        self.assertEqual(o.first_candidate['qpc'],3000)
        self.assertEqual(o.confirmed['qpc'],5000)
        o.observe(self.frame(20,'candidate'))
        self.assertIsNone(o.last_normal);self.assertIsNone(o.confirmed)
        self.assertEqual(o.first_candidate['qpc'],20000)
        o.observe(self.frame(22,'invalid'))
        self.assertIsNone(o.first_candidate)

    def test_slow_snapshot_wait_is_bounded_and_restart_precedes_analysis(self):
        with tempfile.TemporaryDirectory() as d,patch.object(W,'OUTPUT',Path(d)), \
             patch.object(W,'RollingRecorder') as factory:
            monitor=W.Monitor(restart_only=True,light_etw=True)
            folder=Path(d)/'incident';folder.mkdir()
            ready=Mock();ready.wait.return_value=False
            factory.return_value.snapshot.return_value=ready
            calls=[]
            monitor.recover=Mock(side_effect=lambda *_:calls.append('restart'))
            monitor.offline=Mock();monitor.offline.submit.side_effect=lambda *_:calls.append('analysis')
            record={}
            monitor.restart_with_trace(folder,record,[],{'markers':{}})
            ready.wait.assert_called_once_with(2)
            self.assertFalse(record['light_etw']['snapshot_finished_before_recovery'])
            self.assertEqual(calls,['restart','analysis'])

    def test_snapshot_failure_does_not_block_recovery(self):
        with tempfile.TemporaryDirectory() as d,patch.object(W,'OUTPUT',Path(d)), \
             patch.object(W,'RollingRecorder') as factory:
            monitor=W.Monitor(restart_only=True,light_etw=True)
            factory.return_value.snapshot.side_effect=OSError('disk unavailable')
            monitor.recover=Mock();record={}
            monitor.restart_with_trace(Path(d),record,[],{'markers':{}})
            monitor.recover.assert_called_once()
            self.assertIn('disk unavailable',record['light_etw']['error'])

    def test_marker_failure_never_prevents_restart(self):
        with tempfile.TemporaryDirectory() as d,patch.object(W,'OUTPUT',Path(d)), \
             patch.object(W,'mark_recovery',side_effect=OSError('readonly')), \
             patch.object(W,'restart_taskmgr',return_value={'status':'restarted'}) as restart:
            monitor=W.Monitor(restart_only=True)
            evidence={'classification':'restart_only','status':'complete','taskmgr_pid':1,'window':2}
            monitor.recover(Path(d),evidence)
            restart.assert_called_once_with(1,2)
            self.assertEqual(len(evidence['observation_errors']),2)

    def test_disabled_mode_never_starts_collector(self):
        with tempfile.TemporaryDirectory() as d,patch.object(W,'OUTPUT',Path(d)),patch.object(W,'RollingRecorder') as factory:
            W.Monitor(restart_only=True)
            factory.assert_not_called()

    def test_light_recording_never_implicitly_enables_debugger_mode(self):
        with tempfile.TemporaryDirectory() as d,patch.object(W,'OUTPUT',Path(d)),patch.object(W,'RollingRecorder'):
            monitor=W.Monitor(query_trace=True,restart_only=False,light_etw=True)
            self.assertTrue(monitor.restart_only)
            monitor.set_restart_only(False)
            self.assertTrue(monitor.restart_only)

    def test_full_queue_defers_without_losing_raw_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            p=OfflineProcessor();p.queue=[{}]*IW.MAX_QUEUED
            self.assertFalse(p.submit(Path(d),Mock()))
            self.assertEqual(json.loads((Path(d)/'analysis-status.json').read_text())['status'],'deferred')

    def incident(self,d,name='restart-x'):
        folder=Path(d)/name;folder.mkdir()
        (folder/'trace-capture.json').write_text(json.dumps({'status':'preserved'}),encoding='utf-8')
        ready=Mock();ready.wait.return_value=True
        return folder,ready

    def analyse(self,p,folder,ready,results):
        with patch.object(IW.subprocess,'run',side_effect=results) as run:
            p.submit(folder,ready);p.thread.join(10)
        return run,json.loads((folder/'analysis-status.json').read_text())

    def test_timeout_is_retried_into_a_new_sibling_directory(self):
        with tempfile.TemporaryDirectory() as d:
            folder,ready=self.incident(d)
            p=OfflineProcessor(timeouts=(1,2,3),delays=(0,0))
            run,status=self.analyse(p,folder,ready,[subprocess.TimeoutExpired('x',1),Mock(returncode=0)])
            self.assertEqual(run.call_count,2)
            first,second=(c.args[0] for c in run.call_args_list)
            self.assertNotIn('--output',first)
            self.assertEqual(second[-2:],['--output',str(Path(d)/'analysis-retry2-restart-x')])
            self.assertEqual([c.kwargs['timeout'] for c in run.call_args_list],[1,2])
            self.assertEqual(status['status'],'complete')
            self.assertEqual(status['report'],str(Path(d)/'analysis-retry2-restart-x'/'incident-analysis.md'))
            self.assertEqual([a['status'] for a in status['attempts']],['failed','complete'])
            self.assertTrue((folder/'analysis.log').exists() and (folder/'analysis-attempt2.log').exists())

    def test_retries_are_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            folder,ready=self.incident(d)
            p=OfflineProcessor(timeouts=(1,1,1),delays=(0,0))
            run,status=self.analyse(p,folder,ready,[subprocess.TimeoutExpired('x',1)]*3)
            self.assertEqual(run.call_count,3)
            self.assertEqual((status['status'],status['attempt'],len(status['attempts'])),('failed',3,3))

    def test_unusable_data_is_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            folder,ready=self.incident(d)
            p=OfflineProcessor(timeouts=(1,1,1),delays=(0,0))
            run,status=self.analyse(p,folder,ready,[Mock(returncode=2)])
            self.assertEqual(run.call_count,1)
            self.assertEqual(status['status'],'incomplete')

    def test_new_incident_runs_before_a_delayed_retry_and_close_cancels_it(self):
        with tempfile.TemporaryDirectory() as d:
            old,ready=self.incident(d,'restart-old');new,_=self.incident(d,'restart-new')
            p=OfflineProcessor(timeouts=(1,1,1),delays=(3600,3600))
            with patch.object(IW.subprocess,'run',side_effect=[subprocess.TimeoutExpired('x',1),Mock(returncode=0)]) as run:
                p.submit(old,ready)
                for _ in range(200):
                    if p.status().get('status')=='retry_pending':break
                    time.sleep(.02)
                p.submit(new,ready)
                for _ in range(200):
                    if p.status().get('folder')==str(new):break
                    time.sleep(.02)
                p.close();p.thread and p.thread.join(10)
            self.assertEqual([c.args[0][2] for c in run.call_args_list],[str(old),str(new)])
            self.assertEqual(json.loads((old/'analysis-status.json').read_text())['status'],'retry_pending')
            self.assertEqual(json.loads((new/'analysis-status.json').read_text())['status'],'complete')
            self.assertFalse(p.submit(Path(d),ready))

    def test_reset_keeps_disconnected_normal_and_reason_but_never_control(self):
        o=Observations();o.observe(self.frame(1,'normal'))
        o.reset('capture_state',{'state':'wrong_page'})
        o.observe(self.frame(10,'candidate'))
        record=o.record([],123,456)
        self.assertIsNone(record['markers']['last_normal'])
        self.assertEqual(record['continuity']['previous_normal_disconnected']['qpc'],1000)
        self.assertEqual(record['continuity']['recent_breaks'][-1]['detail']['state'],'wrong_page')
        for _ in range(50):o.reset('invalid')
        self.assertEqual(len(o.breaks),32)
        self.assertGreater(o.break_count,32)

    def test_capture_state_reset_is_recorded_by_monitor(self):
        with tempfile.TemporaryDirectory() as d,patch.object(W,'OUTPUT',Path(d)):
            monitor=W.Monitor(restart_only=True)
            monitor.accept({'state':'wrong_page'})
            self.assertEqual(monitor.observations.breaks[-1]['detail']['state'],'wrong_page')


if __name__=='__main__':unittest.main()
