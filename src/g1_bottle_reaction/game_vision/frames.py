from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class RgbdFrame:
    """One BGR frame and an optional depth map aligned to its pixels.

    ``depth_m`` is expressed in metres. Zero, negative, NaN and infinity are
    invalid readings and are deliberately treated as invisible by the game
    filter.
    """

    bgr: np.ndarray
    depth_m: np.ndarray | None
    captured_at: float
    sequence: int = 0
    safety_bgr: np.ndarray | None = None

    def validate(self) -> RgbdFrame:
        if not isinstance(self.bgr, np.ndarray):
            raise RuntimeError("RGB frame must be a numpy array")
        if self.bgr.dtype != np.uint8 or self.bgr.ndim != 3 or self.bgr.shape[2] != 3:
            raise RuntimeError("RGB frame must be an HxWx3 uint8 BGR array")
        if self.bgr.shape[0] == 0 or self.bgr.shape[1] == 0:
            raise RuntimeError("RGB frame dimensions must be non-zero")
        if self.depth_m is not None:
            if not isinstance(self.depth_m, np.ndarray) or self.depth_m.ndim != 2:
                raise RuntimeError("Depth frame must be a two-dimensional numpy array")
            if self.depth_m.shape != self.bgr.shape[:2]:
                raise RuntimeError(
                    "Depth must already be aligned to color and have the same height and width"
                )
            if not np.issubdtype(self.depth_m.dtype, np.number) or np.issubdtype(
                self.depth_m.dtype, np.complexfloating
            ):
                raise RuntimeError("Depth frame must contain numeric metre values")
        if self.safety_bgr is not None:
            if (
                not isinstance(self.safety_bgr, np.ndarray)
                or self.safety_bgr.dtype != np.uint8
                or self.safety_bgr.ndim != 3
                or self.safety_bgr.shape[2] != 3
            ):
                raise RuntimeError("Safety frame must be an HxWx3 uint8 BGR array")
            if self.safety_bgr.shape != self.bgr.shape:
                raise RuntimeError("Safety frame must have the same dimensions as RGB")
        return self
