from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import numpy as np

MOTION_FORMAT_VERSION = 1
POSITION_MODE = "relative_to_runtime_start"


@dataclass(frozen=True, slots=True)
class MotionAsset:
    """Runtime-independent upper-body reaction asset."""

    name: str
    fps: float
    joint_names: tuple[str, ...]
    positions: np.ndarray
    position_mode: str = POSITION_MODE
    metadata: Mapping[str, Any] = field(default_factory=dict)
    format_version: int = MOTION_FORMAT_VERSION

    def __post_init__(self) -> None:
        positions = np.asarray(self.positions)
        joint_names = tuple(self.joint_names)
        if not isinstance(self.name, str):
            raise ValueError("Motion name must be a string")
        if self.format_version != MOTION_FORMAT_VERSION:
            raise ValueError(
                f"Unsupported motion format version: {self.format_version}"
            )
        if not self.name.strip():
            raise ValueError("Motion name cannot be empty")
        if not math.isfinite(self.fps) or self.fps <= 0:
            raise ValueError("Motion fps must be positive and finite")
        if self.position_mode != POSITION_MODE:
            raise ValueError(f"Unsupported position mode: {self.position_mode}")
        if not joint_names or any(
            not isinstance(name, str) or not name for name in joint_names
        ):
            raise ValueError("Motion joint names must be non-empty strings")
        if len(set(joint_names)) != len(joint_names):
            raise ValueError("Motion joint names must be unique")
        if positions.ndim != 2 or positions.shape[0] < 2:
            raise ValueError("Motion positions must have shape (frames >= 2, joints)")
        if positions.shape[1] != len(joint_names):
            raise ValueError("Motion positions width must match joint_names")
        if not np.issubdtype(positions.dtype, np.number) or not np.isrealobj(positions):
            raise ValueError("Motion positions must be real numeric values")
        if not np.isfinite(positions).all():
            raise ValueError("Motion positions must be finite")
        try:
            json.dumps(dict(self.metadata), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Motion metadata must be JSON serializable") from exc
        object.__setattr__(self, "joint_names", joint_names)
        object.__setattr__(self, "positions", positions.astype(np.float64, copy=False))

    @property
    def duration(self) -> float:
        return (self.positions.shape[0] - 1) / self.fps


def save_motion_asset(asset: MotionAsset, path: str | Path) -> Path:
    """Atomically save an asset without pickle/object arrays."""

    asset = MotionAsset(
        name=asset.name,
        fps=float(asset.fps),
        joint_names=tuple(asset.joint_names),
        positions=asset.positions,
        position_mode=asset.position_mode,
        metadata=dict(asset.metadata),
        format_version=asset.format_version,
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b", suffix=".npz", dir=target.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            np.savez_compressed(
                stream,
                format_version=np.asarray(asset.format_version, dtype=np.int64),
                name=np.asarray(asset.name),
                fps=np.asarray(asset.fps, dtype=np.float64),
                joint_names=np.asarray(asset.joint_names),
                positions=np.asarray(asset.positions, dtype=np.float32),
                duration=np.asarray(asset.duration, dtype=np.float64),
                position_mode=np.asarray(asset.position_mode),
                metadata_json=np.asarray(
                    json.dumps(
                        dict(asset.metadata), ensure_ascii=False, sort_keys=True
                    )
                ),
            )
        os.replace(temporary, target)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return target


def load_motion_asset(path: str | Path) -> MotionAsset:
    source = Path(path)
    try:
        with np.load(source, allow_pickle=False) as archive:
            required = {
                "format_version",
                "name",
                "fps",
                "joint_names",
                "positions",
                "duration",
                "position_mode",
                "metadata_json",
            }
            missing = required - set(archive.files)
            if missing:
                raise ValueError(
                    "Motion asset is missing fields: " + ", ".join(sorted(missing))
                )
            asset = MotionAsset(
                name=str(archive["name"].item()),
                fps=float(archive["fps"].item()),
                joint_names=tuple(str(item) for item in archive["joint_names"].tolist()),
                positions=np.asarray(archive["positions"], dtype=np.float64),
                position_mode=str(archive["position_mode"].item()),
                metadata=json.loads(str(archive["metadata_json"].item())),
                format_version=int(archive["format_version"].item()),
            )
            stored_duration = float(archive["duration"].item())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not load motion asset {source}: {exc}") from exc
    if not math.isclose(stored_duration, asset.duration, abs_tol=1e-6):
        raise ValueError("Motion duration does not match frames and fps")
    return asset
