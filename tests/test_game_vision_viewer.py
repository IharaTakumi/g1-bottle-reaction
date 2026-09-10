from __future__ import annotations

import numpy as np

from g1_bottle_reaction.game_vision.viewer import HudState, compose_view, draw_hud


def test_game_and_safety_modes_select_expected_pixels() -> None:
    game = np.zeros((60, 80, 3), dtype=np.uint8)
    safety = np.full((60, 80, 3), 200, dtype=np.uint8)
    assert np.array_equal(
        compose_view(game, safety, mode="game", safety_inset_scale=0.25), game
    )
    assert np.array_equal(
        compose_view(game, safety, mode="safety", safety_inset_scale=0.25), safety
    )


def test_both_mode_puts_unfiltered_safety_inset_over_game() -> None:
    game = np.zeros((100, 200, 3), dtype=np.uint8)
    safety = np.full((100, 200, 3), 220, dtype=np.uint8)
    result = compose_view(game, safety, mode="both", safety_inset_scale=0.25)
    assert result.shape == game.shape
    assert result[50, 20].tolist() == [0, 0, 0]
    assert result[10, 150].tolist() == [220, 220, 220]
    assert np.any(result[:, :, 2] > result[:, :, 0])  # red inset border


def test_hud_preserves_frame_contract_for_depth_and_fail_closed_states() -> None:
    frame = np.zeros((120, 240, 3), dtype=np.uint8)
    state = HudState(
        fps=29.5,
        clear_m=1.5,
        max_m=2.5,
        fog_mode="fade",
        fov_scale=0.55,
        preset="normal",
        depth_active=True,
        fail_closed=False,
    )
    depth_hud = draw_hud(frame, state, mode="game")
    closed_hud = draw_hud(
        frame,
        HudState(
            fps=0.0,
            clear_m=1.5,
            max_m=2.5,
            fog_mode="fade",
            fov_scale=0.55,
            preset="normal",
            depth_active=False,
            fail_closed=True,
        ),
        mode="safety",
    )
    assert depth_hud.shape == frame.shape and depth_hud.dtype == np.uint8
    assert closed_hud.shape == frame.shape and closed_hud.dtype == np.uint8
    assert np.count_nonzero(depth_hud) > 0
    assert np.count_nonzero(closed_hud) > 0


def test_remote_status_does_not_overwrite_sender_range_hud() -> None:
    frame = np.zeros((120, 240, 3), dtype=np.uint8)
    frame[-25:, :] = (17, 33, 49)
    state = HudState(
        fps=28.0,
        clear_m=1.5,
        max_m=2.5,
        fog_mode="fade",
        fov_scale=0.55,
        preset="normal",
        depth_active=False,
        fail_closed=False,
        prefiltered=True,
    )
    result = draw_hud(frame, state, mode="game")
    assert np.array_equal(result[-25:, :], frame[-25:, :])
