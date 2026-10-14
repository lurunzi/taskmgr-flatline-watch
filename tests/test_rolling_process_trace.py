import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from RollingProcessTrace import RollingRecorder
from ProcessTrace import frequency


class FakeSession:
    instances=[]
    def __init__(self,folder,**kwargs):
        self.folder=Path(folder)/'process-trace';self.folder.mkdir()
        self.record={'qpc_frequency':frequency()};self.name=str(len(self.instances));self.active=False
        self.instances.append(self)
    def save(self,**values):self.record.update(values)
    def start(self):
        self.active=True;(self.folder/'process.etl').write_bytes(b'retained raw trace')
    def stop(self):self.active=False
    def baseline(self,final=False):pass


class RollingTests(unittest.TestCase):
    def setUp(self):FakeSession.instances=[]
    def wait(self,fn):
        end=time.monotonic()+3
        while not fn() and time.monotonic()<end:time.sleep(.01)
        self.assertTrue(fn())

    def test_snapshot_preserves_bytes_and_rearms_without_decoding(self):
        with tempfile.TemporaryDirectory() as d:
            recorder=RollingRecorder(d,FakeSession)
            self.addCleanup(recorder.stop)
            recorder.start();self.wait(lambda:recorder.status()['state']=='running')
            incident=Path(d)/'restart-test';incident.mkdir()
            ready=recorder.snapshot(incident)
            self.assertTrue(ready.wait(3))
            self.assertEqual((incident/'process-trace/process.etl').read_bytes(),b'retained raw trace')
            self.wait(lambda:len(FakeSession.instances)==2 and recorder.status()['state']=='running')
            self.assertFalse(FakeSession.instances[0].active)
            self.assertTrue(FakeSession.instances[1].active)
            recorder.stop();self.assertFalse(FakeSession.instances[1].active)
            self.assertEqual(json.loads((incident/'trace-capture.json').read_text())['status'],'preserved')

    def test_start_failure_cleans_only_its_owned_session_and_snapshot_fails_open(self):
        class Failing(FakeSession):
            def start(self):self.active=True;raise RuntimeError('baseline failed')
        with tempfile.TemporaryDirectory() as d:
            recorder=RollingRecorder(d,Failing);recorder.start()
            self.wait(lambda:recorder.thread is not None and not recorder.thread.is_alive())
            self.assertFalse(FakeSession.instances[0].active)
            self.assertEqual(recorder.status()['state'],'error')
            ready=recorder.snapshot(d)
            self.assertTrue(ready.is_set())
            self.assertEqual(json.loads((Path(d)/'trace-capture.json').read_text())['status'],'unavailable')

    def test_stop_during_snapshot_never_rearms(self):
        entered,release=threading.Event(),threading.Event()
        class Blocking(FakeSession):
            def stop(self):entered.set();release.wait(3);super().stop()
        with tempfile.TemporaryDirectory() as d:
            recorder=RollingRecorder(d,Blocking)
            recorder.start();self.wait(lambda:recorder.status()['state']=='running')
            incident=Path(d)/'restart-test';incident.mkdir()
            ready=recorder.snapshot(incident);self.assertTrue(entered.wait(3))
            recorder.stop(timeout=0);release.set();recorder.stop()
            self.assertTrue(ready.is_set())
            self.assertEqual(len(FakeSession.instances),1)
            self.assertFalse(FakeSession.instances[0].active)

    def test_storage_limit_preserves_existing_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            artifact=Path(d)/'old.etl';artifact.write_bytes(b'important')
            recorder=RollingRecorder(d,FakeSession)
            with patch.object(recorder,'_storage',side_effect=RuntimeError('ETW storage budget reached')):
                recorder.start();recorder.thread.join(3)
            self.assertEqual(recorder.status()['state'],'error')
            self.assertEqual(artifact.read_bytes(),b'important')
            self.assertEqual(FakeSession.instances,[])

    def prior(self,d):
        path=Path(d)/'session.json'
        previous=dict(session='TaskmgrCorrelation-'+'a'*32,status='running',owner_role='flatline watcher',
                      created_utc='2026-10-13T12:00:00+00:00')
        path.write_text(json.dumps(previous),encoding='utf-8')
        return path,previous,datetime(2026,10,14,tzinfo=timezone.utc)

    def test_only_verified_absent_previous_boot_session_is_reconciled(self):
        with tempfile.TemporaryDirectory() as d:
            recorder=RollingRecorder(d,FakeSession);p,prior,boot=self.prior(d);original=p.read_bytes()
            with patch('RollingProcessTrace.session_exists',return_value=False) as query:
                self.assertTrue(recorder._resolve_previous_boot(p,prior,boot))
            query.assert_called_once_with(prior['session'])
            self.assertEqual(p.read_bytes(),original)
            self.assertEqual(json.loads((p.parent/'ownership-resolution.json').read_text())['status'],'absent_after_reboot')

    def test_active_or_unknown_session_is_never_claimed_stale(self):
        with tempfile.TemporaryDirectory() as d:
            recorder=RollingRecorder(d,FakeSession);p,prior,boot=self.prior(d)
            with patch('RollingProcessTrace.session_exists',return_value=True):
                self.assertFalse(recorder._resolve_previous_boot(p,prior,boot))
            with patch('RollingProcessTrace.session_exists',side_effect=OSError('access denied')):
                with self.assertRaises(OSError):recorder._resolve_previous_boot(p,prior,boot)
            self.assertFalse((p.parent/'ownership-resolution.json').exists())

    def test_same_boot_owner_is_not_bypassed_even_when_session_absent(self):
        with tempfile.TemporaryDirectory() as d:
            recorder=RollingRecorder(d,FakeSession);p,prior,boot=self.prior(d)
            prior['created_utc']='2026-10-14T01:00:00+00:00'
            with patch('RollingProcessTrace.session_exists') as query:
                self.assertFalse(recorder._resolve_previous_boot(p,prior,boot))
                query.assert_not_called()


if __name__=='__main__':unittest.main()
