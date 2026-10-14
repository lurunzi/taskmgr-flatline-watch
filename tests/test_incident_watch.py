import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import AutomaticWatch as W
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

    def test_busy_decoder_defers_without_losing_raw_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            p=OfflineProcessor();p.thread=Mock();p.thread.is_alive.return_value=True
            self.assertFalse(p.submit(Path(d),Mock()))
            self.assertEqual(json.loads((Path(d)/'analysis-status.json').read_text())['status'],'deferred')

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
