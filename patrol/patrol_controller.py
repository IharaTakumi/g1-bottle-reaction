"""Time-integrated forward-only patrol state machine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import threading
import time

from lidar_guard import GuardState

TRANSPORT_RECOVERY_S = 1.0


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
        self._pause_requested = threading.Event()
        self._pause_acknowledged = threading.Event()
        self._stop = threading.Event()
        self._recovering_telemetry = threading.Event()
        self._motion_lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._pause_reason: str | None = None
        self._active_state = PatrolState.STOPPED
        self._last_error: str | None = None
        self._progress_m: float | None = None
        self._remaining_m: float | None = None
        self._turn_progress_rad: float | None = None
        self._turn_remaining_rad: float | None = None
        self._last_stop_sent_monotonic: float | None = None

    def pause(self) -> None:
        self._pause_requested.set()
        self._activate_pause("operator")

    def request_reaction_pause(self, reason: str, timeout: float) -> dict[str, object]:
        if timeout <= 0:
            raise ValueError("pause timeout must be positive")
        with self._control_lock:
            if self._stop.is_set() or self._last_error:
                raise RuntimeError(self._last_error or "patrol is stopped")
            self._pause_reason = reason.strip() or "reaction"
            self._pause_acknowledged.clear()
            self._pause_requested.set()
        if not self._recovering_telemetry.is_set():
            self._activate_pause(self._pause_reason)
        if not self._pause_acknowledged.wait(timeout):
            self.stop()
            raise TimeoutError("patrol did not confirm pause before timeout")
        status = self.control_status()
        if status["error"] or status["stopped"] or not status["paused"]:
            raise RuntimeError(str(status["error"] or "patrol pause failed"))
        return status

    def resume(self) -> None:
        with self._control_lock:
            if self._stop.is_set() or self._last_error:
                raise RuntimeError(self._last_error or "patrol is stopped")
            if not self._paused.is_set():
                raise RuntimeError("patrol is not paused")
        try:
            self._wait_for_fresh_telemetry("RESUME")
        except Exception as exc:
            self._record_failure(exc)
            self.stop()
            raise
        with self._control_lock:
            if self._stop.is_set() or self._last_error:
                raise RuntimeError(self._last_error or "patrol is stopped")
            if not self._paused.is_set():
                raise RuntimeError("patrol pause was lost during freshness barrier")
            self._pause_reason = None
            self._pause_requested.clear()
            self._pause_acknowledged.clear()
        self._paused.clear()

    def stop(self) -> None:
        self._stop.set()
        self._paused.set()
        self._pause_acknowledged.set()
        self._send_stop()
        self.state = PatrolState.STOPPED

    def verify_reaction_ready(self) -> dict[str, object]:
        with self._control_lock:
            if self._stop.is_set() or self._last_error:
                raise RuntimeError(self._last_error or "patrol is stopped")
            if not self._paused.is_set():
                raise RuntimeError("patrol must be paused before reaction readiness")
        try:
            self._wait_for_fresh_telemetry("REACTION")
        except Exception as exc:
            self._record_failure(exc)
            self.stop()
            raise
        return self.control_status()

    def control_status(self) -> dict[str, object]:
        with self._control_lock:
            return {
                "state": self.state.value,
                "phase": self._active_state.value,
                "paused": self._paused.is_set(),
                "pause_pending": (
                    self._pause_requested.is_set() and not self._paused.is_set()
                ),
                "pause_reason": self._pause_reason,
                "telemetry_recovering": self._recovering_telemetry.is_set(),
                "stopped": self._stop.is_set() or self.state is PatrolState.STOPPED,
                "error": self._last_error,
                "progress_m": self._progress_m,
                "remaining_m": self._remaining_m,
                "turn_progress_rad": self._turn_progress_rad,
                "turn_remaining_rad": self._turn_remaining_rad,
                "stop_sent_monotonic": self._last_stop_sent_monotonic,
            }

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
                    self._send_stop()
                    self.emit(f"[patrol] {state.value} complete")
                    self._checkpoint_reaction_pause()
                    self.sleep(self.config.settle_s)
                completed += 1
                self.emit(f"[patrol] loop complete count={completed}")
        except Exception as exc:
            self._record_failure(exc)
            raise
        finally:
            self._send_stop()
            self._stop.set()
            self._pause_acknowledged.set()
            self.state = PatrolState.STOPPED
            self.emit("[patrol] STOP")

    def run_turn_only(self) -> None:
        try:
            self._run_turn(PatrolState.TURN_BACK, self.config.turn_angle_rad)
            self._send_stop()
            self.emit("[patrol] TURN_BACK complete")
            self._checkpoint_reaction_pause()
        except Exception as exc:
            self._record_failure(exc)
            raise
        finally:
            self._send_stop()
            self._stop.set()
            self._pause_acknowledged.set()
            self.state = PatrolState.STOPPED
            self.emit("[patrol] STOP")

    def _run_forward(self, state: PatrolState, target: float) -> None:
        vx = self.config.forward_speed_m_s
        if target <= 0 or vx <= 0:
            raise ValueError("forward distance and speed must be positive")
        self._active_state = state
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
                self._send_stop()
                raise RuntimeError("FORWARD FAILED: unnatural odom jump")
            previous_x, previous_y = x, y
            imu = self._require_imu()
            current_yaw = float(imu["yaw"])
            yaw_delta = wrap_to_pi(current_yaw - previous_imu_yaw)
            if abs(yaw_delta) > 0.50:
                self._send_stop()
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
                self._send_stop()
                raise RuntimeError(
                    f"FORWARD FAILED: lateral drift {lateral:.3f}m exceeds "
                    f"{self.config.max_lateral_drift_m:.3f}m"
                )
            remaining = target - progress
            with self._control_lock:
                self._progress_m = progress
                self._remaining_m = max(0.0, remaining)
            if remaining <= self.config.distance_tolerance_m:
                self._send_stop()
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
            if guard_state is GuardState.STALE:
                self._send_stop()
                raise RuntimeError("FORWARD FAILED: LiDAR guard stale")
            if self._pause_requested.is_set() and not self._paused.is_set():
                self._activate_pause(self._pause_reason or "reaction")
            if not self._paused.is_set() and guard_state is GuardState.CLEAR:
                if not was_moving:
                    self.emit(f"[guard] CLEAR -> RESUME remaining={remaining:.3f}m")
                self.state = state
                was_moving = self._send_move(vx, correction)
            else:
                if was_moving:
                    self._send_stop()
                    self.emit(f"[guard] {guard_state.value} -> STOP")
                was_moving = False
                if self._paused.is_set():
                    self.state = PatrolState.PAUSED
            self.sleep(self.config.command_period_s)

    def _require_odom(self):
        sample = self.locomotion.odom_sample()
        if not self._odom_source_healthy(sample):
            self.emit("[patrol] ODOM SOURCE STALE -> FINAL STOP")
            self._emit_odom_diagnostic(sample)
            self._send_stop()
            raise RuntimeError("FORWARD FAILED: odometry stale or invalid")
        if not self._transport_is_fresh(sample):
            return self._recover_transport("ODOM", sample)
        return sample

    def _wait_for_odom(self, context: str = "FORWARD"):
        deadline = self.clock() + 2.0
        sample = None
        while self.clock() < deadline:
            sample = self.locomotion.odom_sample()
            if self._odom_is_fresh(sample):
                return sample
            self.sleep(0.02)
        self._send_stop()
        self._emit_odom_diagnostic(sample)
        raise RuntimeError(
            f"{context} FAILED: fresh odometry not received within 2.0s"
        )

    def _run_turn(self, state: PatrolState, target: float) -> None:
        vyaw = self.config.turn_yaw_rate_rad_s
        if target <= 0 or not 0 < abs(vyaw) <= 0.50:
            raise ValueError("turn angle and yaw rate must be within limits")
        self._active_state = state
        self.state = state
        sample = self._wait_for_imu()
        previous = float(sample["yaw"])
        accumulated = 0.0
        direction = 1.0 if vyaw > 0 else -1.0
        started = self.clock()
        pause_started = None
        moving = False
        self.emit(f"[patrol] {state.value} target={target:.3f}rad IMU closed-loop")
        while direction * accumulated < self.config.turn_stop_at_rad:
            if self._stop.is_set():
                raise RuntimeError("patrol stopped")
            sample = self._require_imu()
            current = float(sample["yaw"])
            delta = wrap_to_pi(current - previous)
            if abs(delta) > 0.50:
                self._send_stop()
                raise RuntimeError(f"TURN FAILED: unnatural yaw jump {delta:.3f}rad")
            accumulated += delta
            previous = current
            progress = direction * accumulated
            with self._control_lock:
                self._turn_progress_rad = progress
                self._turn_remaining_rad = max(
                    0.0, self.config.turn_stop_at_rad - progress
                )
            guard_state = self.guard.state("front")
            if guard_state is GuardState.STALE:
                self._send_stop()
                raise RuntimeError("TURN FAILED: LiDAR guard stale")
            if self._paused.is_set():
                if pause_started is None:
                    pause_started = self.clock()
                if moving:
                    self._send_stop()
                moving = False
                self.sleep(self.config.command_period_s)
                continue
            if pause_started is not None:
                started += self.clock() - pause_started
                pause_started = None
            if self.clock() - started > self.config.max_turn_duration_s:
                self._send_stop()
                raise RuntimeError("TURN FAILED: max turn duration exceeded")
            command_yaw = (self.config.turn_slow_yaw_rate_rad_s
                           if progress >= self.config.turn_slow_after_rad else abs(vyaw)) * direction
            moving = self._send_move(0.0, command_yaw)
            self.sleep(self.config.command_period_s)
        self._send_stop()
        self.sleep(0.50)
        settled = self._require_imu()
        accumulated += wrap_to_pi(float(settled["yaw"]) - previous)
        self.emit(f"[patrol] {state.value} actual_relative_yaw={math.degrees(direction*accumulated):.1f}deg")

    def _require_imu(self):
        sample = self.locomotion.imu_sample()
        if not self._imu_source_healthy(sample):
            self.emit("[patrol] IMU SOURCE STALE -> FINAL STOP")
            self._emit_imu_diagnostic(sample)
            self._send_stop()
            raise RuntimeError("TURN FAILED: IMU telemetry stale or invalid")
        if not self._transport_is_fresh(sample):
            return self._recover_transport("IMU", sample)
        return sample

    def _wait_for_imu(self, context: str = "TURN"):
        deadline = self.clock() + 2.0
        sample = None
        while self.clock() < deadline:
            sample = self.locomotion.imu_sample()
            if self._imu_is_fresh(sample):
                return sample
            self.sleep(0.02)
        self._send_stop()
        self._emit_imu_diagnostic(sample)
        raise RuntimeError(
            f"{context} FAILED: fresh IMU telemetry not received within 2.0s"
        )

    def _imu_is_fresh(self, sample) -> bool:
        return self._imu_source_healthy(sample) and self._transport_is_fresh(sample)

    def _odom_is_fresh(self, sample) -> bool:
        return self._odom_source_healthy(sample) and self._transport_is_fresh(sample)

    def _imu_source_healthy(self, sample) -> bool:
        if not sample or not sample.get("imu_ready"):
            return False
        try:
            yaw = float(sample.get("yaw"))
            imu_age = float(sample.get("imu_age"))
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(yaw)
            and math.isfinite(imu_age)
            and imu_age <= self.config.imu_stale_s
        )

    def _odom_source_healthy(self, sample) -> bool:
        if not sample or not sample.get("odom_ready"):
            return False
        try:
            values = tuple(
                float(sample.get(name))
                for name in ("odom_x", "odom_y", "odom_yaw")
            )
            odom_age = float(sample.get("odom_age"))
        except (TypeError, ValueError):
            return False
        return (
            all(math.isfinite(value) for value in values)
            and math.isfinite(odom_age)
            and odom_age <= self.config.odom_stale_s
        )

    def _transport_is_fresh(self, sample) -> bool:
        try:
            age = float(sample.get("transport_age"))
        except (AttributeError, TypeError, ValueError):
            return False
        return math.isfinite(age) and age <= self.config.imu_stale_s

    def _recover_transport(self, source: str, sample):
        self._send_stop()
        self._recovering_telemetry.set()
        started = self.clock()
        self.emit(
            "[patrol] TELEMETRY TRANSPORT STALE -> STOP "
            f"source={source} transport_age={sample.get('transport_age')}"
        )
        latest = sample
        source_healthy = (
            self._imu_source_healthy if source == "IMU"
            else self._odom_source_healthy
        )
        get_sample = (
            self.locomotion.imu_sample if source == "IMU"
            else self.locomotion.odom_sample
        )
        recovered = False
        try:
            deadline = started + TRANSPORT_RECOVERY_S
            while self.clock() < deadline:
                if self._stop.is_set():
                    raise RuntimeError("patrol stopped during telemetry recovery")
                latest = get_sample()
                if not source_healthy(latest):
                    self.emit(f"[patrol] {source} SOURCE STALE -> FINAL STOP")
                    if source == "IMU":
                        self._emit_imu_diagnostic(latest)
                    else:
                        self._emit_odom_diagnostic(latest)
                    raise RuntimeError(f"{source} source became stale during recovery")
                if self._transport_is_fresh(latest):
                    elapsed = max(0.0, self.clock() - started)
                    self.emit(
                        "[patrol] TELEMETRY RECOVERED "
                        f"source={source} recovery_time={elapsed:.3f}s"
                    )
                    recovered = True
                    return latest
                self.sleep(0.02)
            self.emit(
                "[patrol] TELEMETRY RECOVERY TIMEOUT -> FINAL STOP "
                f"source={source} transport_age="
                f"{None if latest is None else latest.get('transport_age')}"
            )
            raise RuntimeError(
                f"{source} telemetry transport did not recover within "
                f"{TRANSPORT_RECOVERY_S:.1f}s"
            )
        finally:
            self._recovering_telemetry.clear()
            if (recovered and self._pause_requested.is_set()
                    and not self._paused.is_set()):
                self._activate_pause(self._pause_reason or "reaction")

    def _emit_imu_diagnostic(self, sample) -> None:
        sample = sample or {}
        self.emit(
            "[patrol] IMU STALE "
            f"imu_ready={sample.get('imu_ready')} "
            f"imu_age={sample.get('imu_age')} "
            f"transport_age={sample.get('transport_age')} "
            f"imu_rate_hz={sample.get('imu_rate_hz')}"
        )

    def _emit_odom_diagnostic(self, sample) -> None:
        sample = sample or {}
        self.emit(
            "[patrol] ODOM STALE "
            f"odom_ready={sample.get('odom_ready')} "
            f"odom_age={sample.get('odom_age')} "
            f"transport_age={sample.get('transport_age')} "
            f"odom_rate_hz={sample.get('odom_rate_hz')}"
        )

    def _wait_for_fresh_telemetry(self, context: str) -> tuple[object, object]:
        deadline = self.clock() + 2.0
        imu = odom = None
        while self.clock() < deadline:
            imu = self.locomotion.imu_sample()
            odom = self.locomotion.odom_sample()
            if self._imu_is_fresh(imu) and self._odom_is_fresh(odom):
                return imu, odom
            self.sleep(0.02)
        self._send_stop()
        if not self._imu_is_fresh(imu):
            self._emit_imu_diagnostic(imu)
        if not self._odom_is_fresh(odom):
            self._emit_odom_diagnostic(odom)
        raise RuntimeError(
            f"{context} FAILED: fresh IMU and odometry not received within 2.0s"
        )

    def _activate_pause(self, reason: str) -> None:
        with self._control_lock:
            if self._paused.is_set():
                return
            self._pause_reason = reason
            self._paused.set()
            self.state = PatrolState.PAUSED
        self._send_stop()
        self._pause_acknowledged.set()
        self.emit(f"[patrol] PAUSED reason={reason}")

    def _checkpoint_reaction_pause(self) -> None:
        if self._pause_requested.is_set() and not self._paused.is_set():
            self._activate_pause(self._pause_reason or "reaction")
        while self._paused.is_set():
            if self._stop.is_set():
                raise RuntimeError("patrol stopped")
            self._require_odom()
            self._require_imu()
            if self.guard.state("front") is GuardState.STALE:
                self._send_stop()
                raise RuntimeError("PAUSE FAILED: LiDAR guard stale")
            self.sleep(self.config.command_period_s)

    def _send_move(self, vx: float, vyaw: float) -> bool:
        with self._motion_lock:
            if (self._paused.is_set() or self._stop.is_set()
                    or self._recovering_telemetry.is_set()):
                return False
            self.locomotion.move(vx, vyaw)
            return True

    def _send_stop(self) -> None:
        with self._motion_lock:
            self.locomotion.stop()
            self._last_stop_sent_monotonic = self.clock()

    def _record_failure(self, exc: BaseException) -> None:
        with self._control_lock:
            self._last_error = str(exc)
        self._stop.set()
        self._pause_acknowledged.set()

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
