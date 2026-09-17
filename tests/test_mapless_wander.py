from __future__ import annotations

from dataclasses import replace
import math

import pytest

from g1_bottle_reaction.navigation.wander import (
    LocalObstaclePerception,
    LocalObstacleSnapshot,
    MaplessWanderCore,
    OdomSample,
    SafetyState,
    WanderAction,
)


NAMES = ("left", "front_left", "front", "front_right", "right")


def snapshot(config, values, *, timestamp=10.0, valid=True):
    return LocalObstacleSnapshot.from_clearances(
        timestamp,
        dict(zip(NAMES, values)),
        blocked_distance_m=config.wander.policy.blocked_distance_m,
        valid=valid,
        invalid_reason=None if valid else "test invalid",
    )


def pose(*, x=0.0, y=0.0, yaw=0.0, timestamp=10.0):
    return OdomSample(x, y, yaw, timestamp)


def test_point_cloud_sector_assignment_is_configurable(app_config) -> None:
    perception = LocalObstaclePerception(
        app_config.wander.point_cloud,
        blocked_distance_m=app_config.wander.policy.blocked_distance_m,
    )
    points = []
    for angle, distance in [(-72, 2.5), (-36, 2.0), (0, 1.5), (36, 1.0), (72, 0.7)]:
        radians = math.radians(angle)
        points.append([distance * math.cos(radians), distance * math.sin(radians), 0.5])

    result = perception.snapshot(points, timestamp=10.0)

    assert result.valid
    assert result.sector("left").clearance_m == pytest.approx(2.5)
    assert result.sector("front").clearance_m == pytest.approx(1.5)
    assert result.sector("right").blocked


def test_hard_stop_and_stale_sensor_fail_closed(app_config) -> None:
    core = MaplessWanderCore(app_config.wander, seed=1)
    decision = core.decide(
        snapshot(app_config, (4.0, 4.0, 0.2, 4.0, 4.0)),
        pose(),
        now=10.0,
    )
    assert decision.action is WanderAction.STOP
    assert decision.safety is SafetyState.STOP_REQUIRED

    decision = core.decide(
        snapshot(app_config, (4.0,) * 5, timestamp=9.0),
        pose(),
        now=10.0,
    )
    assert decision.action is WanderAction.STOP
    assert decision.safety is SafetyState.SENSOR_STALE


def test_invalid_or_missing_sensor_fails_closed(app_config) -> None:
    core = MaplessWanderCore(app_config.wander, seed=1)
    assert core.decide(None, pose(), now=10.0).action is WanderAction.STOP
    decision = core.decide(
        snapshot(app_config, (4.0,) * 5, valid=False),
        pose(),
        now=10.0,
    )
    assert decision.action is WanderAction.STOP
    assert decision.safety is SafetyState.INVALID


def test_stale_odometry_and_heartbeat_loss_fail_closed(app_config) -> None:
    core = MaplessWanderCore(app_config.wander, seed=1)
    open_space = snapshot(app_config, (4.0,) * 5)
    stale_odom = core.decide(
        open_space,
        pose(timestamp=9.0),
        now=10.0,
    )
    assert stale_odom.action is WanderAction.STOP
    assert stale_odom.safety is SafetyState.SENSOR_STALE

    heartbeat = core.decide(
        open_space,
        pose(),
        now=10.0,
        control_heartbeat_ok=False,
    )
    assert heartbeat.action is WanderAction.STOP
    assert heartbeat.safety is SafetyState.STOP_REQUIRED


def test_seed_is_deterministic_and_blocked_direction_is_not_selected(app_config) -> None:
    obstacles = snapshot(app_config, (4.0, 4.0, 0.6, 4.0, 4.0))
    first = MaplessWanderCore(app_config.wander, seed=22).decide(
        obstacles, pose(), now=10.0
    )
    second = MaplessWanderCore(app_config.wander, seed=22).decide(
        obstacles, pose(), now=10.0
    )
    assert first == second
    assert first.action in {WanderAction.TURN_LEFT, WanderAction.TURN_RIGHT}


def test_soft_and_hard_leash(app_config) -> None:
    open_space = snapshot(app_config, (4.0,) * 5)
    soft_decision = MaplessWanderCore(app_config.wander, seed=4).decide(
        open_space,
        pose(x=app_config.wander.leash.soft_limit_m),
        now=10.0,
    )
    assert soft_decision.action in {WanderAction.TURN_LEFT, WanderAction.TURN_RIGHT}

    hard_decision = MaplessWanderCore(app_config.wander, seed=4).decide(
        open_space,
        pose(x=app_config.wander.leash.hard_limit_m),
        now=10.0,
    )
    assert hard_decision.action is WanderAction.STOP
    assert hard_decision.safety is SafetyState.STOP_REQUIRED


def test_recent_trail_penalizes_forward_candidate(app_config) -> None:
    core = MaplessWanderCore(app_config.wander, seed=7)
    core.trail.add(
        OdomSample(app_config.wander.policy.candidate_step_m, 0.0, 0.0, 9.9)
    )
    decision = core.decide(
        snapshot(app_config, (4.0,) * 5), pose(), now=10.0
    )
    assert decision.action in {WanderAction.TURN_LEFT, WanderAction.TURN_RIGHT}


def test_dead_end_turns_toward_available_side(app_config) -> None:
    decision = MaplessWanderCore(app_config.wander, seed=3).decide(
        snapshot(app_config, (4.0, 0.6, 0.6, 0.6, 4.0)),
        pose(),
        now=10.0,
    )
    assert decision.action in {WanderAction.TURN_LEFT, WanderAction.TURN_RIGHT}


def test_minimum_action_duration_prevents_left_right_oscillation(app_config) -> None:
    core = MaplessWanderCore(app_config.wander, seed=8)
    first = core.decide(
        snapshot(app_config, (4.0, 4.0, 0.6, 4.0, 4.0)),
        pose(),
        now=10.0,
    )
    second = core.decide(
        snapshot(app_config, (4.0, 4.0, 0.6, 4.0, 4.0), timestamp=10.1),
        pose(timestamp=10.1),
        now=10.1,
    )
    assert second.action is first.action
    assert second.reason == "minimum_action_duration"


def test_axis_sign_can_be_changed_without_code_changes(app_config) -> None:
    cloud = replace(app_config.wander.point_cloud, forward_axis="-x", left_axis="-y")
    cloud.validate()
    perception = LocalObstaclePerception(
        cloud,
        blocked_distance_m=app_config.wander.policy.blocked_distance_m,
    )
    points = [[-1.0, 0.0, 0.5]] * cloud.minimum_valid_points
    result = perception.snapshot(points, timestamp=10.0)
    assert result.valid
    assert result.sector("front").clearance_m == pytest.approx(1.0)
