from __future__ import annotations

import numpy as np
import pytest

from g1_bottle_reaction.game_vision.filters import (
    GameVisionPipeline,
    apply_visibility,
    distance_visibility,
    fov_visibility,
)
from g1_bottle_reaction.game_vision.frames import RgbdFrame


def test_fade_distance_mask_and_invalid_depth_fail_closed() -> None:
    depth = np.array(
        [[1.0, 1.5, 1.8, 2.5, 3.0, 0.0, -1.0, np.nan, np.inf]],
        dtype=np.float32,
    )
    mask = distance_visibility(depth, clear_m=1.5, max_m=2.5, mode="fade")
    assert mask.dtype == np.float32
    assert mask[0, 0] == pytest.approx(1.0)
    assert mask[0, 1] == pytest.approx(1.0)
    assert mask[0, 2] == pytest.approx(0.7)
    assert np.all(mask[0, 3:] == 0.0)


def test_hard_distance_mask_uses_max_distance() -> None:
    depth = np.array([[1.0, 2.49, 2.5, 8.0, 0.0]], dtype=np.float32)
    assert distance_visibility(depth, 1.5, 2.5, "hard").tolist() == [
        [1.0, 1.0, 0.0, 0.0, 0.0]
    ]


def test_visibility_darkens_bgr_without_changing_contract() -> None:
    bgr = np.full((1, 3, 3), 100, dtype=np.uint8)
    result = apply_visibility(bgr, np.array([[1.0, 0.7, 0.0]], dtype=np.float32))
    assert result.dtype == np.uint8
    assert result.shape == bgr.shape
    assert result[0, :, 0].tolist() == [100, 70, 0]


def test_fov_mask_keeps_center_and_hides_edges() -> None:
    mask = fov_visibility(101, 101, scale=0.55, feather=0.25)
    assert mask.shape == (101, 101)
    assert mask[50, 50] == pytest.approx(1.0)
    assert mask[50, 0] == pytest.approx(0.0)
    assert mask[0, 50] == pytest.approx(0.0)
    assert mask[50, 20] > mask[50, 10]
    assert np.allclose(mask, np.flip(mask, axis=0))
    assert np.allclose(mask, np.flip(mask, axis=1))


def test_pipeline_combines_depth_and_fov_and_caches_mask() -> None:
    bgr = np.full((21, 21, 3), 100, dtype=np.uint8)
    depth = np.full((21, 21), 1.0, dtype=np.float32)
    depth[10, 10] = 0.0
    frame = RgbdFrame(bgr, depth, 1.0)
    pipeline = GameVisionPipeline(
        clear_m=1.5,
        max_m=2.5,
        fog_mode="fade",
        fov_enabled=True,
        fov_scale=0.55,
        fov_feather=0.25,
    )
    first = pipeline.process(frame)
    cached = pipeline._fov_mask
    second = pipeline.process(frame)
    assert pipeline._fov_mask is cached
    assert first.depth_active and not first.fail_closed
    assert first.bgr[10, 10].tolist() == [0, 0, 0]
    assert first.bgr[10, 0].tolist() == [0, 0, 0]
    assert np.array_equal(first.bgr, second.bgr)


def test_missing_depth_preview_and_g1_fail_closed_modes() -> None:
    bgr = np.full((9, 9, 3), 100, dtype=np.uint8)
    frame = RgbdFrame(bgr, None, 1.0)
    pipeline = GameVisionPipeline(
        clear_m=1.5,
        max_m=2.5,
        fog_mode="fade",
        fov_enabled=False,
        fov_scale=0.55,
        fov_feather=0.25,
    )
    preview = pipeline.process(frame, missing_depth="preview")
    closed = pipeline.process(frame, missing_depth="black")
    assert np.array_equal(preview.bgr, bgr)
    assert not preview.depth_active and not preview.fail_closed
    assert np.count_nonzero(closed.bgr) == 0
    assert not closed.depth_active and closed.fail_closed


def test_aligned_shape_is_an_enforced_frame_invariant() -> None:
    with pytest.raises(RuntimeError, match="aligned"):
        RgbdFrame(
            np.zeros((4, 6, 3), dtype=np.uint8),
            np.zeros((3, 6), dtype=np.float32),
            1.0,
        ).validate()


def test_remote_safety_frame_must_match_game_dimensions() -> None:
    with pytest.raises(RuntimeError, match="Safety frame"):
        RgbdFrame(
            np.zeros((4, 6, 3), dtype=np.uint8),
            None,
            1.0,
            safety_bgr=np.zeros((3, 6, 3), dtype=np.uint8),
        ).validate()
