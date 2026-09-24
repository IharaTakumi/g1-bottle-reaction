"""Time-integrated forward-only patrol state machine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import threading
import time

from lidar_guard import GuardState


class PatrolState(str, Enum):
    FORWARD_OUT = "FORWARD_OUT"
    TURN_BACK = "TURN_BACK"
    FORWARD_RETURN = "FORWARD_RETURN"
    TURN_HOME = "TURN_HOME"
    FORWARD_HOME = "FORWARD_HOME"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class PatrolConfig:
    forward_speed_m_s: float = 0.30
    forward_distance_m: float = 2.0
    return_distance_m: float = 4.0
    home_distance_m: float = 2.0
    turn_yaw_rate_rad_s: float = 0.50
    turn_angle_rad: float = math.pi
    turn_slow_yaw_rate_rad_s: float = 0.25
    turn_slow_after_rad: float = math.radians(150.0)
    turn_stop_at_rad: float = math.radians(177.0)
    max_turn_duration_s: float = 20.0
    imu_stale_s: float = 0.20
    settle_s: float = 0.20
    command_period_s: float = 0.10
    distance_tolerance_m: float = 0.05
    slow_distance_m: float = 0.30
    slow_forward_speed_m_s: float = 0.10
    odom_stale_s: float = 0.50
    odom_jump_m: float = 1.0
    max_lateral_drift_m: float = 0.80
    heading_hold: bool = True
    heading_kp: float = 0.8
    heading_max_yaw_rad_s: float = 0.12
    heading_deadband_rad: float = math.radians(2.0)


class PatrolController:
    def __init__(self, locomotion, guard, config: PatrolConfig | None = None,
                 clock=time.monotonic, sleep=time.sleep, emit=print):
        self.locomotion = locomotion
        self.guard = guard
        self.config = config or PatrolConfig()
        self.clock, self.sleep, self.emit = clock, sleep, emit
        self.state = PatrolState.STOPPED
        self._paused = threading.Event()
        self._stop = threading.Event()

    def pause(self) -> None:
        self._paused.set()
        self.locomotion.stop()

    def resume(self) -> None:
        self._paused.clear()

    def stop(self) -> None:
        self._stop.set()
        self.locomotion.stop()
        self.state = PatrolState.STOPPED

    def run(self, cycles: int | None = 1) -> None:
        stages = (
            ("forward", PatrolState.FORWARD_OUT, self.config.forward_distance_m),
            ("turn", PatrolState.TURN_BACK, self.config.turn_angle_rad),
            ("forward", PatrolState.FORWARD_RETURN, self.config.return_distance_m),
            ("turn", PatrolState.TURN_HOME, self.config.turn_angle_rad),
            ("forward", PatrolState.FORWARD_HOME, self.config.home_distance_m),
        )
        completed = 0
        try:
            while cycles is None or completed < cycles:
                for kind, state, target in stages:
                    if kind == "forward":
                        self._run_forward(state, target)
                    else:
                        self._run_turn(state, target)
                    self.locomotion.stop()
                    self.emit(f"[patrol] {state.value} complete")
                    self.sleep(self.config.settle_s)
                completed += 1
                self.emit(f"[patrol] loop complete count={completed}")
        finally:
            self.locomotion.stop()
            self.state = PatrolState.STOPPED
            self.emit("[patrol] STOP")

    def run_turn_only(self) -> None:
        try:
            self._run_turn(PatrolState.TURN_BACK, self.config.turn_angle_rad)
            self.locomotion.stop()
            self.emit("[patrol] TURN_BACK complete")
        finally:
            self.locomotion.stop()
            self.state = PatrolState.STOPPED
            self.emit("[patrol] STOP")

    def _run_forward(self, state: PatrolState, target: float) -> None:
        vx = self.config.forward_speed_m_s
        if target <= 0 or vx <= 0:
            raise ValueError("forward distance and speed must be positive")
        self.state = state
        start = self._wait_for_odom()
        start_x, start_y, start_yaw = (float(start["odom_x"]), float(start["odom_y"]),
                                       float(start["odom_yaw"]))
        imu = self._wait_for_imu()
        target_yaw = float(imu["yaw"])
        previous_imu_yaw = target_yaw
        previous_x, previous_y = start_x, start_y
        was_moving = False
        max_lateral = 0.0
        heading_error_sum = 0.0
        heading_error_count = 0
        max_heading_error = 0.0
        max_correction = 0.0
        self.emit(f"[patrol] {state.value} target={target:.3f}m odom closed-loop "
                  f"heading_target={math.degrees(target_yaw):.1f}deg")
        while True:
            if self._stop.is_set():
                raise RuntimeError("patrol stopped")
            sample = self._require_odom()
            x, y = float(sample["odom_x"]), float(sample["odom_y"])
            if math.hypot(x - previous_x, y - previous_y) > self.config.odom_jump_m:
                self.locomotion.stop()
                raise RuntimeError("FORWARD FAILED: unnatural odom jump")
            previous_x, previous_y = x, y
            imu = self._require_imu()
            current_yaw = float(imu["yaw"])
            yaw_delta = wrap_to_pi(current_yaw - previous_imu_yaw)
            if abs(yaw_delta) > 0.50:
                self.locomotion.stop()
                raise RuntimeError(f"FORWARD FAILED: unnatural IMU yaw jump {yaw_delta:.3f}rad")
            previous_imu_yaw = current_yaw
            heading_error = wrap_to_pi(target_yaw - current_yaw)
            correction = heading_yaw_command(heading_error, self.config)
            heading_error_sum += abs(heading_error)
            heading_error_count += 1
            max_heading_error = max(max_heading_error, abs(heading_error))
            max_correction = max(max_correction, abs(correction))
            dx, dy = x - start_x, y - start_y
            progress = dx * math.cos(start_yaw) + dy * math.sin(start_yaw)
            lateral = -dx * math.sin(start_yaw) + dy * math.cos(start_yaw)
            max_lateral = max(max_lateral, abs(lateral))
            if abs(lateral) > self.config.max_lateral_drift_m:
                self.locomotion.stop()
                raise RuntimeError(
                    f"FORWARD FAILED: lateral drift {lateral:.3f}m exceeds "
                    f"{self.config.max_lateral_drift_m:.3f}m"
                )
            remaining = target - progress
            if remaining <= self.config.distance_tolerance_m:
                self.locomotion.stop()
                self.last_forward_metrics = {
                    "progress_m": progress,
                    "max_lateral_m": max_lateral,
                    "max_heading_error_rad": max_heading_error,
                    "average_heading_error_rad": heading_error_sum / max(1, heading_error_count),
                    "max_correction_vyaw": max_correction,
                }
                self.emit(f"[patrol] {state.value} odom_progress={progress:.3f}m lateral={lateral:.3f}m")
                return
            guard_state = self.guard.state("front")
            if not self._paused.is_set() and guard_state is GuardState.CLEAR:
                if not was_moving:
                    self.emit(f"[guard] CLEAR -> RESUME remaining={remaining:.3f}m")
                self.locomotion.move(vx, correction)
                was_moving = True
            else:
                if was_moving:
                    self.locomotion.stop()
                    self.emit(f"[guard] {guard_state.value} -> STOP")
                was_moving = False
            self.sleep(self.config.command_period_s)

    def _require_odom(self):
        sample = self.locomotion.odom_sample()
        values = None if not sample else (sample.get("odom_x"), sample.get("odom_y"),
                                          sample.get("odom_yaw"))
        ages = () if not sample else (sample.get("odom_age"), sample.get("transport_age"))
        if (not sample or not sample.get("odom_ready") or values is None or
                not all(value is not None and math.isfinite(float(value)) for value in values) or
                any(age is None or float(age) > self.config.odom_stale_s for age in ages)):
            self.locomotion.stop()
            raise RuntimeError("FORWARD FAILED: odometry stale or invalid")
        return sample

    def _wait_for_odom(self):
        deadline = self.clock() + 2.0
        while self.clock() < deadline:
            sample = self.locomotion.odom_sample()
            if sample:
                return self._require_odom()
            self.sleep(0.02)
        self.locomotion.stop()
        raise RuntimeError("FORWARD FAILED: odometry not received before start")

    def _run_turn(self, state: PatrolState, target: float) -> None:
        vyaw = self.config.turn_yaw_rate_rad_s
        if target <= 0 or not 0 < abs(vyaw) <= 0.50:
            raise ValueError("turn angle and yaw rate must be within limits")
        self.state = state
        sample = self._wait_for_imu()
        previous = float(sample["yaw"])
        accumulated = 0.0
        direction = 1.0 if vyaw > 0 else -1.0
        started = self.clock()
        moving = False
        self.emit(f"[patrol] {state.value} target={target:.3f}rad IMU closed-loop")
        while direction * accumulated < self.config.turn_stop_at_rad:
            if self._stop.is_set():
                raise RuntimeError("patrol stopped")
            if self.clock() - started > self.config.max_turn_duration_s:
                self.locomotion.stop()
                raise RuntimeError("TURN FAILED: max turn duration exceeded")
            sample = self._require_imu()
            current = float(sample["yaw"])
            delta = wrap_to_pi(current - previous)
            if abs(delta) > 0.50:
                self.locomotion.stop()
                raise RuntimeError(f"TURN FAILED: unnatural yaw jump {delta:.3f}rad")
            accumulated += delta
            previous = current
            progress = direction * accumulated
            guard_state = self.guard.state("front")
            if guard_state is GuardState.STALE:
                self.locomotion.stop()
                raise RuntimeError("TURN FAILED: LiDAR guard stale")
            if self._paused.is_set():
                if moving:
                    self.locomotion.stop()
                moving = False
                self.sleep(self.config.command_period_s)
                continue
            command_yaw = (self.config.turn_slow_yaw_rate_rad_s
                           if progress >= self.config.turn_slow_after_rad else abs(vyaw)) * direction
            self.locomotion.move(0.0, command_yaw)
            moving = True
            self.sleep(self.config.command_period_s)
        self.locomotion.stop()
        self.sleep(0.50)
        settled = self._require_imu()
        accumulated += wrap_to_pi(float(settled["yaw"]) - previous)
        self.emit(f"[patrol] {state.value} actual_relative_yaw={math.degrees(direction*accumulated):.1f}deg")

    def _require_imu(self):
        sample = self.locomotion.imu_sample()
        if not sample:
            self.locomotion.stop()
            raise RuntimeError("TURN FAILED: IMU telemetry missing")
        yaw = sample.get("yaw")
        ages = (sample.get("imu_age"), sample.get("transport_age"))
        if (not sample.get("imu_ready") or yaw is None or not math.isfinite(float(yaw))
                or any(age is None or float(age) > self.config.imu_stale_s for age in ages)):
            self.locomotion.stop()
            raise RuntimeError("TURN FAILED: IMU telemetry stale or invalid")
        return sample

    def _wait_for_imu(self):
        deadline = self.clock() + 2.0
        while self.clock() < deadline:
            sample = self.locomotion.imu_sample()
            if sample:
                return self._require_imu()
            self.sleep(0.02)
        self.locomotion.stop()
        raise RuntimeError("TURN FAILED: IMU telemetry not received before start")

    def _run_timed_motion(self, state, target, vx, vyaw, rate, unit):
        self.state = state
        progress = 0.0
        last = self.clock()
        was_moving = False
        self.emit(f"[patrol] {state.value} target={target:.3f}{unit} vx={vx:+.2f} vyaw={vyaw:+.2f}")
        while progress < target:
            if self._stop.is_set():
                raise RuntimeError("patrol stopped")
            now = self.clock()
            if was_moving:
                progress += rate * max(0.0, now - last)
            last = now
            guard_state = self.guard.state("front")
            allowed = not self._paused.is_set() and guard_state is GuardState.CLEAR
            if allowed:
                if not was_moving:
                    self.emit(f"[guard] CLEAR -> RESUME remaining={max(0.0, target-progress):.3f}{unit}")
                self.state = state
                self.locomotion.move(vx, vyaw)
                was_moving = True
            else:
                if was_moving:
                    self.locomotion.stop()
                    self.emit(f"[guard] {guard_state.value} -> STOP")
                was_moving = False
                if self._paused.is_set():
                    self.state = PatrolState.PAUSED
            self.sleep(self.config.command_period_s)


def wrap_to_pi(value: float) -> float:
    return math.atan2(math.sin(value), math.cos(value))


def heading_yaw_command(error: float, config: PatrolConfig) -> float:
    if not config.heading_hold or abs(error) < config.heading_deadband_rad:
        return 0.0
    return max(-config.heading_max_yaw_rad_s,
               min(config.heading_max_yaw_rad_s, config.heading_kp * error))
