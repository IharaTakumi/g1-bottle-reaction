from __future__ import annotations

import threading
import time
from pathlib import Path
import sys

import pytest

PATROL = Path(__file__).resolve().parents[1] / "patrol"
sys.path.insert(0, str(PATROL))

from lidar_guard import GuardState
from patrol_controller import (
    TRANSPORT_RECOVERY_S,
    PatrolConfig,
    PatrolController,
    PatrolState,
)


class ClearGuard:
    def __init__(self) -> None:
        self.value = GuardState.CLEAR

    def state(self, _direction: str) -> GuardState:
        return self.value


class SimulatedLocomotion:
    def __init__(self) -> None:
        self.x = 0.0
        self.yaw = 0.0
        self.moves: list[tuple[float, float]] = []
        self.stops = 0
        self.imu_stale = False
        self.lock = threading.Lock()

    def move(self, vx: float, vyaw: float) -> None:
        with self.lock:
            self.moves.append((vx, vyaw))
            self.x += vx * 0.1
            self.yaw += vyaw * 0.4
        time.sleep(.002)

    def stop(self) -> None:
        with self.lock:
            self.stops += 1

    def odom_sample(self):
        with self.lock:
            return {
                "odom_ready": True,
                "odom_x": self.x,
                "odom_y": 0.0,
                "odom_yaw": 0.0,
                "odom_age": 0.0,
                "transport_age": 0.0,
                "odom_rate_hz": 10.0,
            }

    def imu_sample(self):
        with self.lock:
            return {
                "imu_ready": not self.imu_stale,
                "yaw": self.yaw,
                "imu_age": 1.0 if self.imu_stale else 0.0,
                "transport_age": 0.0,
                "imu_rate_hz": 1000.0,
            }


class AdvancingClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SequencedTelemetry:
    def __init__(self, *, imu=(), odom=()):
        self.imu = list(imu)
        self.odom = list(odom)
        self.last_imu = self.imu[-1] if self.imu else None
        self.last_odom = self.odom[-1] if self.odom else None
        self.stops = 0

    def imu_sample(self):
        if self.imu:
            self.last_imu = self.imu.pop(0)
        return self.last_imu

    def odom_sample(self):
        if self.odom:
            self.last_odom = self.odom.pop(0)
        return self.last_odom

    def stop(self):
        self.stops += 1


class ForwardTransportGap(SimulatedLocomotion):
    def __init__(self):
        super().__init__()
        self.gap_remaining = 0
        self.gap_injected = False

    def odom_sample(self):
        sample = super().odom_sample()
        if self.x >= .25 and not self.gap_injected:
            self.gap_injected = True
            self.gap_remaining = 25
        if self.gap_remaining:
            self.gap_remaining -= 1
            sample["transport_age"] = .25
        return sample


class TurnTransportGap(SimulatedLocomotion):
    def __init__(self):
        super().__init__()
        self.gap_remaining = 0
        self.gap_injected = False

    def imu_sample(self):
        sample = super().imu_sample()
        if len(self.moves) >= 2 and not self.gap_injected:
            self.gap_injected = True
            self.gap_remaining = 25
        if self.gap_remaining:
            self.gap_remaining -= 1
            sample["transport_age"] = .25
        return sample


def imu_sample(*, ready=True, age=0.0, transport=0.0):
    return {
        "imu_ready": ready, "yaw": 0.2, "imu_age": age,
        "transport_age": transport, "imu_rate_hz": 1000.0,
    }


def odom_sample(*, ready=True, age=0.0, transport=0.0):
    return {
        "odom_ready": ready, "odom_x": 1.0, "odom_y": 0.0,
        "odom_yaw": 0.2, "odom_age": age,
        "transport_age": transport, "odom_rate_hz": 10.0,
    }


def config() -> PatrolConfig:
    return PatrolConfig(
        forward_distance_m=1.0,
        return_distance_m=1.0,
        home_distance_m=1.0,
        forward_speed_m_s=.5,
        distance_tolerance_m=.01,
        command_period_s=.001,
        settle_s=0,
        turn_stop_at_rad=1.0,
        turn_slow_after_rad=.8,
        max_turn_duration_s=3,
    )


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.002)
    assert predicate()


