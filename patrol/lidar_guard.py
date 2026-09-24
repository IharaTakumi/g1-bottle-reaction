"""Fail-closed directional guard for MID-360 PointCloud2 samples."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import struct
import threading
import time
from typing import Iterable, Iterator


class GuardState(str, Enum):
    CLEAR = "CLEAR"
    BLOCKED = "BLOCKED"
    STALE = "STALE"


@dataclass(frozen=True)
class GuardConfig:
    stale_after_s: float = 0.50
    clear_hold_s: float = 0.40
    stop_distance_m: float = 0.80
    corridor_half_width_m: float = 0.55
    min_range_m: float = 0.25
    min_height_m: float = -0.90
    max_height_m: float = 0.50
    blocked_point_count: int = 5
    ready_point_count: int = 20
    minimum_scan_points: int = 100
    minimum_scan_rate_hz: float = 5.0
    maximum_scan_rate_hz: float = 30.0


_FIELD_FORMATS = {1: "b", 2: "B", 3: "h", 4: "H", 5: "i", 6: "I", 7: "f", 8: "d"}


def decode_xyz(cloud: object) -> Iterator[tuple[float, float, float]]:
    fields = {field.name: field for field in cloud.fields}
    if not all(name in fields for name in ("x", "y", "z")):
        raise ValueError("PointCloud2 requires x/y/z fields")
    if cloud.point_step <= 0 or cloud.row_step < cloud.width * cloud.point_step:
        raise ValueError("inconsistent PointCloud2 layout")
    if len(cloud.data) != cloud.row_step * cloud.height:
        raise ValueError("inconsistent PointCloud2 payload length")
    endian = ">" if cloud.is_bigendian else "<"
    unpackers = {}
    for name in ("x", "y", "z"):
        field = fields[name]
        if field.count != 1 or field.datatype not in _FIELD_FORMATS:
            raise ValueError("PointCloud2 x/y/z fields must be scalar numeric values")
        unpackers[name] = struct.Struct(endian + _FIELD_FORMATS[field.datatype])
        if field.offset + unpackers[name].size > cloud.point_step:
            raise ValueError("PointCloud2 field exceeds point_step")
    data = bytes(cloud.data)
    for row in range(cloud.height):
        row_start = row * cloud.row_step
        for column in range(cloud.width):
            start = row_start + column * cloud.point_step
            yield tuple(
                unpackers[name].unpack_from(data, start + fields[name].offset)[0]
                for name in ("x", "y", "z")
            )


class LidarGuard:
    """Reduce raw sensor-frame points to front/rear CLEAR/BLOCKED/STALE.

    Local evidence says the MID-360 is roll-flipped: sensor +x remains robot
    forward, sensor -y is robot left, and sensor -z is robot up.  Therefore a
    point's base-relative coordinates here are (x, -y, -z).  Live stationary
    preflight must still pass before reverse can be armed.
    """

    def __init__(self, config: GuardConfig | None = None, clock=time.monotonic):
        self.config = config or GuardConfig()
        self._clock = clock
        self._lock = threading.Lock()
        self._last_scan_at: float | None = None
        self._scan_times: list[float] = []
        self._blocked = {"front": True, "rear": True}
        self._clear_since = {"front": None, "rear": None}
        self._stop_points = {"front": 0, "rear": 0}
        self._coverage_points = {"front": 0, "rear": 0}
        self._nearest = {"front": None, "rear": None}
        self._total_scan_points = 0
        self._error: str | None = "no scan received"

    def update_cloud(self, cloud: object) -> None:
        self.update_points(decode_xyz(cloud))

    def update_points(self, points: Iterable[tuple[float, float, float]]) -> None:
        now = self._clock()
        stop = {"front": 0, "rear": 0}
        coverage = {"front": 0, "rear": 0}
        nearest = {"front": math.inf, "rear": math.inf}
        total_scan_points = 0
        try:
            for raw_x, raw_y, raw_z in points:
                x, left, up = float(raw_x), -float(raw_y), -float(raw_z)
                if not all(math.isfinite(v) for v in (x, left, up)):
                    continue
                total_scan_points += 1
                if not (self.config.min_height_m <= up <= self.config.max_height_m):
                    continue
                if abs(left) > self.config.corridor_half_width_m:
                    continue
                direction = "front" if x > 0 else "rear"
                distance = abs(x)
                if distance < self.config.min_range_m:
                    continue
                if distance <= 4.0:
                    coverage[direction] += 1
                if distance <= self.config.stop_distance_m:
                    stop[direction] += 1
                    nearest[direction] = min(nearest[direction], distance)
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            return
        with self._lock:
            self._last_scan_at = now
            self._scan_times = [t for t in self._scan_times if now - t <= 2.0] + [now]
            self._stop_points = stop
            self._coverage_points = coverage
            self._nearest = {k: (None if math.isinf(v) else v) for k, v in nearest.items()}
            self._total_scan_points = total_scan_points
            self._error = (None if total_scan_points >= self.config.minimum_scan_points
                           else f"only {total_scan_points} finite scan points")
            for direction in ("front", "rear"):
                is_blocked = stop[direction] >= self.config.blocked_point_count
                if is_blocked:
                    self._blocked[direction] = True
                    self._clear_since[direction] = None
                elif self._blocked[direction]:
                    since = self._clear_since[direction]
                    if since is None:
                        self._clear_since[direction] = now
                    elif now - since >= self.config.clear_hold_s:
                        self._blocked[direction] = False
                else:
                    self._clear_since[direction] = now

    def state(self, direction: str) -> GuardState:
        if direction not in ("front", "rear"):
            raise ValueError("direction must be front or rear")
        now = self._clock()
        with self._lock:
            if self._error or self._last_scan_at is None or now - self._last_scan_at > self.config.stale_after_s:
                return GuardState.STALE
            return GuardState.BLOCKED if self._blocked[direction] else GuardState.CLEAR

    def snapshot(self) -> dict[str, object]:
        now = self._clock()
        with self._lock:
            age = None if self._last_scan_at is None else max(0.0, now - self._last_scan_at)
            times = tuple(self._scan_times)
            rate = 0.0 if len(times) < 2 else (len(times) - 1) / (times[-1] - times[0])
            result = {
                "rate_hz": rate,
                "last_scan_age_s": age,
                "error": self._error,
                "stop_points": dict(self._stop_points),
                "coverage_points": dict(self._coverage_points),
                "nearest_m": dict(self._nearest),
                "total_scan_points": self._total_scan_points,
            }
        sensor_ready = bool(
            age is not None
            and age <= self.config.stale_after_s
            and not result["error"]
            and self.config.minimum_scan_rate_hz <= rate <= self.config.maximum_scan_rate_hz
        )
        result["sensor_health"] = "READY" if sensor_ready else ("STALE" if age is None or age > self.config.stale_after_s else "INVALID")
        result["front_state"] = self.state("front").value
        result["rear_state"] = self.state("rear").value
        result["front_ready"] = sensor_ready
        result["rear_ready"] = bool(age is not None and age <= self.config.stale_after_s and not result["error"] and result["coverage_points"]["rear"] >= self.config.ready_point_count)
        return result
