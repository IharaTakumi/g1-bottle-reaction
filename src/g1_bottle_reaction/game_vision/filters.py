from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .frames import RgbdFrame


def distance_visibility(
    depth_m: np.ndarray,
    clear_m: float = 1.5,
    max_m: float = 2.5,
    mode: str = "fade",
) -> np.ndarray:
    """Return a per-pixel visibility mask in [0, 1].

    Invalid readings always map to zero. In ``fade`` mode visibility is one
    through ``clear_m`` and falls linearly to zero at ``max_m``. ``hard`` mode
    keeps valid pixels below ``max_m`` and hides everything else.
    """

    if not isinstance(depth_m, np.ndarray) or depth_m.ndim != 2:
        raise ValueError("depth_m must be a two-dimensional numpy array")
    if not math.isfinite(clear_m) or not math.isfinite(max_m):
        raise ValueError("distance limits must be finite")
    if clear_m < 0 or max_m <= clear_m:
        raise ValueError("distance limits must satisfy 0 <= clear_m < max_m")
    if mode not in {"fade", "hard"}:
        raise ValueError("fog mode must be fade or hard")

    depth = depth_m.astype(np.float32, copy=False)
    valid = np.isfinite(depth) & (depth > 0)
    if mode == "hard":
        return (valid & (depth < max_m)).astype(np.float32)

    visibility = np.zeros(depth.shape, dtype=np.float32)
    visibility[valid & (depth <= clear_m)] = 1.0
    fading = valid & (depth > clear_m) & (depth < max_m)
    visibility[fading] = (max_m - depth[fading]) / (max_m - clear_m)
    return visibility


def fov_visibility(
    height: int,
    width: int,
    scale: float = 0.55,
    feather: float = 0.25,
    enabled: bool = True,
) -> np.ndarray:
    """Create an elliptical vignette with a fully visible central area.

    ``scale`` is the fraction of horizontal/vertical image span that remains
    fully visible through the centre. ``feather`` controls the normalized
    transition toward black.
    """

    if height <= 0 or width <= 0:
        raise ValueError("FOV mask dimensions must be positive")
    if not 0 < scale <= 1 or not 0 < feather <= 1:
        raise ValueError("FOV scale and feather must be in (0, 1]")
    if not enabled or scale >= 1:
        return np.ones((height, width), dtype=np.float32)

    x = np.abs((np.arange(width, dtype=np.float32) + 0.5 - width / 2) / (width / 2))
    y = np.abs((np.arange(height, dtype=np.float32) + 0.5 - height / 2) / (height / 2))
    radius = np.sqrt(y[:, None] ** 2 + x[None, :] ** 2)
    outer = min(1.0, scale + feather)
    if outer <= scale:
        return (radius <= scale).astype(np.float32)
    t = np.clip((radius - scale) / (outer - scale), 0.0, 1.0)
    smooth = t * t * (3.0 - 2.0 * t)
    return np.ascontiguousarray(1.0 - smooth, dtype=np.float32)


def apply_visibility(bgr: np.ndarray, visibility: np.ndarray) -> np.ndarray:
    if bgr.dtype != np.uint8 or bgr.ndim != 3 or bgr.shape[2] != 3:
        raise ValueError("bgr must be an HxWx3 uint8 array")
    if visibility.shape != bgr.shape[:2]:
        raise ValueError("visibility mask must match the BGR frame dimensions")
    clipped = np.clip(visibility, 0.0, 1.0)
    return np.clip(
        bgr.astype(np.float32) * clipped[:, :, None], 0, 255
    ).astype(np.uint8)


@dataclass(frozen=True, slots=True)
class FilterResult:
    bgr: np.ndarray
    depth_active: bool
    fail_closed: bool


class GameVisionPipeline:
    """Small RGBD -> distance fog -> FOV pipeline with a cached FOV mask."""

    def __init__(
        self,
        *,
        clear_m: float,
        max_m: float,
        fog_mode: str,
        fov_enabled: bool,
        fov_scale: float,
        fov_feather: float,
    ) -> None:
        self.clear_m = clear_m
        self.max_m = max_m
        self.fog_mode = fog_mode
        self.fov_enabled = fov_enabled
        self.fov_scale = fov_scale
        self.fov_feather = fov_feather
        self._fov_key: tuple[int, int, float, float, bool] | None = None
        self._fov_mask: np.ndarray | None = None
        self._validate()

    def set_distance(self, clear_m: float, max_m: float) -> None:
        self.clear_m = clear_m
        self.max_m = max_m
        self._validate()

    def adjust_max_distance(self, delta_m: float) -> None:
        self.max_m = max(self.clear_m + 0.1, self.max_m + delta_m)

    def process(
        self,
        frame: RgbdFrame,
        *,
        missing_depth: str = "preview",
    ) -> FilterResult:
        frame.validate()
        if missing_depth not in {"preview", "black"}:
            raise ValueError("missing_depth must be preview or black")
        height, width = frame.bgr.shape[:2]
        mask = self._get_fov_mask(height, width)
        if frame.depth_m is None:
            if missing_depth == "black":
                return FilterResult(np.zeros_like(frame.bgr), False, True)
            return FilterResult(apply_visibility(frame.bgr, mask), False, False)

        depth_mask = distance_visibility(
            frame.depth_m,
            clear_m=self.clear_m,
            max_m=self.max_m,
            mode=self.fog_mode,
        )
        return FilterResult(apply_visibility(frame.bgr, depth_mask * mask), True, False)

    def _get_fov_mask(self, height: int, width: int) -> np.ndarray:
        key = (height, width, self.fov_scale, self.fov_feather, self.fov_enabled)
        if self._fov_key != key:
            self._fov_mask = fov_visibility(
                height,
                width,
                self.fov_scale,
                self.fov_feather,
                self.fov_enabled,
            )
            self._fov_key = key
        assert self._fov_mask is not None
        return self._fov_mask

    def _validate(self) -> None:
        if not math.isfinite(self.clear_m) or not math.isfinite(self.max_m):
            raise ValueError("distance limits must be finite")
        if self.clear_m < 0 or self.max_m <= self.clear_m:
            raise ValueError("distance limits must satisfy 0 <= clear_m < max_m")
        if self.fog_mode not in {"fade", "hard"}:
            raise ValueError("fog mode must be fade or hard")
        if not math.isfinite(self.fov_scale) or not math.isfinite(self.fov_feather):
            raise ValueError("FOV scale and feather must be finite")
        if not 0 < self.fov_scale <= 1 or not 0 < self.fov_feather <= 1:
            raise ValueError("FOV scale and feather must be in (0, 1]")
