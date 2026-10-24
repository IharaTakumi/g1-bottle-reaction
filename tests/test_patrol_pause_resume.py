from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path
import sys

import pytest

PATROL = Path(__file__).resolve().parents[1] / "patrol"
sys.path.insert(0, str(PATROL))

from lidar_guard import GuardState
from patrol_controller import (
    LIDAR_BLOCKED_RECOVERY_S,
    LIDAR_RECOVERY_S,
    ODOM_SOURCE_RECOVERY_S,
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


class SequencedGuard:
    def __init__(self, states):
        self.states = list(states)
        self.last = self.states[-1]

    def state(self, _direction: str) -> GuardState:
        if self.states:
            self.last = self.states.pop(0)
        return self.last


class SimulatedLocomotion:
    def __init__(self) -> None:
        self.x = 0.0
        self.yaw = 0.0
        self.moves: list[tuple[float, float]] = []
        self.stops = 0
        self.imu_stale = False
        self.odom_stale = False
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
                "odom_ready": not self.odom_stale,
                "odom_x": self.x,
                "odom_y": 0.0,
                "odom_yaw": 0.0,
                "odom_age": 1.0 if self.odom_stale else 0.0,
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


class ForwardOdomSourceGap(SimulatedLocomotion):
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
            sample["odom_age"] = .639
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
    assert 0 < controller.control_status()["turn_remaining_rad"] <= 1.0
    controller.resume()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert 1.0 <= locomotion.yaw <= 1.25
    assert errors == []


@pytest.mark.parametrize("stale_source", ["odom", "imu"])
def test_telemetry_stale_while_paused_keeps_stop_and_does_not_abort(stale_source):
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
    move_count = len(locomotion.moves)
    if stale_source == "odom":
        locomotion.odom_stale = True
    else:
        locomotion.imu_stale = True
    time.sleep(.03)
    assert worker.is_alive()
    assert errors == []
    assert len(locomotion.moves) == move_count
    assert locomotion.stops > 0
    locomotion.odom_stale = False
    locomotion.imu_stale = False
    controller.resume()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []


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


def test_attended_two_second_transport_recovery_still_stops_while_stale():
    clock = AdvancingClock()
    events = []
    stale = imu_sample(transport=.25)
    locomotion = SequencedTelemetry(imu=[stale] * 76 + [imu_sample()])
    cfg = replace(config(), telemetry_recovery_s=2.0)
    controller = PatrolController(
        locomotion, ClearGuard(), cfg, clock=clock,
        sleep=clock.sleep, emit=events.append,
    )
    assert controller._require_imu()["transport_age"] == 0.0
    assert locomotion.stops == 1
    assert clock.now == pytest.approx(1.5)


def test_attended_two_second_transport_recovery_fails_closed_at_cap():
    clock = AdvancingClock()
    locomotion = SequencedTelemetry(imu=[imu_sample(transport=.25)])
    cfg = replace(config(), telemetry_recovery_s=2.0)
    controller = PatrolController(
        locomotion, ClearGuard(), cfg, clock=clock,
        sleep=clock.sleep, emit=lambda _message: None,
    )
    with pytest.raises(RuntimeError, match="within 2.0s"):
        controller._require_imu()
    assert locomotion.stops == 1
    assert clock.now >= 2.0


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


def test_hackathon_imu_source_stale_stops_then_recovers():
    clock = AdvancingClock()
    stale = imu_sample(ready=False, age=.8)
    locomotion = SequencedTelemetry(imu=[stale] * 26 + [imu_sample()])
    cfg = replace(config(), hackathon_runtime=True)
    controller = PatrolController(
        locomotion, ClearGuard(), cfg, clock=clock, sleep=clock.sleep
    )
    assert controller._require_imu()["imu_ready"] is True
    assert locomotion.stops == 1
    assert clock.now == pytest.approx(.5)


def test_hackathon_unrecovered_imu_source_is_final_stop():
    clock = AdvancingClock()
    locomotion = SequencedTelemetry(imu=[imu_sample(ready=False, age=.8)])
    cfg = replace(config(), hackathon_runtime=True)
    controller = PatrolController(
        locomotion, ClearGuard(), cfg, clock=clock, sleep=clock.sleep,
        emit=lambda _message: None,
    )
    with pytest.raises(RuntimeError, match="fresh IMU not received"):
        controller._require_imu()
    assert locomotion.stops == 1
    assert clock.now >= 2.0


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


def test_odom_transport_recovery_transition_to_source_gap_uses_two_second_wait():
    clock = AdvancingClock()
    events = []
    locomotion = SequencedTelemetry(odom=[
        odom_sample(transport=.25),
        odom_sample(age=.639),
        odom_sample(age=.639),
        odom_sample(),
    ])
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock,
        sleep=clock.sleep, emit=events.append,
    )
    assert controller._require_odom()["odom_age"] == 0.0
    assert locomotion.stops == 2
    assert any("ODOM SOURCE STALE -> STOP" in item for item in events)
    assert any("ODOM SOURCE RECOVERED -> RESUME" in item for item in events)


