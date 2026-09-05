from __future__ import annotations

from dataclasses import dataclass, field

from .robot import RobotAdapter


@dataclass(slots=True)
class MockRobotAdapter(RobotAdapter):
    motions: list[str] = field(default_factory=list)
    attention_yaws: list[float] = field(default_factory=list)

    def play_motion(self, motion: str) -> None:
        self.motions.append(motion)
        print(f"[MOTION] {motion}", flush=True)

    def set_attention_yaw(self, yaw_radians: float) -> None:
        self.attention_yaws.append(yaw_radians)

    def wait_for_motion_complete(self, motion: str, timeout: float | None = None) -> bool:
        del timeout
        return motion in self.motions
