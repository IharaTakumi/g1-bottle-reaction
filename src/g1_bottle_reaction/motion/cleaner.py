from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import yaml

from .asset import MotionAsset
from .g1_joints import (
    G1_UPPER_BODY_JOINT_LIMITS,
    UPPER_BODY_JOINT_NAMES,
    motion_group,
)


@dataclass(frozen=True, slots=True)
class MotionCleanerConfig:
    output_fps: float
    smoothing_time_constant_s: float
    blend_in_seconds: float
    blend_out_seconds: float
    joint_margin_rad: float
    waist_scales: Mapping[str, float]
    arm_scale: float
    max_velocity_rad_s: Mapping[str, float]
    max_offset_rad: Mapping[str, float]

    def validate(self) -> None:
        positive = (
            self.output_fps,
            self.smoothing_time_constant_s,
            *self.max_velocity_rad_s.values(),
            *self.max_offset_rad.values(),
        )
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ValueError("Motion cleaner rates and limits must be positive")
        nonnegative = (
            self.blend_in_seconds,
            self.blend_out_seconds,
            self.joint_margin_rad,
            self.arm_scale,
            *self.waist_scales.values(),
        )
        if any(not math.isfinite(value) or value < 0 for value in nonnegative):
            raise ValueError("Motion cleaner scales and blend settings cannot be negative")
        if set(self.waist_scales) != {"yaw", "roll", "pitch"}:
            raise ValueError("waist scaling must define yaw, roll, and pitch")
        required_groups = {"waist", "shoulder", "elbow", "wrist"}
        if set(self.max_velocity_rad_s) != required_groups:
            raise ValueError("velocity limits must define all upper-body groups")
        if set(self.max_offset_rad) != required_groups:
            raise ValueError("offset limits must define all upper-body groups")


@dataclass(frozen=True, slots=True)
class MotionCleaningReport:
    source_frames: int
    output_frames: int
    source_fps: float
    output_fps: float
    source_limit_clips: int
    offset_limit_clips: int


def default_motion_cleaner_config_path() -> Path:
    repository_path = Path(__file__).resolve().parents[3] / "config" / "reaction_motion.yaml"
    if repository_path.is_file():
        return repository_path
    return Path(__file__).resolve().parents[1] / "config" / "reaction_motion.yaml"


