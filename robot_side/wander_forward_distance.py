"""Bounded closed-loop forward commissioning; not connected to WanderDecision."""

from dataclasses import dataclass
import math
import time


MAX_SPEED_M_S = 0.10
MAX_PULSE_S = 0.50
MAX_TARGET_M = 1.00
MAX_HARD_CAP_M = 1.20
MAX_WALL_CLOCK_S = 15.0


@dataclass(frozen=True)
class ForwardDistancePlan:
    target_distance_m: float = 1.0
    speed_m_s: float = 0.10
    pulse_duration_s: float = 0.50
    hard_cap_m: float = 1.20
    wall_clock_cap_s: float = 15.0
    stale_after_s: float = 0.50
    hard_stop_distance_m: float = 0.35
    minimum_start_clearance_m: float = 2.00
    maximum_yaw_change_rad: float = 0.35

    def validate(self):
        values = tuple(self.__dict__.values())
        if not all(math.isfinite(value) for value in values):
            raise ValueError("all commissioning limits must be finite")
        if not 0 < self.speed_m_s <= MAX_SPEED_M_S:
            raise ValueError("speed must be in (0, %.2f]" % MAX_SPEED_M_S)
        if not 0 < self.pulse_duration_s <= MAX_PULSE_S:
            raise ValueError("pulse duration must be in (0, %.2f]" % MAX_PULSE_S)
        if not 0 < self.target_distance_m <= MAX_TARGET_M:
            raise ValueError("target distance must be in (0, %.2f]" % MAX_TARGET_M)
        if not self.target_distance_m < self.hard_cap_m <= MAX_HARD_CAP_M:
            raise ValueError("hard cap must be above target and at most %.2f" % MAX_HARD_CAP_M)
        if not 0 < self.wall_clock_cap_s <= MAX_WALL_CLOCK_S:
            raise ValueError("wall clock cap must be in (0, %.1f]" % MAX_WALL_CLOCK_S)
        if self.stale_after_s <= 0 or self.hard_stop_distance_m <= 0:
            raise ValueError("freshness and clearance limits must be positive")
        if self.minimum_start_clearance_m < 2.0:
            raise ValueError("start clearance must be at least 2.0 m")
        if self.maximum_yaw_change_rad <= 0:
            raise ValueError("yaw limit must be positive")


def _angle_delta(value, reference):
    return math.atan2(math.sin(value - reference), math.cos(value - reference))


def _pose(sample):
    odom = sample.get("odom") if sample else None
    if not isinstance(odom, dict):
        raise RuntimeError("dog_odom missing")
    values = tuple(float(odom[name]) for name in ("x", "y", "yaw"))
    if not all(math.isfinite(value) for value in values):
        raise RuntimeError("dog_odom invalid")
    return values


def _validate_sample(sample, plan, start_pose):
    if not sample:
        raise RuntimeError("telemetry missing")
    if sample.get("error"):
        raise RuntimeError("telemetry error: " + str(sample["error"]))
    for key in ("odom_age_s", "cloud_age_s"):
        age = sample.get(key)
        if age is None or not math.isfinite(float(age)) or float(age) > plan.stale_after_s:
            raise RuntimeError("%s stale or missing" % key)
    if not sample.get("cloud_valid"):
        raise RuntimeError("LiDAR invalid: " + str(sample.get("cloud_invalid_reason")))
    clearance = float(sample.get("forward_clearance_m", float("nan")))
    if not math.isfinite(clearance) or clearance <= plan.hard_stop_distance_m:
        raise RuntimeError("LiDAR safety failure: forward clearance %.3f" % clearance)
    pose = _pose(sample)
    yaw_change = _angle_delta(pose[2], start_pose[2])
    if abs(yaw_change) > plan.maximum_yaw_change_rad:
        raise RuntimeError("unexpected yaw change %.3f rad" % yaw_change)
    distance = math.hypot(pose[0] - start_pose[0], pose[1] - start_pose[1])
    if distance > plan.hard_cap_m:
        raise RuntimeError("distance hard cap exceeded: %.3f m" % distance)
    return pose, distance, yaw_change, clearance


def run_forward_distance(plan, telemetry, client, conflict_check,
                         clock=time.monotonic, sleep=time.sleep, emit=None):
    """Run bounded pulses and issue StopMove exactly once after any velocity call."""
    plan.validate()
    emit = emit or (lambda value: None)
    first = telemetry.wait_ready(min(3.0, plan.wall_clock_cap_s))
    start_pose = _pose(first)
    _, _, _, start_clearance = _validate_sample(first, plan, start_pose)
    if start_clearance < plan.minimum_start_clearance_m:
        raise RuntimeError("preflight forward clearance %.3f m is below %.3f m" % (
            start_clearance, plan.minimum_start_clearance_m))
    started = clock()
    calls = 0
    rpc_results = []
    stop_result = None
    end_pose = start_pose
    distance = 0.0
    yaw_change = 0.0
    failure = None
    try:
        while True:
            sample = telemetry.latest()
            end_pose, distance, yaw_change, clearance = _validate_sample(
                sample, plan, start_pose)
            elapsed = clock() - started
            if distance >= plan.target_distance_m:
                break
            if elapsed >= plan.wall_clock_cap_s:
                raise RuntimeError("wall clock cap reached before target")
            conflicts = conflict_check()
            if conflicts:
                raise RuntimeError("body writer conflict: " + "; ".join(conflicts))
            result = client.SetVelocity(
                plan.speed_m_s, 0.0, 0.0, plan.pulse_duration_s)
            calls += 1
            rpc_results.append(result)
            emit({
                "timestamp": time.time(), "x": end_pose[0], "y": end_pose[1],
                "yaw": end_pose[2], "distance_from_start": distance,
                "forward_clearance_m": clearance, "rpc_result": result,
                "set_velocity_call": calls,
            })
            if result != 0:
                raise RuntimeError("SetVelocity returned %r" % (result,))
            pulse_end = clock() + plan.pulse_duration_s
            while clock() < pulse_end:
                sleep(min(0.02, max(0.0, pulse_end - clock())))
                sample = telemetry.latest()
                end_pose, distance, yaw_change, _ = _validate_sample(
                    sample, plan, start_pose)
                if distance >= plan.target_distance_m:
                    break
                if clock() - started >= plan.wall_clock_cap_s:
                    raise RuntimeError("wall clock cap reached before target")
            if distance >= plan.target_distance_m:
                break
    except BaseException as exc:
        failure = exc
    finally:
        if calls:
            try:
                stop_result = client.StopMove()
            except BaseException as exc:
                if failure is None:
                    failure = exc
    final_sample = telemetry.latest()
    try:
        end_pose, distance, yaw_change, _ = _validate_sample(
            final_sample, plan, start_pose)
    except BaseException as exc:
        if failure is None:
            failure = exc
    if stop_result not in (None, 0) and failure is None:
        failure = RuntimeError("StopMove returned %r" % (stop_result,))
    return {
        "status": "pass" if failure is None and distance >= plan.target_distance_m else "fail",
        "reason": None if failure is None else str(failure),
        "start_pose": start_pose, "end_pose": end_pose,
        "net_displacement_m": distance, "yaw_change_rad": yaw_change,
        "elapsed_s": clock() - started, "set_velocity_calls": calls,
        "rpc_results": rpc_results, "stop_result": stop_result,
    }
