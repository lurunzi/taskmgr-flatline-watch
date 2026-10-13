import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import IncidentAnalysis as A


class AnalysisTests(unittest.TestCase):
    def event(self,kind,t,pid,created=None,parent=0):
        return dict(kind=kind,qpc=t,pid=pid,create_time=created,sequence=None,parent_pid=parent,image=f'{pid}.exe')

    def header(self):
        return dict(raw_start=0,StartTime=0,PerfFreq=1000,EventsLost=0,BuffersLost=0)

    def fixture(self,d):
        folder=Path(d);trace=folder/'process-trace';trace.mkdir()
        (trace/'baseline.json').write_text('[]')
        (trace/'session.json').write_text(json.dumps(dict(status='stopped',decode_exit=0,qpc_frequency=1000,baseline_end_qpc=0,stopped_qpc=9000)))
        observation=dict(qpc_frequency=1000,watcher_pid=99,markers=dict(last_normal={'qpc':4000},first_candidate={'qpc':5000},confirmed={'qpc':8000}),
                         samples=[dict(qpc=t,state='normal' if t<=4000 else 'candidate') for t in range(1000,9000,1000)])
        (folder/'observation.json').write_text(json.dumps(observation))
        return folder

    def test_pid_reuse_keeps_instances_and_parent_lifetimes_separate(self):
        events=[self.event('process_start',100,7,1),self.event('process_start',150,42,2,7),
                self.event('process_stop',200,42,2,7),self.event('process_stop',300,7,1),
                self.event('process_start',400,7,3),self.event('process_start',450,42,4,7),
                self.event('thread_start',500,42)]
        _,resolve=A.process_instances(events,[],self.header())
        self.assertNotEqual(events[1]['instance']['key'],events[4]['instance']['key'])
        self.assertEqual(resolve(7,150)['created'],1)
        self.assertEqual(resolve(7,450)['created'],3)
        self.assertEqual(events[-1]['instance']['created'],4)

    def test_overlapping_lifetimes_are_unknown(self):
        events=[self.event('process_start',100,7,1),self.event('process_start',150,7,2),self.event('thread_start',200,7)]
        A.process_instances(events,[],self.header())
        self.assertIsNone(events[-1]['instance'])

    def test_nearby_creation_clocks_never_merge_by_rounded_time(self):
        header=self.header()
        created=A._decoder.ticks('2026-10-13T12:00:00.123456Z')
        header.update(StartTime=created)
        baseline=[{'ProcessId':42,'ParentProcessId':1,'CreateTime':'2026-10-13T12:00:00.123456Z','Name':'old.exe'}]
        events=[self.event('process_start',100,42,created+7),self.event('thread_start',200,42)]
        instances,_=A.process_instances(events,baseline,header)
        self.assertEqual(len(instances[42]),2)
        self.assertIsNone(events[-1]['instance'])

    def test_ranks_parent_process_creators_and_keeps_unknown_counts(self):
        with tempfile.TemporaryDirectory() as d:
            folder=self.fixture(d)
            events=[self.event('process_start',1000,10,1),self.event('process_start',6000,42,2,10),
                    self.event('process_start',6100,43,3,10),self.event('thread_start',6500,999)]
            with patch.object(A._decoder,'decode',return_value=(events,self.header(),[])):
                r=A.analyze(folder)
            self.assertEqual(r['top'][0]['pid'],10)
            self.assertEqual(r['top'][0]['child_process_starts'],2)
            self.assertEqual(r['unresolved_counts']['thread_start'],1)
            self.assertEqual(r['normal_exposure_seconds'],3)
            self.assertEqual(r['attribution'],'unproven')
            self.assertTrue((folder/'incident-analysis.md').exists())

    def test_event_loss_suppresses_named_ranking(self):
        with tempfile.TemporaryDirectory() as d:
            folder=self.fixture(d)
            with patch.object(A._decoder,'decode',return_value=([],self.header(),['ETW lost events or buffers'])):
                r=A.analyze(folder)
            self.assertFalse(r['quality']['ranking_usable']);self.assertEqual(r['top'],[])

    def test_overwrite_of_old_history_is_allowed_but_not_missing_required_window(self):
        for first,usable in ((2000,True),(5000,False)):
            with self.subTest(first=first),tempfile.TemporaryDirectory() as d:
                folder=self.fixture(d)
                events=[self.event('process_start',first,42,2),self.event('thread_start',6000,42)]
                with patch.object(A._decoder,'decode',return_value=(events,self.header(),['circular trace may have overwritten earlier buffers'])):
                    r=A.analyze(folder)
                self.assertEqual(r['quality']['ranking_usable'],usable)
                self.assertFalse(r['quality']['baseline_usable'])

    def test_missing_control_is_not_fabricated(self):
        with tempfile.TemporaryDirectory() as d:
            folder=self.fixture(d);p=folder/'observation.json';o=json.loads(p.read_text())
            o['markers']['last_normal']=None;p.write_text(json.dumps(o))
            with patch.object(A._decoder,'decode',return_value=([],self.header(),[])):
                r=A.analyze(folder)
            self.assertIsNone(r['normal_window_qpc']);self.assertIsNone(r['normal_exposure_seconds'])
            self.assertTrue(r['quality']['missing_pre_candidate_normal'])

    def test_fresh_post_stop_snapshot_restores_surviving_parent_after_wrap(self):
        with tempfile.TemporaryDirectory() as d:
            folder=self.fixture(d);trace=folder/'process-trace'
            path=trace/'session.json';session=json.loads(path.read_text())
            session.update(end_baseline_begin_qpc=9100,end_baseline_end_qpc=9200);path.write_text(json.dumps(session))
            created=A._decoder.ticks('2026-10-13T12:00:00Z')
            (trace/'end-baseline.json').write_text(json.dumps([dict(ProcessId=10,ParentProcessId=0,Name='parent.exe',CreateTime='2026-10-13T12:00:00Z')]))
            h=self.header();h['StartTime']=created
            events=[self.event('thread_start',2000,10),self.event('process_start',6000,42,created+60_000_000,10)]
            with patch.object(A._decoder,'decode',return_value=(events,h,['circular trace may have overwritten earlier buffers'])):
                r=A.analyze(folder)
            self.assertTrue(r['quality']['ranking_usable'])
            self.assertTrue(r['quality']['end_baseline_available'])
            self.assertEqual(r['top'][0]['pid'],10)
            self.assertEqual(r['top'][0]['child_process_starts'],1)

    def test_clock_mismatch_or_marker_order_blocks_ranking(self):
        for frequency,confirmed in ((999,8000),(1000,3000)):
            with self.subTest(frequency=frequency),tempfile.TemporaryDirectory() as d:
                folder=self.fixture(d);p=folder/'observation.json';o=json.loads(p.read_text())
                o['qpc_frequency']=frequency;o['markers']['confirmed']['qpc']=confirmed;p.write_text(json.dumps(o))
                with patch.object(A._decoder,'decode',return_value=([],self.header(),[])):
                    r=A.analyze(folder)
                self.assertFalse(r['quality']['ranking_usable'])


if __name__=='__main__':unittest.main()