def test_odom_source_stale_stops_then_recovers_within_two_seconds():
    clock = AdvancingClock()
    events = []
    stale = odom_sample(age=.639)
    locomotion = SequencedTelemetry(odom=[stale] * 26 + [odom_sample()])
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock,
        sleep=clock.sleep, emit=events.append,
    )
    result = controller._require_odom()
    assert result["odom_age"] == 0.0
    assert locomotion.stops == 1
    assert clock.now == pytest.approx(.5)
    assert any("ODOM SOURCE STALE -> STOP" in item for item in events)
    assert any("ODOM SOURCE RECOVERED -> RESUME" in item for item in events)


def test_odom_source_recovery_timeout_is_final_failure():
    clock = AdvancingClock()
    events = []
    locomotion = SequencedTelemetry(odom=[odom_sample(age=.639)])
    controller = PatrolController(
        locomotion, ClearGuard(), config(), clock=clock,
        sleep=clock.sleep, emit=events.append,
    )
    with pytest.raises(RuntimeError, match="within 2.0s"):
        controller._require_odom()
    assert locomotion.stops >= 1
    assert clock.now >= ODOM_SOURCE_RECOVERY_S
    assert any("RECOVERY TIMEOUT -> FINAL STOP" in item for item in events)


def test_forward_transport_gap_preserves_odom_leg_progress():
    locomotion = ForwardTransportGap()
    controller = PatrolController(locomotion, ClearGuard(), config(), sleep=time.sleep)
    controller._run_forward(PatrolState.FORWARD_OUT, 1.0)
    assert locomotion.gap_injected
    assert locomotion.stops >= 2
    assert .99 <= locomotion.x <= 1.05
    assert controller.last_forward_metrics["progress_m"] >= .99


def test_forward_odom_source_gap_stops_and_resumes_same_leg_progress():
    locomotion = ForwardOdomSourceGap()
    events = []
    controller = PatrolController(
        locomotion, ClearGuard(), config(), sleep=time.sleep, emit=events.append
    )
    controller._run_forward(PatrolState.FORWARD_OUT, 1.0)
    assert locomotion.gap_injected
    assert locomotion.stops >= 2
    assert .99 <= locomotion.x <= 1.05
    assert controller.last_forward_metrics["progress_m"] >= .99
    assert any("ODOM SOURCE RECOVERED -> RESUME" in item for item in events)


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


def test_lidar_transient_stale_stops_and_recovers_before_move():
    clock = AdvancingClock()
    locomotion = SequencedTelemetry()
    guard = SequencedGuard([GuardState.STALE] * 6 + [GuardState.CLEAR])
    events = []
    controller = PatrolController(
        locomotion, guard, config(), clock=clock, sleep=clock.sleep,
        emit=events.append,
    )
    assert controller._recover_lidar_guard("FORWARD") is GuardState.CLEAR
    assert locomotion.stops == 1
    assert clock.now == pytest.approx(.12)
    assert any("LIDAR RECOVERED" in event for event in events)


