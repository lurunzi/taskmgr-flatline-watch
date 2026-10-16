import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from FlatlineDetection import FlatlineDetector, empty_chart

def empty_graph(dark=False):
    image = np.full((50,75,3),32 if dark else 255,np.uint8)
    image[0,:,:] = image[-1,:,:] = 128
    image[:,0,:] = image[:,-1,:] = 128
    return image

def graph(level=20, wave=False):
    image=np.full((50,75,3),255,np.uint8)
    heights=[level+(int(6*np.sin(x*.6)) if wave else 0) for x in range(75)]
    for x,y in enumerate(heights):
        image[y:,x]=(240,220,180)
        image[y,x]=(190,135,0)
    return image

class DetectionTests(unittest.TestCase):
    def sequence(self, detector, aggregate, moving=True, start=0, end=42):
        results=[]
        for t in range(start,end,2):
            cores=graph(15+(t%8) if moving else 15,wave=True)
            results.append(detector.observe(t,aggregate(t),cores))
        return results

    def test_flat_with_live_cores_triggers_once_after_30_seconds(self):
        results=self.sequence(FlatlineDetector(),lambda t:graph())
        self.assertEqual([i*2 for i,r in enumerate(results) if r['alert']],[30])

    def test_whole_page_frozen_does_not_trigger(self):
        self.assertFalse(any(r['alert'] for r in self.sequence(FlatlineDetector(),lambda t:graph(),False)))

    def test_normal_wave_does_not_trigger(self):
        self.assertFalse(any(r['alert'] for r in self.sequence(FlatlineDetector(),lambda t:graph(wave=True))))

    def test_flat_but_changing_level_does_not_trigger(self):
        self.assertFalse(any(r['alert'] for r in self.sequence(FlatlineDetector(),lambda t:graph(10+t%12))))

    def test_capture_gap_resets_confirmation(self):
        detector=FlatlineDetector()
        self.sequence(detector,lambda t:graph(),end=28)
        result=detector.observe(50,graph(),graph(wave=True))
        self.assertFalse(result['alert']);self.assertEqual(result['seconds'],0)

    def test_blank_capture_is_invalid(self):
        r=FlatlineDetector().observe(0,np.full((50,75,3),255,np.uint8),graph())
        self.assertEqual(r['state'],'invalid');self.assertFalse(r['alert'])

    def test_recovery_rearms(self):
        detector=FlatlineDetector()
        self.sequence(detector,lambda t:graph())
        self.sequence(detector,lambda t:graph(wave=True),start=42,end=56)
        results=self.sequence(detector,lambda t:graph(),start=56,end=96)
        self.assertEqual(sum(r['alert'] for r in results),1)

    def empty_sequence(self, cpu=8, moving=True, dark=False):
        detector = FlatlineDetector()
        return [detector.observe(t,empty_graph(dark),graph(15+t%8 if moving else 15,wave=True),cpu)
                for t in range(0,42,2)]

    def test_framed_empty_aggregate_with_busy_cpu_and_live_cores_alerts(self):
        for dark in (False, True):
            results = self.empty_sequence(dark=dark)
            self.assertEqual([i*2 for i,r in enumerate(results) if r['alert']],[30])
            self.assertEqual(results[-1]['detector_kind'],'empty_aggregate')

    def test_empty_aggregate_requires_independent_current_activity(self):
        for cpu in (None,0,.49,1.99,float('nan'),101):
            results = self.empty_sequence(cpu=cpu)
            self.assertTrue(all(r['state']=='invalid' and not r['alert'] for r in results))

    def test_empty_aggregate_with_static_cores_does_not_trigger(self):
        self.assertFalse(any(r['alert'] for r in self.empty_sequence(moving=False)))

    def test_unpainted_or_partial_frames_rejected_even_with_cpu_activity(self):
        for background in (0,32,255):
            image = np.full((50,75,3),background,np.uint8)
            self.assertFalse(empty_chart(image))
            r=FlatlineDetector().observe(0,image,graph(),8)
            self.assertEqual(r['state'],'invalid')
        image=empty_graph();image[:,0,:]=255
        self.assertFalse(empty_chart(image))

    def test_empty_confirmation_restarts_after_missing_activity_or_capture_gap(self):
        for disruption in ('activity','gap'):
            detector=FlatlineDetector()
            for t in range(0,28,2):
                detector.observe(t,empty_graph(),graph(15+t%8,wave=True),8)
            if disruption=='activity':
                detector.observe(28,empty_graph(),graph(wave=True),None)
                now=30
            else:
                now=40
            result=detector.observe(now,empty_graph(),graph(wave=True),8)
            self.assertEqual(result['seconds'],0)
            self.assertFalse(result['alert'])

    def test_curve_to_empty_requires_new_confirmation(self):
        detector=FlatlineDetector()
        self.sequence(detector,lambda t:graph(),end=28)
        result=detector.observe(28,empty_graph(),graph(wave=True),8)
        self.assertEqual(result['seconds'],0)
        self.assertFalse(result['alert'])

if __name__=='__main__':unittest.main()
