from __future__ import annotations

from dataclasses import replace
import math

import pytest

from g1_bottle_reaction.adapters.mock_robot import MockRobotAdapter
from g1_bottle_reaction.app import run_tracking_preview
from g1_bottle_reaction.stealth.models import GameState
from g1_bottle_reaction.stealth.tracking import (
    TargetTrackingController,
    calculate_target_yaw,
)
from test_stealth_game import observation


def test_center_mapping_deadzone_clamp_and_invert(app_config) -> None:
    config = app_config.stealth_game.tracking
    maximum = math.radians(config.max_preview_yaw_degrees)
    assert calculate_target_yaw(-1.0, config) == pytest.approx(-maximum)
    assert calculate_target_yaw(0.0, config) == 0.0
    assert calculate_target_yaw(1.0, config) == pytest.approx(maximum)
    assert calculate_target_yaw(0.05, config) == 0.0
    assert calculate_target_yaw(5.0, config) == pytest.approx(maximum)
    inverted = replace(config, invert_x=True)
    assert calculate_target_yaw(-1.0, inverted) == pytest.approx(maximum)
    assert calculate_target_yaw(1.0, inverted) == pytest.approx(-maximum)


def test_unaware_does_not_track_visible_target(app_config) -> None:
    controller = TargetTrackingController(
        app_config.stealth_game.tracking, MockRobotAdapter()
    )
    controller.update(observation(0.0, center_x=1.0), state=GameState.UNAWARE, now=0.0)
    command = controller.update(
        observation(0.5, center_x=1.0), state=GameState.UNAWARE, now=0.5
    )
    assert not command.active
    assert command.status == "OFF"
    assert command.desired_yaw_rad == 0.0
    assert command.actual_yaw_rad == 0.0


def test_suspicious_is_slower_than_alert(app_config) -> None:
    config = app_config.stealth_game.tracking
    slow = TargetTrackingController(config, MockRobotAdapter())
    fast = TargetTrackingController(config, MockRobotAdapter())
    slow.update(observation(0.0, center_x=1.0), state=GameState.SUSPICIOUS, now=0.0)
    fast.update(observation(0.0, center_x=1.0), state=GameState.ALERT, now=0.0)
    slow_command = slow.update(
        observation(0.2, center_x=1.0), state=GameState.SUSPICIOUS, now=0.2
    )
    fast_command = fast.update(
        observation(0.2, center_x=1.0), state=GameState.ALERT, now=0.2
    )
    assert 0 < slow_command.actual_yaw_rad < fast_command.actual_yaw_rad
    assert slow_command.strength < fast_command.strength


def test_smoothing_approaches_target_without_jump(app_config) -> None:
    controller = TargetTrackingController(
        app_config.stealth_game.tracking, MockRobotAdapter()
    )
    controller.update(observation(0.0, center_x=1.0), state=GameState.ALERT, now=0.0)
    first = controller.update(
        observation(0.1, center_x=1.0), state=GameState.ALERT, now=0.1
    )
    second = controller.update(
        observation(0.2, center_x=1.0), state=GameState.ALERT, now=0.2
    )
    assert 0 < first.actual_yaw_rad < second.actual_yaw_rad < second.desired_yaw_rad


def test_found_locks_target_and_game_over_ignores_new_input(app_config) -> None:
    controller = TargetTrackingController(
        app_config.stealth_game.tracking, MockRobotAdapter()
    )
    controller.update(observation(0.0, center_x=-0.8), state=GameState.ALERT, now=0.0)
    controller.update(observation(0.2, center_x=-0.8), state=GameState.ALERT, now=0.2)
    found = controller.update(
        observation(0.3, center_x=-0.8), state=GameState.FOUND, now=0.3
    )
    game_over = controller.update(
        observation(0.6, center_x=0.8), state=GameState.GAME_OVER, now=0.6
    )
    assert found.status == "LOCKED"
    assert found.desired_yaw_rad < 0
    assert game_over.status == "LOCKED"
    assert game_over.desired_yaw_rad == found.desired_yaw_rad
    assert game_over.actual_yaw_rad < 0


def test_lost_hold_then_recenter(app_config) -> None:
    controller = TargetTrackingController(
        app_config.stealth_game.tracking, MockRobotAdapter()
    )
    controller.update(observation(0.0, center_x=-0.8), state=GameState.SUSPICIOUS, now=0.0)
    visible = controller.update(
        observation(0.2, center_x=-0.8), state=GameState.SUSPICIOUS, now=0.2
    )
    held = controller.update(None, state=GameState.SUSPICIOUS, now=0.4)
    recentering = controller.update(None, state=GameState.SUSPICIOUS, now=0.8)
    later = controller.update(None, state=GameState.UNAWARE, now=1.3)
    assert held.status == "HOLD"
    assert held.desired_yaw_rad == visible.desired_yaw_rad
    assert held.last_seen_seconds == pytest.approx(0.2)
    assert recentering.status == "RECENTER"
    assert recentering.desired_yaw_rad == 0.0
    assert abs(later.actual_yaw_rad) < abs(recentering.actual_yaw_rad)


def test_exponential_smoothing_is_fps_independent(app_config) -> None:
    config = app_config.stealth_game.tracking
    one_step = TargetTrackingController(config, MockRobotAdapter())
    two_steps = TargetTrackingController(config, MockRobotAdapter())
    one_step.update(observation(0.0, center_x=1.0), state=GameState.ALERT, now=0.0)
    two_steps.update(observation(0.0, center_x=1.0), state=GameState.ALERT, now=0.0)
    one = one_step.update(
        observation(1.0, center_x=1.0), state=GameState.ALERT, now=1.0
    )
    two_steps.update(observation(0.5, center_x=1.0), state=GameState.ALERT, now=0.5)
    two = two_steps.update(
        observation(1.0, center_x=1.0), state=GameState.ALERT, now=1.0
    )
    assert one.actual_yaw_rad == pytest.approx(two.actual_yaw_rad)


def test_reset_clears_lock_and_last_seen_then_smoothly_recenters(app_config) -> None:
    robot = MockRobotAdapter()
    controller = TargetTrackingController(app_config.stealth_game.tracking, robot)
    controller.update(observation(0.0, center_x=1.0), state=GameState.ALERT, now=0.0)
    before = controller.update(
        observation(0.2, center_x=1.0), state=GameState.ALERT, now=0.2
    )
    reset = controller.reset(now=0.3)
    after = controller.update(None, state=GameState.UNAWARE, now=0.8)
    assert reset.desired_yaw_rad == 0.0
    assert reset.last_seen_seconds is None
    assert reset.status == "RECENTER"
    assert 0 < after.actual_yaw_rad < before.actual_yaw_rad
    immediate = controller.reset(now=0.9, immediate=True)
    assert immediate.actual_yaw_rad == 0.0
    assert robot.attention_yaws[-1] == 0.0


def test_tracking_only_preview_covers_left_center_right_without_gui(app_config) -> None:
    commands = run_tracking_preview(
        MockRobotAdapter(), app_config.stealth_game.tracking, realtime=False
    )
    segment_ends = [commands[index] for index in (49, 99, 149, 199, 249, 299)]
    assert segment_ends[0].actual_yaw_rad < segment_ends[1].actual_yaw_rad < 0
    assert abs(segment_ends[2].actual_yaw_rad) < abs(segment_ends[1].actual_yaw_rad)
    assert 0 < segment_ends[3].actual_yaw_rad < segment_ends[4].actual_yaw_rad
    assert abs(segment_ends[5].actual_yaw_rad) < abs(segment_ends[4].actual_yaw_rad)
