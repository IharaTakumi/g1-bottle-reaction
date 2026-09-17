from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .models import LocalObstacleSnapshot, OdomSample, WanderConfig
from .perception import LocalObstaclePerception


def read_replay(path: Path, config: WanderConfig) -> Iterator[tuple[float, LocalObstacleSnapshot, OdomSample]]:
    perception = LocalObstaclePerception(
        config.point_cloud,
        blocked_distance_m=config.policy.blocked_distance_m,
    )
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            timestamp = float(value["timestamp"])
            odom = value["odom"]
            pose = OdomSample(
                x=float(odom["x"]),
                y=float(odom["y"]),
                yaw=float(odom["yaw"]),
                timestamp=timestamp,
            )
            if "points" in value:
                snapshot = perception.snapshot(value["points"], timestamp=timestamp)
            elif "obstacle_snapshot" in value:
                snapshot = LocalObstacleSnapshot.from_clearances(
                    timestamp,
                    value["obstacle_snapshot"],
                    blocked_distance_m=config.policy.blocked_distance_m,
                )
            else:
                raise ValueError(f"Replay line {line_number} has no points or obstacle_snapshot")
            yield timestamp, snapshot, pose
