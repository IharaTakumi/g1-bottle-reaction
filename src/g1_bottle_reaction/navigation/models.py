from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math


class NavigationState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    IDLE = "IDLE"
    PATROLLING = "PATROLLING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class Pose2D:
    x: float
    y: float
    yaw_rad: float
    frame_id: str
    source_timestamp: float

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (self.x, self.y, self.yaw_rad, self.source_timestamp)
        ):
            raise ValueError("Pose2D values must be finite")
        if not self.frame_id.strip():
            raise ValueError("Pose2D frame_id cannot be empty")


@dataclass(frozen=True, slots=True)
class NavigationStatus:
    state: NavigationState
    connected: bool
    active_route_id: str | None = None
    pause_reason: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class NavigationCommandRecord:
    action: str
    timestamp: float
    route_id: str | None = None
    pause_reason: str | None = None
    command_id: str | None = None