def load_motion_cleaner_config(
    path: str | Path | None = None,
) -> MotionCleanerConfig:
    source = Path(path) if path is not None else default_motion_cleaner_config_path()
    with source.open("r", encoding="utf-8") as stream:
        raw: dict[str, Any] = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ValueError("Reaction motion config must be a YAML mapping")
    if int(raw.get("format_version", -1)) != 1:
        raise ValueError("Unsupported reaction motion config format_version")
    try:
        scaling = raw["scaling"]
        safety = raw["safety"]
        blend = raw["blend"]
        config = MotionCleanerConfig(
            output_fps=float(raw["output_fps"]),
            smoothing_time_constant_s=float(
                raw["smoothing"]["time_constant_seconds"]
            ),
            blend_in_seconds=float(blend["in_seconds"]),
            blend_out_seconds=float(blend["out_seconds"]),
            joint_margin_rad=float(safety["joint_margin_rad"]),
            waist_scales={
                key: float(value) for key, value in scaling["waist"].items()
            },
            arm_scale=float(scaling["arms"]),
            max_velocity_rad_s={
                key: float(value)
                for key, value in safety["max_velocity_rad_s"].items()
            },
            max_offset_rad={
                key: float(value)
                for key, value in safety["max_offset_rad"].items()
            },
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Invalid reaction motion config structure: {exc}") from exc
    config.validate()
    return config


def clean_upper_body_motion(
    positions: np.ndarray,
    joint_names: tuple[str, ...] | list[str],
    *,
    source_fps: float,
    name: str,
    config: MotionCleanerConfig,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[MotionAsset, MotionCleaningReport]:
    config.validate()
    source = np.asarray(positions, dtype=np.float64)
    if source.ndim != 2 or source.shape[0] < 2:
        raise ValueError("GMR positions must have shape (frames >= 2, joints)")
    if source.shape[1] != len(joint_names):
        raise ValueError("GMR positions width must match joint_names")
    if not math.isfinite(source_fps) or source_fps <= 0:
        raise ValueError("GMR fps must be positive and finite")
    if not np.isfinite(source).all():
        raise ValueError("GMR positions contain non-finite values")
    names = tuple(str(item) for item in joint_names)
    if len(set(names)) != len(names):
        raise ValueError("GMR joint names must be unique")
    by_name = {joint_name: index for index, joint_name in enumerate(names)}
    missing = [name for name in UPPER_BODY_JOINT_NAMES if name not in by_name]
    if missing:
        raise ValueError("GMR output is missing G1 joints: " + ", ".join(missing))

    upper = source[:, [by_name[name] for name in UPPER_BODY_JOINT_NAMES]].copy()
    source_limit_clips = 0
    for column, joint_name in enumerate(UPPER_BODY_JOINT_NAMES):
        low, high = G1_UPPER_BODY_JOINT_LIMITS[joint_name]
        low += config.joint_margin_rad
        high -= config.joint_margin_rad
        if low >= high:
            raise ValueError(f"Joint margin is too large for {joint_name}")
        clipped = np.clip(upper[:, column], low, high)
        source_limit_clips += int(np.count_nonzero(clipped != upper[:, column]))
        upper[:, column] = clipped

    reference = upper[0].copy()
    relative = upper - reference
    for column, joint_name in enumerate(UPPER_BODY_JOINT_NAMES):
        relative[:, column] *= _scale_for_joint(config, joint_name)

    smoothed = _zero_phase_ema(
        relative,
        fps=source_fps,
        time_constant_s=config.smoothing_time_constant_s,
    )
    resampled = _resample(smoothed, source_fps, config.output_fps)
    blended = _add_blends(
        resampled,
        fps=config.output_fps,
        blend_in_seconds=config.blend_in_seconds,
        blend_out_seconds=config.blend_out_seconds,
    )

    offset_limit_clips = 0
    for column, joint_name in enumerate(UPPER_BODY_JOINT_NAMES):
        group = motion_group(joint_name)
        model_low, model_high = G1_UPPER_BODY_JOINT_LIMITS[joint_name]
        low = max(
            model_low + config.joint_margin_rad - reference[column],
            -config.max_offset_rad[group],
        )
        high = min(
            model_high - config.joint_margin_rad - reference[column],
            config.max_offset_rad[group],
        )
        clipped = np.clip(blended[:, column], low, high)
        offset_limit_clips += int(np.count_nonzero(clipped != blended[:, column]))
        blended[:, column] = clipped

    velocity_limits = np.asarray(
        [config.max_velocity_rad_s[motion_group(name)] for name in UPPER_BODY_JOINT_NAMES]
    )
    safe = _limit_velocity_with_fixed_endpoints(
        blended,
        velocity_limits=velocity_limits,
        fps=config.output_fps,
    )
    safe[0] = 0.0
    safe[-1] = 0.0

    asset_metadata = dict(metadata or {})
    asset_metadata.update(
        {
            "source_fps": float(source_fps),
            "source_frames": int(source.shape[0]),
            "source_reference_positions_rad": {
                joint_name: float(value)
                for joint_name, value in zip(UPPER_BODY_JOINT_NAMES, reference)
            },
            "cleaner": {
                "smoothing": "zero_phase_exponential",
                "smoothing_time_constant_s": config.smoothing_time_constant_s,
                "blend_in_seconds": config.blend_in_seconds,
                "blend_out_seconds": config.blend_out_seconds,
                "joint_margin_rad": config.joint_margin_rad,
                "waist_scales": dict(config.waist_scales),
                "arm_scale": config.arm_scale,
                "max_velocity_rad_s": dict(config.max_velocity_rad_s),
                "max_offset_rad": dict(config.max_offset_rad),
                "source_limit_clips": source_limit_clips,
                "offset_limit_clips": offset_limit_clips,
            },
        }
    )
    asset = MotionAsset(
        name=name,
        fps=config.output_fps,
        joint_names=UPPER_BODY_JOINT_NAMES,
        positions=safe.astype(np.float32),
        metadata=asset_metadata,
    )
    report = MotionCleaningReport(
        source_frames=source.shape[0],
        output_frames=safe.shape[0],
        source_fps=float(source_fps),
        output_fps=config.output_fps,
        source_limit_clips=source_limit_clips,
        offset_limit_clips=offset_limit_clips,
    )
    return asset, report


def _scale_for_joint(config: MotionCleanerConfig, joint_name: str) -> float:
    if joint_name == "waist_yaw_joint":
        return config.waist_scales["yaw"]
    if joint_name == "waist_roll_joint":
        return config.waist_scales["roll"]
    if joint_name == "waist_pitch_joint":
        return config.waist_scales["pitch"]
    return config.arm_scale


def _zero_phase_ema(
    values: np.ndarray, *, fps: float, time_constant_s: float
) -> np.ndarray:
    alpha = 1.0 - math.exp(-1.0 / (fps * time_constant_s))

    def pass_once(samples: np.ndarray) -> np.ndarray:
        result = samples.copy()
        for index in range(1, len(result)):
            result[index] = alpha * samples[index] + (1.0 - alpha) * result[index - 1]
        return result

    forward = pass_once(values)
    return pass_once(forward[::-1])[::-1]


def _resample(values: np.ndarray, source_fps: float, target_fps: float) -> np.ndarray:
    duration = (len(values) - 1) / source_fps
    frame_count = max(2, int(round(duration * target_fps)) + 1)
    source_times = np.arange(len(values), dtype=np.float64) / source_fps
    target_times = np.linspace(0.0, duration, frame_count, dtype=np.float64)
    return np.column_stack(
        [
            np.interp(target_times, source_times, values[:, column])
            for column in range(values.shape[1])
        ]
    )


def _add_blends(
    values: np.ndarray,
    *,
    fps: float,
    blend_in_seconds: float,
    blend_out_seconds: float,
) -> np.ndarray:
    in_frames = int(round(blend_in_seconds * fps))
    out_frames = int(round(blend_out_seconds * fps))
    parts: list[np.ndarray] = []
    if in_frames:
        weights = _smoothstep(np.linspace(0.0, 1.0, in_frames + 1)[:-1])
        parts.append(weights[:, None] * values[0])
    parts.append(values)
    if out_frames:
        weights = _smoothstep(np.linspace(1.0, 0.0, out_frames + 1)[1:])
        parts.append(weights[:, None] * values[-1])
    return np.concatenate(parts, axis=0)


def _smoothstep(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, 0.0, 1.0)
    return clipped * clipped * (3.0 - 2.0 * clipped)


def _limit_velocity_with_fixed_endpoints(
    values: np.ndarray,
    *,
    velocity_limits: np.ndarray,
    fps: float,
) -> np.ndarray:
    result = values.copy()
    result[0] = 0.0
    result[-1] = 0.0
    max_step = velocity_limits / fps
    for _ in range(max(4, len(result) * 2)):
        previous = result.copy()
        for index in range(1, len(result) - 1):
            result[index] = np.clip(
                result[index],
                result[index - 1] - max_step,
                result[index - 1] + max_step,
            )
        for index in range(len(result) - 2, 0, -1):
            result[index] = np.clip(
                result[index],
                result[index + 1] - max_step,
                result[index + 1] + max_step,
            )
        if np.allclose(result, previous, rtol=0.0, atol=1e-12):
            break
    if np.any(np.abs(np.diff(result, axis=0)) > max_step + 1e-9):
        raise RuntimeError("Could not satisfy configured joint velocity limits")
    return result
