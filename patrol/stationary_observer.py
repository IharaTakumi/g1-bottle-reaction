"""Pure offline evidence evaluation; CONFIRMED is not a physical-stop guarantee.

All timestamps must use a caller-verified common monotonic/replay clock. Raw
relay UDP alone does not supply source-time conversion and cannot qualify.
No I/O, SDK, wall clock, production thresholds, or Reaction integration.
"""
from dataclasses import dataclass, asdict
import math
import uuid


IDENTITY = ("relay_epoch", "owner_session", "movement_generation", "stop_request_id")


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def uuid_value(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True)
class EvidenceConfig:
    max_planar_excursion_m: float
    max_yaw_excursion_rad: float
    max_gyro_rad_s: float
    required_hold_s: float
    minimum_samples: int
    maximum_sample_gap_s: float
    maximum_transport_age_s: float
    maximum_source_age_s: float

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key == "minimum_samples":
                if type(value) is not int or value < 2:
                    raise ValueError("minimum_samples must be an integer >= 2")
            elif not finite(value) or value <= 0:
                raise ValueError(key + " must be explicitly positive and finite")
        if self.max_yaw_excursion_rad >= math.pi:
            raise ValueError("yaw bound must be below pi; sparse rotations are ambiguous")


class StationaryEvidenceObserver:
    """One explicit post-commit window; invalid/moving evidence never auto-resets.

    Constant memory. Excursion is relative to the first eligible sample; yaw
    uses accumulated shortest signed deltas and the full min/max range.
    """
    def __init__(self, config):
        if not isinstance(config, EvidenceConfig):
            raise ValueError("explicit EvidenceConfig required; confirmation disabled")
        self.config = config
        self.state = "IDLE"
        self.reason = "not started"
        self.context = None
        self._last = None
        self._now = None
        self._count = 0
        self._first = None
        self._metrics = dict(max_planar_excursion_m=0.0, odom_yaw_excursion_rad=0.0,
                             imu_yaw_excursion_rad=0.0, max_gyro_rad_s=0.0,
                             maximum_sample_gap_s=0.0, duration_s=0.0)
        self._yaw = {key: [0.0, 0.0, 0.0] for key in ("odom_yaw", "imu_yaw")}

    def invalidate(self, reason):
        self.state, self.reason = "INVALID_EVIDENCE", str(reason)
        return self.result()

    def start(self, context):
        # New evaluation requires an explicit start, never automatic rebinding.
        self.__init__(self.config)
        if not isinstance(context, dict):
            return self.invalidate("missing STOP context")
        if (any(not uuid_value(context.get(k)) for k in IDENTITY) or
                context.get("stop_rpc_status") != "STOP_RPC_CONFIRMED" or
                context.get("relay_state") != "MOVEMENT_HELD" or
                context.get("control_fault") is not False or
                context.get("owner_valid") is not True or
                not isinstance(context.get("clock_id"), str) or not context["clock_id"] or
                not finite(context.get("commit_at")) or context["commit_at"] < 0 or
                type(context.get("odom_stamp_ns")) is not int or context["odom_stamp_ns"] <= 0 or
                type(context.get("imu_tick")) is not int or not 0 <= context["imu_tick"] < 2**32):
            return self.invalidate("unconfirmed STOP or invalid source baselines/clock")
        self.context = dict(context)
        self.state, self.reason = "COLLECTING", "insufficient evidence"
        return self.result()

    def _context_valid(self, value):
        return (isinstance(value, dict) and self.context is not None and
                all(value.get(k) == self.context[k] for k in IDENTITY + ("clock_id",)) and
                value.get("relay_state") == "MOVEMENT_HELD" and
                value.get("owner_valid") is True and value.get("control_fault") is False and
                value.get("stop_rpc_status") == "STOP_RPC_CONFIRMED")

    def _health(self, sample, now):
        if not finite(now) or (self._now is not None and now < self._now):
            return "evaluation clock invalid/backwards"
        self._now = now
        if not self._context_valid(sample):
            return "STOP identity, ownership, movement or control state changed"
        times = [sample.get(k) for k in ("received_at", "odom_source_at", "imu_source_at")]
        if not all(finite(t) for t in times):
            return "missing/nonfinite source or transport time"
        received, odom_time, imu_time = times
        if not (self.context["commit_at"] < min(odom_time, imu_time) <= max(odom_time, imu_time) <= received <= now):
            return "pre-STOP sample or invalid source/transport clock ordering"
        if now - received > self.config.maximum_transport_age_s:
            return "transport stale"
        if max(now - odom_time, now - imu_time) > self.config.maximum_source_age_s:
            return "source stale"
        return None

    def observe(self, sample, now):
        if self.state == "INVALID_EVIDENCE":
            return self.result()
        error = self._health(sample, now)
        if error:
            return self.invalidate(error)
        try:
            values = [sample[k] for k in ("x", "y", "odom_yaw", "imu_yaw")]
            gyro = sample["gyro"]
            if (not all(finite(v) for v in values) or
                    not isinstance(gyro, (tuple, list)) or len(gyro) != 3 or
                    not all(finite(v) for v in gyro)):
                return self.invalidate("missing/nonfinite pose or gyro")
            if any(abs(sample[k]) > math.pi for k in ("odom_yaw", "imu_yaw")):
                return self.invalidate("yaw must be in [-pi, pi]")
            previous = self._last or self.context
            for key in ("odom_stamp_ns", "imu_tick"):
                if type(sample.get(key)) is not int or sample[key] <= previous[key]:
                    return self.invalidate("source progression stopped/backwards: " + key)
            if not 0 <= sample["imu_tick"] < 2**32:
                return self.invalidate("tick outside uint32; wrap requires a new evaluation")
            if self._last is not None:
                gaps = [sample[k] - self._last[k] for k in
                        ("received_at", "odom_source_at", "imu_source_at")]
                if min(gaps) <= 0:
                    return self.invalidate("transport/source time not progressing")
                self._metrics["maximum_sample_gap_s"] = max(
                    self._metrics["maximum_sample_gap_s"], max(gaps))
                if max(gaps) > self.config.maximum_sample_gap_s:
                    return self.invalidate("sample gap exceeded")
            # Snapshot caller-owned data; later mutation cannot alter evidence.
            snapshot = dict(sample, gyro=tuple(gyro))
            if self._first is None:
                self._first = snapshot
            self._count += 1
            excursion = math.hypot(sample["x"] - self._first["x"], sample["y"] - self._first["y"])
            self._metrics["max_planar_excursion_m"] = max(self._metrics["max_planar_excursion_m"], excursion)
            self._metrics["max_gyro_rad_s"] = max(self._metrics["max_gyro_rad_s"], math.hypot(*gyro))
            for key in self._yaw:
                if self._last is not None:
                    delta = sample[key] - self._last[key]
                    self._yaw[key][0] += math.atan2(math.sin(delta), math.cos(delta))
                angle, low, high = self._yaw[key]
                self._yaw[key] = [angle, min(low, angle), max(high, angle)]
                self._metrics[key + "_excursion_rad"] = self._yaw[key][2] - self._yaw[key][1]
            duration = min(sample["odom_source_at"], sample["imu_source_at"]) - max(
                self._first["odom_source_at"], self._first["imu_source_at"])
            self._metrics["duration_s"] = max(0.0, duration)
            self._last = snapshot
            if not all(finite(v) for v in self._metrics.values()):
                return self.invalidate("metric overflow")
            moving = (self._metrics["max_planar_excursion_m"] > self.config.max_planar_excursion_m or
                      max(self._metrics["odom_yaw_excursion_rad"], self._metrics["imu_yaw_excursion_rad"]) > self.config.max_yaw_excursion_rad or
                      self._metrics["max_gyro_rad_s"] > self.config.max_gyro_rad_s)
            if moving or self.state == "NOT_STATIONARY":
                self.state, self.reason = "NOT_STATIONARY", "window motion bound exceeded"
            elif self._count >= self.config.minimum_samples and duration >= self.config.required_hold_s:
                self.state, self.reason = "CONFIRMED", "offline evidence within explicit bounds"
            return self.result()
        except (KeyError, TypeError, ValueError, OverflowError):
            return self.invalidate("malformed evidence sample")

    def evaluate(self, now, context):
        """Recheck age and external control events even when no sample arrives."""
        if self.state == "INVALID_EVIDENCE":
            return self.result()
        if not self._context_valid(context):
            return self.invalidate("STOP identity or control event changed")
        if self._last is None:
            if (not finite(now) or now < self.context["commit_at"] or
                    (self._now is not None and now < self._now)):
                return self.invalidate("invalid evaluation clock")
            self._now = now
            if now - self.context["commit_at"] > self.config.maximum_sample_gap_s:
                return self.invalidate("no post-STOP samples")
        else:
            error = self._health(self._last, now)
            if error:
                return self.invalidate(error)
            if now - min(self._last["odom_source_at"], self._last["imu_source_at"]) > self.config.maximum_sample_gap_s:
                return self.invalidate("source progression stopped")
        return self.result()

    def result(self):
        """Snapshot, not a freshness refresh: use evaluate(now, context) before use."""
        return dict(state=self.state, reason=self.reason, sample_count=self._count,
                    metrics=dict(self._metrics), physical_stationary_proven=False)
