import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from FlatlineDetection import FlatlineDetector

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

if __name__=='__main__':unittest.main()
