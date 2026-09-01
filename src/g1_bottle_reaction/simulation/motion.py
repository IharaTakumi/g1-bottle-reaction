from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
from typing import Callable, Mapping

import yaml


Pose = dict[str, float]
JointLimits = Mapping[str, tuple[float, float] | None]


@dataclass(frozen=True, slots=True)
class MotionKeyframe:
    time_seconds: float
    pose: Pose


@dataclass(frozen=True, slots=True)
class MotionAnimation:
    name: str
    keyframes: tuple[MotionKeyframe, ...]

    @property
    def duration_seconds(self) -> float:
        return self.keyframes[-1].time_seconds

    def sample(self, time_seconds: float, *, baseline: Mapping[str, float]) -> Pose:
        if time_seconds <= self.keyframes[0].time_seconds:
            return _expanded_pose(baseline, self.keyframes[0].pose)
        if time_seconds >= self.duration_seconds:
            return _expanded_pose(baseline, self.keyframes[-1].pose)

        for left, right in zip(self.keyframes, self.keyframes[1:]):
            if time_seconds <= right.time_seconds:
                span = right.time_seconds - left.time_seconds
                fraction = (time_seconds - left.time_seconds) / span
                eased = _smoothstep(fraction)
                names = baseline.keys() | left.pose.keys() | right.pose.keys()
                return {
                    name: left.pose.get(name, baseline.get(name, 0.0))
                    + (
                        right.pose.get(name, baseline.get(name, 0.0))
                        - left.pose.get(name, baseline.get(name, 0.0))
                    )
                    * eased
                    for name in names
                }
        return _expanded_pose(baseline, self.keyframes[-1].pose)


@dataclass(frozen=True, slots=True)
class MotionLibrary:
    fps: int
    motions: dict[str, MotionAnimation]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self.motions)

    @property
    def stand_pose(self) -> Pose:
        return self.motions["stand"].sample(0.0, baseline={})

    def animation(self, name: str) -> MotionAnimation:
        try:
            return self.motions[name]
        except KeyError as exc:
            available = ", ".join(self.names)
            raise KeyError(f"Unknown motion '{name}'. Available: {available}") from exc


class AnimationState(str, Enum):
    READY = "READY"
    PLAYING = "PLAYING"


class AnimationPlayer:
    """Clock-driven animation state independent of MuJoCo and GUI code."""

    def __init__(self, library: MotionLibrary) -> None:
        self.library = library
        self.state = AnimationState.READY
        self.current_motion: str | None = None
        self._started_at = 0.0

    def start(self, motion: str, *, now: float) -> None:
        self.library.animation(motion)
        self.current_motion = motion
        self._started_at = now
        self.state = AnimationState.PLAYING

    def update(self, *, now: float) -> tuple[Pose, str | None]:
        if self.current_motion is None:
            return dict(self.library.stand_pose), None
        animation = self.library.animation(self.current_motion)
        elapsed = max(0.0, now - self._started_at)
        if elapsed >= animation.duration_seconds:
            completed = self.current_motion
            self.current_motion = None
            self.state = AnimationState.READY
            return dict(self.library.stand_pose), completed
        return animation.sample(elapsed, baseline=self.library.stand_pose), None


def default_motion_config_path() -> Path:
    repository_config = (
        Path(__file__).resolve().parents[3] / "config" / "mujoco_motions.yaml"
    )
    if repository_config.is_file():
        return repository_config
    return Path(__file__).resolve().parents[1] / "config" / "mujoco_motions.yaml"


def load_motion_library(path: str | Path | None = None) -> MotionLibrary:
    config_path = Path(path) if path is not None else default_motion_config_path()
    with config_path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    fps = int(raw.get("fps", 60))
    if not 1 <= fps <= 240:
        raise ValueError("motion fps must be between 1 and 240")
    motions: dict[str, MotionAnimation] = {}
    for name, motion_raw in raw["motions"].items():
        keyframes = tuple(
            MotionKeyframe(
                time_seconds=float(item["time"]),
                pose={joint: float(value) for joint, value in item["pose"].items()},
            )
            for item in motion_raw["keyframes"]
        )
        _validate_keyframes(str(name), keyframes)
        motions[str(name)] = MotionAnimation(str(name), keyframes)
    if "stand" not in motions:
        raise ValueError("motion config must define stand")
    return MotionLibrary(fps=fps, motions=motions)


def sanitize_pose(
    pose: Mapping[str, float],
    joint_limits: JointLimits,
    *,
    warn: Callable[[str], None],
) -> Pose:
    """Ignore unknown joints and clamp limited joints to the official model range."""
    safe: Pose = {}
    for name, value in pose.items():
        if name not in joint_limits:
            warn(f"Motion config joint '{name}' does not exist in the MuJoCo model; ignored")
            continue
        limits = joint_limits[name]
        safe_value = float(value)
        if limits is not None:
            low, high = limits
            clamped = min(max(safe_value, low), high)
            if clamped != safe_value:
                warn(
                    f"Motion joint '{name}' value {safe_value:.4f} was clamped "
                    f"to [{low:.4f}, {high:.4f}]"
                )
            safe_value = clamped
        safe[name] = safe_value
    return safe


def _expanded_pose(baseline: Mapping[str, float], pose: Mapping[str, float]) -> Pose:
    expanded = dict(baseline)
    expanded.update(pose)
    return expanded


def _smoothstep(value: float) -> float:
    value = min(max(value, 0.0), 1.0)
    return value * value * (3.0 - 2.0 * value)


def _validate_keyframes(
    name: str, keyframes: tuple[MotionKeyframe, ...]
) -> None:
    if not keyframes:
        raise ValueError(f"motion '{name}' has no keyframes")
    previous = -1.0
    for keyframe in keyframes:
        if not math.isfinite(keyframe.time_seconds) or keyframe.time_seconds < 0:
            raise ValueError(f"motion '{name}' has an invalid keyframe time")
        if keyframe.time_seconds <= previous:
            raise ValueError(f"motion '{name}' keyframe times must be strictly increasing")
        if any(not math.isfinite(value) for value in keyframe.pose.values()):
            raise ValueError(f"motion '{name}' contains a non-finite joint angle")
        previous = keyframe.time_seconds
