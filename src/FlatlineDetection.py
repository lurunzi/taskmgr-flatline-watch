"""Visual suspicion detector; a flat plot alone does not prove a CPU telemetry bug."""
from collections import deque

import cv2
import numpy as np


def blue_mask(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (78, 45, 45), (115, 255, 255)) > 0


def curve_tail(image):
    if image is None or image.shape[0] < 12 or image.shape[1] < 25:
        return None
    mask = blue_mask(image[2:-2, 2:-2])
    # The newest portion is at the right; discard the border and grid.
    tail = mask[:, -max(8, int(mask.shape[1] * .2)):]
    present = tail.any(axis=0)
    if present.mean() < .85:
        return None
    heights = np.argmax(tail[:, present], axis=0)
    return float(np.median(heights)), float(np.ptp(heights))


def empty_chart(image):
    """Recognise a painted, empty neutral chart, not an unpainted bitmap.

    Require the native frame on all four sides and a uniform interior. This
    deliberately rejects borderless blank captures and unsupported themes.
    """
    if image is None or image.shape[0] < 12 or image.shape[1] < 25:
        return False
    inside = image[2:-2, 2:-2].astype(np.int16)
    background = np.median(inside, axis=(0, 1))
    if np.ptp(background) > 8 or np.mean(np.max(abs(inside-background), axis=2) <= 2) < .995:
        return False
    contrast = np.max(abs(image.astype(np.int16)-background), axis=2) >= 12
    return all(float(edge.mean()) >= .8 for edge in (
        contrast[:2, 2:-2].any(axis=0), contrast[-2:, 2:-2].any(axis=0),
        contrast[2:-2, :2].any(axis=1), contrast[2:-2, -2:].any(axis=1)))


class FlatlineDetector:
    def __init__(self, seconds=30):
        self.seconds = seconds
        self.reset()

    def reset(self):
        self.last_time = None
        self.last_core = None
        self.anchor = None
        self.since = None
        self.changes = deque()
        self.latched = False
        self.recovery_since = None
        self.kind = None

    def observe(self, now, aggregate, cores, system_cpu_percent=None):
        if self.last_time is not None and (now <= self.last_time or now-self.last_time > 6):
            self.reset()
        previous_time = self.last_time
        self.last_time = now
        tail = curve_tail(aggregate)
        core = blue_mask(cores)
        # Zero plots may have no blue pixels. Only admit them when a painted
        # frame and independent, current system CPU activity corroborate them.
        empty = tail is None and empty_chart(aggregate)
        if empty and system_cpu_percent is not None and np.isfinite(system_cpu_percent) and 2 <= system_cpu_percent <= 100:
            tail = (float(aggregate.shape[0]-4), 0.0)
        if tail is None or core.mean() < .005:
            self.reset()
            return {'state': 'invalid', 'seconds': 0, 'alert': False,
                    'reason': 'empty_unconfirmed' if empty else 'unrecognised_chart',
                    'system_cpu_percent': system_cpu_percent}
        kind = 'empty_aggregate' if empty else 'flat_curve'
        if self.kind is not None and kind != self.kind:
            self.reset()
            self.last_time = now
            previous_time = None
        self.kind = kind
        change = 0.0
        if self.last_core is not None and self.last_core.shape == core.shape:
            change = float(np.mean(self.last_core != core))
        self.last_core = core.copy()
        if change > .003 and previous_time is not None:
            self.changes.append(now)
        while self.changes and self.changes[0] < now-self.seconds:
            self.changes.popleft()
        height, spread = tail
        # A one-pixel change is real activity on the small CPU thumbnail.
        # Admitting it turns normal low-load waves into a freeze candidate.
        flat = spread == 0.0
        same_level = self.anchor is None or height == self.anchor
        if not flat or not same_level:
            self.since = None
            self.anchor = None
            if self.recovery_since is None:
                self.recovery_since = now
            if now-self.recovery_since >= 10:
                self.latched = False
            return {'state': 'normal', 'seconds': 0, 'alert': False, 'core_change': change}
        self.recovery_since = None
        if self.since is None:
            self.since, self.anchor = now, height
        elapsed = now-self.since
        alive = len(self.changes) >= 3 and now-self.changes[-1] <= 6
        suspicious = elapsed >= self.seconds and alive
        alert = suspicious and not self.latched
        if alert:
            self.latched = True
        return {'state': 'suspect' if suspicious else ('candidate' if alive else 'no_motion'),
                'seconds': round(elapsed, 1), 'alert': alert,
                'detector_kind': kind, 'system_cpu_percent': system_cpu_percent,
                'core_change': change, 'tail_spread': spread, 'tail_height': height}