def test_forward_pause_stops_and_resumes_same_odom_leg():
    locomotion = SimulatedLocomotion()
    controller = PatrolController(locomotion, ClearGuard(), config(), sleep=time.sleep)
    errors = []
    worker = threading.Thread(
        target=lambda: _capture(errors, controller._run_forward,
                                PatrolState.FORWARD_OUT, 1.0)
    )
    worker.start()
    wait_until(lambda: locomotion.x >= .25)

    paused = controller.request_reaction_pause("person", timeout=1)
    paused_x = locomotion.x
    move_count = len(locomotion.moves)
    assert paused["paused"] is True
    assert 0 < paused["remaining_m"] < 1
    time.sleep(.03)
    assert locomotion.x == pytest.approx(paused_x)
    assert len(locomotion.moves) == move_count

    controller.resume()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []
    assert .99 <= locomotion.x <= 1.05
    assert controller.last_forward_metrics["progress_m"] >= .99


def test_turn_detection_stops_immediately_and_resumes_remaining_angle():
    locomotion = SimulatedLocomotion()
    controller = PatrolController(locomotion, ClearGuard(), config(), sleep=time.sleep)
    errors = []

    def patrol_segment():
        try:
            controller._run_turn(PatrolState.TURN_BACK, 1.1)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=patrol_segment)
    worker.start()
    wait_until(lambda: locomotion.yaw >= .2)
    pause_result = []
    requester = threading.Thread(
        target=lambda: pause_result.append(
            controller.request_reaction_pause("banana", timeout=2)
        )
    )
    requester.start()
    requester.join(timeout=1)
    assert pause_result[0]["paused"] is True
    paused_yaw = locomotion.yaw
    move_count = len(locomotion.moves)
    time.sleep(.03)
    assert locomotion.yaw == pytest.approx(paused_yaw)
    assert len(locomotion.moves) == move_count
    assert any(vyaw != 0 for _, vyaw in locomotion.moves)
    assert 0 < controller.control_status()["turn_remaining_rad"] < 1.0
    controller.resume()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert 1.0 <= locomotion.yaw <= 1.25
    assert errors == []


def test_sensor_fault_while_paused_latches_final_stop():
    locomotion = SimulatedLocomotion()
    controller = PatrolController(locomotion, ClearGuard(), config(), sleep=time.sleep)
    errors = []
    worker = threading.Thread(
        target=lambda: _capture(errors, controller._run_forward,
                                PatrolState.FORWARD_OUT, 1.0)
    )
    worker.start()
    wait_until(lambda: locomotion.x >= .2)
    controller.request_reaction_pause("person", timeout=1)
    locomotion.imu_stale = True
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert "IMU telemetry stale" in str(errors[0])
    assert locomotion.stops > 0


def test_wait_for_imu_polls_past_cached_stale_until_fresh():
    clock = AdvancingClock()
    locomotion = SequencedTelemetry(
        imu=[imu_sample(age=1.0), imu_sample(transport=1.0), imu_sample()]
    )
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock, sleep=clock.sleep
    )
    result = controller._wait_for_imu()
    assert result["imu_age"] == 0.0
    assert clock.now == pytest.approx(.04)
    assert locomotion.stops == 0


def test_wait_for_imu_fails_only_after_two_seconds_of_stale_samples():
    clock = AdvancingClock()
    events = []
    locomotion = SequencedTelemetry(imu=[imu_sample(age=1.0)])
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock, sleep=clock.sleep,
        emit=events.append,
    )
    with pytest.raises(RuntimeError, match="within 2.0s"):
        controller._wait_for_imu()
    assert clock.now >= 2.0
    assert locomotion.stops == 1
    assert "imu_rate_hz=1000.0" in events[-1]


def test_wait_for_fresh_imu_returns_immediately():
    clock = AdvancingClock()
    locomotion = SequencedTelemetry(imu=[imu_sample()])
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock, sleep=clock.sleep
    )
    assert controller._wait_for_imu()["imu_ready"] is True
    assert clock.now == 0.0


def test_wait_for_odom_polls_stale_then_fresh_and_times_out_if_still_stale():
    clock = AdvancingClock()
    locomotion = SequencedTelemetry(
        odom=[odom_sample(ready=False), odom_sample(age=1.0), odom_sample()]
    )
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock, sleep=clock.sleep
    )
    assert controller._wait_for_odom()["odom_ready"] is True
    assert clock.now == pytest.approx(.04)

    timeout_clock = AdvancingClock()
    events = []
    stale = SequencedTelemetry(odom=[odom_sample(transport=1.0)])
    timeout_controller = PatrolController(
        stale, ClearGuard(), config(), clock=timeout_clock,
        sleep=timeout_clock.sleep, emit=events.append,
    )
    with pytest.raises(RuntimeError, match="within 2.0s"):
        timeout_controller._wait_for_odom()
    assert timeout_clock.now >= 2.0
    assert "odom_rate_hz=10.0" in events[-1]