def test_lidar_stale_timeout_remains_final_stop():
    clock = AdvancingClock()
    locomotion = SequencedTelemetry()
    guard = SequencedGuard([GuardState.STALE])
    controller = PatrolController(
        locomotion, guard, config(), clock=clock, sleep=clock.sleep,
        emit=lambda _message: None,
    )
    with pytest.raises(RuntimeError, match="did not recover"):
        controller._recover_lidar_guard("FORWARD")
    assert clock.now >= LIDAR_RECOVERY_S
    assert locomotion.stops == 2


def test_normal_lidar_blocked_stops_and_fails_without_move():
    locomotion = SimulatedLocomotion()
    controller = PatrolController(
        locomotion, SequencedGuard([GuardState.BLOCKED]), config(), sleep=time.sleep
    )
    with pytest.raises(RuntimeError, match="FAILED.*BLOCKED"):
        controller._run_forward(PatrolState.FORWARD_OUT, 1.0)
    assert locomotion.moves == []
    assert locomotion.stops >= 1


def test_hackathon_forward_blocked_stops_until_clear_then_resumes():
    locomotion = SimulatedLocomotion()
    guard = SequencedGuard([GuardState.BLOCKED] * 6 + [GuardState.CLEAR])
    events = []
    cfg = replace(config(), hackathon_runtime=True)
    controller = PatrolController(
        locomotion, guard, cfg, sleep=time.sleep, emit=events.append
    )
    controller._run_forward(PatrolState.FORWARD_OUT, 1.0)
    assert locomotion.stops >= 2
    assert locomotion.moves
    assert all(vx > 0 for vx, _ in locomotion.moves)
    assert any("RECOVERABLE_STOP" in event for event in events)
    assert any("BLOCKED RECOVERED -> RESUME" in event for event in events)


def test_hackathon_turn_blocked_stops_yaw_and_resumes_remaining_turn():
    locomotion = SimulatedLocomotion()
    guard = SequencedGuard([GuardState.BLOCKED] * 6 + [GuardState.CLEAR])
    events = []
    cfg = replace(config(), hackathon_runtime=True)
    controller = PatrolController(
        locomotion, guard, cfg, sleep=time.sleep, emit=events.append
    )
    controller._run_turn(PatrolState.TURN_HOME, 1.1)
    assert locomotion.stops >= 2
    assert locomotion.moves
    assert all(vx == 0.0 and vyaw != 0.0 for vx, vyaw in locomotion.moves)
    assert controller.control_status()["turn_progress_rad"] >= 1.0
    assert any("context=TURN" in event for event in events)


def test_hackathon_blocked_timeout_is_hard_fault_and_never_moves():
    clock = AdvancingClock()
    locomotion = SequencedTelemetry(
        imu=[imu_sample()], odom=[odom_sample()]
    )
    controller = PatrolController(
        locomotion, SequencedGuard([GuardState.BLOCKED]),
        replace(config(), hackathon_runtime=True),
        clock=clock, sleep=clock.sleep, emit=lambda _message: None,
    )
    with pytest.raises(RuntimeError, match="HARD_FAULT.*recovery timeout"):
        controller._recover_lidar_blocked("FORWARD")
    assert clock.now >= LIDAR_BLOCKED_RECOVERY_S
    assert locomotion.stops >= 2


def test_hackathon_blocked_recovery_operator_abort_is_immediate():
    clock = AdvancingClock()
    locomotion = SequencedTelemetry(
        imu=[imu_sample()], odom=[odom_sample()]
    )
    controller = PatrolController(
        locomotion, SequencedGuard([GuardState.BLOCKED]),
        replace(config(), hackathon_runtime=True),
        clock=clock, sleep=clock.sleep, emit=lambda _message: None,
    )

    def aborting_sleep(seconds):
        clock.sleep(seconds)
        controller.stop()

    controller.sleep = aborting_sleep
    with pytest.raises(RuntimeError, match="operator abort"):
        controller._recover_lidar_blocked("TURN")
    assert clock.now < LIDAR_BLOCKED_RECOVERY_S


def _capture(errors, function, *args):
    try:
        function(*args)
    except Exception as exc:
        errors.append(exc)
