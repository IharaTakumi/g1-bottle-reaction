from __future__ import annotations

import numpy as np

from .models import (
    LocalObstacleSnapshot,
    PointCloudConfig,
    SECTOR_NAMES,
    SectorObservation,
)


class LocalObstaclePerception:
    """Convert xyz points into five local sectors without assuming sensor axes."""

    def __init__(self, config: PointCloudConfig, *, blocked_distance_m: float) -> None:
        self.config = config
        self.blocked_distance_m = blocked_distance_m

    def snapshot(self, points: object, *, timestamp: float) -> LocalObstacleSnapshot:
        array = np.asarray(points, dtype=float)
        if array.ndim != 2 or array.shape[1] < 3:
            return LocalObstacleSnapshot(timestamp, {}, False, "point cloud must be Nx3")
        forward = self._axis(array, self.config.forward_axis)
        left = self._axis(array, self.config.left_axis)
        up = self._axis(array, self.config.up_axis)
        planar_range = np.hypot(forward, left)
        finite = np.isfinite(forward) & np.isfinite(left) & np.isfinite(up)
        usable = (
            finite
            & (up >= self.config.min_height_m)
            & (up <= self.config.max_height_m)
            & (planar_range >= self.config.min_range_m)
            & (planar_range <= self.config.max_range_m)
        )
        self_mask = (
            (forward <= self.config.self_mask_front_m)
            & (forward >= -self.config.self_mask_back_m)
            & (np.abs(left) <= self.config.self_mask_half_width_m)
        )
        usable &= ~self_mask
        forward = forward[usable]
        left = left[usable]
        planar_range = planar_range[usable]
        in_front_half = forward > 0
        forward = forward[in_front_half]
        left = left[in_front_half]
        planar_range = planar_range[in_front_half]
        if planar_range.size < self.config.minimum_valid_points:
            return LocalObstacleSnapshot(
                timestamp,
                {},
                False,
                f"only {planar_range.size} usable points",
            )

        angles = np.degrees(np.arctan2(left, forward))
        edges = self.config.sector_edges_degrees
        ordered_names = ("right", "front_right", "front", "front_left", "left")
        sectors: dict[str, SectorObservation] = {}
        for index, name in enumerate(ordered_names):
            inclusive = angles <= edges[index + 1] if index == 4 else angles < edges[index + 1]
            selected = (angles >= edges[index]) & inclusive
            distances = planar_range[selected]
            clearance = float(distances.min()) if distances.size else self.config.max_range_m
            sectors[name] = SectorObservation(
                clearance_m=clearance,
                valid_point_count=int(distances.size),
                blocked=clearance < self.blocked_distance_m,
            )
        return LocalObstacleSnapshot(
            timestamp=timestamp,
            sectors={name: sectors[name] for name in SECTOR_NAMES},
        )

    @staticmethod
    def _axis(points: np.ndarray, specification: str) -> np.ndarray:
        axis = specification.lstrip("+-")
        sign = -1.0 if specification.startswith("-") else 1.0
        return points[:, {"x": 0, "y": 1, "z": 2}[axis]] * sign
