"""Fail-closed bounded-pulse controller for the Mapless Wander MVP."""

from dataclasses import dataclass
import math
import random
import time


SECTORS = ("left", "front_left", "front", "front_right", "right")


@dataclass(frozen=True)
class ReactiveMvpPlan:
    run_seconds: float = 30.0
    forward_speed_m_s: float = 0.20
    forward_duration_s: float = 0.50
    turn_rate_rad_s: float = 0.25
    turn_duration_s: float = 1.00
    blocked_distance_m: float = 0.80
    stale_after_s: float = 0.50
    tie_margin_m: float = 0.05
    seed: int = 1
    max_pulses: int = 100

    def validate(self):
        numeric = (
            self.run_seconds, self.forward_speed_m_s, self.forward_duration_s,
            self.turn_rate_rad_s, self.turn_duration_s,
            self.blocked_distance_m, self.stale_after_s, self.tie_margin_m,
        )
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("all limits must be finite")
        if not 0 < self.run_seconds <= 60.0:
            raise ValueError("run_seconds must be in (0, 60]")
        if not 0 < self.forward_speed_m_s <= 0.20:
            raise ValueError("forward speed must be in (0, 0.20]")
        if not 0 < self.forward_duration_s <= 0.50:
            raise ValueError("forward duration must be in (0, 0.50]")
        if not 0 < self.turn_rate_rad_s <= 0.25:
            raise ValueError("turn rate must be in (0, 0.25]")
        if not 0 < self.turn_duration_s <= 1.0:
            raise ValueError("turn duration must be in (0, 1.0]")
        if self.blocked_distance_m <= 0 or self.stale_after_s <= 0:
            raise ValueError("clearance and freshness limits must be positive")
        if self.tie_margin_m < 0:
            raise ValueError("tie margin must not be negative")
        if self.max_pulses <= 0:
            raise ValueError("max_pulses must be positive")


def validate_snapshot(sample, plan):
    if not isinstance(sample, dict):
        raise RuntimeError("LiDAR missing")
    if sample.get("error"):
        raise RuntimeError("telemetry error: " + str(sample["error"]))
    age = sample.get("cloud_age_s")
    if age is None or not math.isfinite(float(age)) or float(age) > plan.stale_after_s:
        raise RuntimeError("LiDAR stale or missing")
    if not sample.get("cloud_valid"):
        raise RuntimeError("LiDAR invalid: " + str(sample.get("cloud_invalid_reason")))
    snapshot = sample.get("obstacle_snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError("LiDAR snapshot missing")
    values = {}
    for name in SECTORS:
        try:
            value = float(snapshot[name])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("LiDAR sector %s missing or invalid" % name)
        if not math.isfinite(value) or value < 0:
            raise RuntimeError("LiDAR sector %s missing or invalid" % name)
        values[name] = value
    return values


def choose_action(snapshot, plan, rng):
    forward_clearance = min(
        snapshot["front_left"], snapshot["front"], snapshot["front_right"])
    if forward_clearance > plan.blocked_distance_m:
        return "FORWARD_PULSE", forward_clearance, None
    left_clearance = min(snapshot["left"], snapshot["front_left"])
    right_clearance = min(snapshot["right"], snapshot["front_right"])
    if left_clearance > right_clearance + plan.tie_margin_m:
        return "TURN_LEFT_PULSE", forward_clearance, "left-clearer"
    if right_clearance > left_clearance + plan.tie_margin_m:
        return "TURN_RIGHT_PULSE", forward_clearance, "right-clearer"
    action = rng.choice(("TURN_LEFT_PULSE", "TURN_RIGHT_PULSE"))
    return action, forward_clearance, "seeded-tie"


def _command_for(action, plan):
    if action == "FORWARD_PULSE":
        return (plan.forward_speed_m_s, 0.0, 0.0, plan.forward_duration_s)
    if action == "TURN_LEFT_PULSE":
        return (0.0, 0.0, plan.turn_rate_rad_s, plan.turn_duration_s)
    if action == "TURN_RIGHT_PULSE":
        return (0.0, 0.0, -plan.turn_rate_rad_s, plan.turn_duration_s)
    raise ValueError("unknown action: " + action)


def run_reactive_mvp(plan, telemetry, client, conflict_check,
                     clock=time.monotonic, sleep=time.sleep, emit=None):
    """Alternate OBSERVE and one bounded motion pulse; fail closed on any error."""
    plan.validate()
    emit = emit or (lambda value: None)
    rng = random.Random(plan.seed)
    started = clock()
    actions = []
    failure = None
    telemetry.wait_ready(min(3.0, plan.run_seconds))
    try:
        while clock() - started < plan.run_seconds and len(actions) < plan.max_pulses:
            conflicts = conflict_check()
            if conflicts:
                raise RuntimeError("body writer conflict: " + "; ".join(conflicts))
            observed = telemetry.latest()
            snapshot = validate_snapshot(observed, plan)
            action, forward_clearance, reason = choose_action(snapshot, plan, rng)
            vx, vy, omega, duration = _command_for(action, plan)
            entry = {
                "state": action, "timestamp": time.time(),
                "clearance": snapshot, "forward_clearance_m": forward_clearance,
                "selection_reason": reason, "vx": vx, "vy": vy,
                "omega": omega, "duration_s": duration,
                "odom": observed.get("odom"),
            }
            actions.append(entry)
            try:
                result = client.SetVelocity(vx, vy, omega, duration)
                entry["rpc_result"] = result
                emit(entry)
                if result != 0:
                    raise RuntimeError("SetVelocity returned %r" % (result,))
                pulse_end = clock() + duration
                next_conflict_check = clock() + 0.25
                while clock() < pulse_end:
                    sleep(min(0.05, max(0.0, pulse_end - clock())))
                    if clock() >= next_conflict_check:
                        conflicts = conflict_check()
                        if conflicts:
                            raise RuntimeError(
                                "body writer conflict: " + "; ".join(conflicts))
                        next_conflict_check = clock() + 0.25
                    live_snapshot = validate_snapshot(telemetry.latest(), plan)
                    if (action == "FORWARD_PULSE" and
                            min(live_snapshot["front_left"], live_snapshot["front"],
                                live_snapshot["front_right"]) <= plan.blocked_distance_m):
                        entry["interrupted_reason"] = (
                            "obstacle entered forward safety clearance")
                        break
            finally:
                stop_result = client.StopMove()
                entry["stop_result"] = stop_result
                if stop_result not in (None, 0):
                    raise RuntimeError("StopMove returned %r" % (stop_result,))
    except BaseException as exc:
        failure = exc
    result = {
        "status": "pass" if failure is None else "fail",
        "state": "STOPPED", "reason": None if failure is None else str(failure),
        "elapsed_s": clock() - started, "pulse_count": len(actions),
        "actions": actions,
    }
    emit({"state": "STOPPED", "reason": result["reason"]})
    return result