def test_running_require_imu_still_fails_immediately_on_stale_sample():
    clock = AdvancingClock()
    events = []
    locomotion = SequencedTelemetry(imu=[imu_sample(age=.21), imu_sample()])
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock,
        sleep=clock.sleep, emit=events.append,
    )
    with pytest.raises(RuntimeError, match="stale or invalid"):
        controller._require_imu()
    assert clock.now == 0.0
    assert locomotion.stops == 1
    assert len(locomotion.imu) == 1


def test_transport_only_stale_stops_then_recovers_after_half_second():
    clock = AdvancingClock()
    events = []
    stale = imu_sample(transport=.25)
    locomotion = SequencedTelemetry(imu=[stale] * 26 + [imu_sample()])
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock,
        sleep=clock.sleep, emit=events.append,
    )
    result = controller._require_imu()
    assert result["transport_age"] == 0.0
    assert locomotion.stops == 1
    assert clock.now == pytest.approx(.5)
    assert any("TELEMETRY TRANSPORT STALE -> STOP" in item for item in events)
    assert any("TELEMETRY RECOVERED" in item for item in events)


def test_transport_recovery_timeout_is_final_failure():
    clock = AdvancingClock()
    events = []
    locomotion = SequencedTelemetry(imu=[imu_sample(transport=.25)])
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock,
        sleep=clock.sleep, emit=events.append,
    )
    with pytest.raises(RuntimeError, match="did not recover within 1.0s"):
        controller._require_imu()
    assert locomotion.stops == 1
    assert clock.now >= TRANSPORT_RECOVERY_S
    assert any("RECOVERY TIMEOUT -> FINAL STOP" in item for item in events)


def test_imu_source_fault_is_immediate_without_transport_recovery():
    clock = AdvancingClock()
    events = []
    locomotion = SequencedTelemetry(imu=[imu_sample(ready=False, transport=.25)])
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock,
        sleep=clock.sleep, emit=events.append,
    )
    with pytest.raises(RuntimeError, match="stale or invalid"):
        controller._require_imu()
    assert clock.now == 0.0
    assert locomotion.stops == 1
    assert events[0] == "[patrol] IMU SOURCE STALE -> FINAL STOP"


def test_odom_transport_only_stale_uses_same_bounded_recovery():
    clock = AdvancingClock()
    stale = odom_sample(transport=.25)
    locomotion = SequencedTelemetry(odom=[stale] * 6 + [odom_sample()])
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock, sleep=clock.sleep
    )
    assert controller._require_odom()["transport_age"] == 0.0
    assert locomotion.stops == 1
    assert clock.now == pytest.approx(.1)


def test_forward_transport_gap_preserves_odom_leg_progress():
    locomotion = ForwardTransportGap()
    controller = PatrolController(locomotion, ClearGuard(), config(), sleep=time.sleep)
    controller._run_forward(PatrolState.FORWARD_OUT, 1.0)
    assert locomotion.gap_injected
    assert locomotion.stops >= 2
    assert .99 <= locomotion.x <= 1.05
    assert controller.last_forward_metrics["progress_m"] >= .99


def test_turn_transport_gap_preserves_accumulated_progress_and_jump_guard():
    locomotion = TurnTransportGap()
    controller = PatrolController(locomotion, ClearGuard(), config(), sleep=time.sleep)
    controller._run_turn(PatrolState.TURN_BACK, 1.1)
    assert locomotion.gap_injected
    assert locomotion.stops >= 2
    assert 1.0 <= locomotion.yaw <= 1.25


def test_resume_requires_fresh_imu_and_odom_before_clearing_pause():
    clock = AdvancingClock()
    locomotion = SequencedTelemetry(
        imu=[imu_sample(age=1.0), imu_sample()],
        odom=[odom_sample(age=1.0), odom_sample()],
    )
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock, sleep=clock.sleep
    )
    controller.pause()
    controller.resume()
    assert not controller.control_status()["paused"]
    assert clock.now == pytest.approx(.02)


def test_resume_freshness_timeout_latches_final_stop():
    clock = AdvancingClock()
    locomotion = SequencedTelemetry(
        imu=[imu_sample()], odom=[odom_sample(transport=1.0)]
    )
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock, sleep=clock.sleep,
        emit=lambda _message: None,
    )
    controller.pause()
    with pytest.raises(RuntimeError, match="fresh IMU and odometry"):
        controller.resume()
    status = controller.control_status()
    assert status["stopped"] is True
    assert status["paused"] is True
    assert "within 2.0s" in status["error"]


def _capture(errors, function, *args):
    try:
        function(*args)
    except Exception as exc:
        errors.append(exc)
