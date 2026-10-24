from __future__ import annotations

from collections import deque
from pathlib import Path
import sys
import threading

import pytest

PATROL = Path(__file__).resolve().parents[1] / "patrol"
sys.path.insert(0, str(PATROL))

from locomotion_relay import odom_health, wait_for_continuous_odom


class Clock:
    def __init__(self, odom, *, publish=True, freeze_after=None):
        self.now = 0.0
        self.odom = odom
        self.publish = publish
        self.freeze_after = freeze_after

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if (self.publish and (self.freeze_after is None
                              or self.now <= self.freeze_after)):
            self.odom.update(
                x=1.0, y=2.0, yaw=0.1, received=self.now,
                count=self.odom["count"] + 1,
            )
            self.odom["arrivals"].append(self.now)


def state():
    return {
        "x": None, "y": None, "yaw": None, "received": None,
        "count": 0, "started": 0.0, "arrivals": deque(maxlen=500),
    }


def test_continuous_ten_hz_odom_passes_only_after_three_seconds():
    odom = state()
    clock = Clock(odom)
    result = wait_for_continuous_odom(
        odom, threading.Lock(), 4.0, clock=clock, sleep=clock.sleep
    )
    assert result is not None
    assert result["continuous_s"] >= 3.0
    assert result["age"] == pytest.approx(0.0)
    assert result["rate_hz"] >= 5.0


def test_one_odom_packet_then_frozen_never_becomes_ready():
    odom = state()
    clock = Clock(odom, freeze_after=0.02)
    result = wait_for_continuous_odom(
        odom, threading.Lock(), 4.0, clock=clock, sleep=clock.sleep
    )
    assert result is None
    assert odom_health(odom, threading.Lock(), now=clock.now)["ready"] is False


def test_existing_stale_odom_is_not_healthy():
    odom = state()
    odom.update(x=1.0, y=2.0, yaw=0.1, received=1.0, count=1)
    odom["arrivals"].append(1.0)
    result = odom_health(odom, threading.Lock(), now=124.31)
    assert result["ready"] is False
    assert result["age"] == pytest.approx(123.31)
    assert result["rate_hz"] == 0.0


def test_nonfinite_odom_is_not_healthy_even_when_fresh():
    odom = state()
    odom.update(x=float("nan"), y=0.0, yaw=0.0, received=1.0, count=10)
    odom["arrivals"].extend([0.8, 0.9, 1.0])
    result = odom_health(odom, threading.Lock(), now=1.0)
    assert result["ready"] is False
    assert result["finite"] is False
