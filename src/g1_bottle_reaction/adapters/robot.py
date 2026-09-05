from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TrackingCommand:
    target_visible: bool
    active: bool
    desired_yaw_rad: float
    actual_yaw_rad: float
    strength: float
    game_state: str
    status: str
    timestamp: float
    last_seen_seconds: float | None

    @property
    def error_yaw_rad(self) -> float:
        return self.desired_yaw_rad - self.actual_yaw_rad


class RobotAdapter(ABC):
    @abstractmethod
    def play_motion(self, motion: str) -> None:
        """Start a named motion without blocking the vision loop."""

    def play_motion_timed(
        self,
        motion: str,
        *,
        timeline_start: float,
        timing_debug: bool = False,
    ) -> bool:
        """Start motion against a shared reaction clock when supported."""

        del timeline_start, timing_debug
        self.play_motion(motion)
        return True

    def set_attention_yaw(self, yaw_radians: float) -> None:
        """Preview continuous target attention; real-G1 adapters remain a safe no-op."""

    def apply_tracking(self, command: TrackingCommand) -> None:
        """Apply a continuous attention command without invoking a discrete motion."""

        self.set_attention_yaw(command.actual_yaw_rad)

    def wait_for_motion_complete(self, motion: str, timeout: float | None = None) -> bool:
        """Return true only when completion of the named motion is confirmed.

        Adapters without a reliable completion signal deliberately return false.
        Normal reaction behavior does not call this method unless an orchestrator
        explicitly requires physical completion confirmation.
        """

        del motion, timeout
        return False

    def close(self) -> None:
        """Release robot-side resources."""
