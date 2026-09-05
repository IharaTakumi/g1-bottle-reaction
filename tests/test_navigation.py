from __future__ import annotations

import pytest

from g1_bottle_reaction.adapters.mock_navigation import MockNavigationAdapter
from g1_bottle_reaction.adapters.navigation import (
    NavigationConnectionError,
    NavigationTransitionError,
)
from g1_bottle_reaction.navigation.models import NavigationState, Pose2D


def test_pose2d_and_mock_pose_update() -> None:
    navigation = MockNavigationAdapter(clock=lambda: 12.5)
    pose = Pose2D(1.0, -2.0, 0.25, "map", 10.0)
    navigation.update_pose(pose)
    assert navigation.pose() == pose
    with pytest.raises(ValueError, match="finite"):
        Pose2D(float("nan"), 0, 0, "map", 0)
    with pytest.raises(ValueError, match="frame_id"):
        Pose2D(0, 0, 0, "", 0)


def test_mock_patrol_pause_resume_stop_sequence() -> None:
    navigation = MockNavigationAdapter(clock=lambda: 1.0)
    assert navigation.status().state is NavigationState.IDLE
    assert navigation.start_patrol("outer-loop").state is NavigationState.PATROLLING
    paused = navigation.pause("reaction:test")
    assert paused.state is NavigationState.PAUSED
    assert paused.pause_reason == "reaction:test"
    assert navigation.resume().state is NavigationState.PATROLLING
    assert navigation.stop().state is NavigationState.STOPPED
    assert [item.action for item in navigation.command_history] == [
        "start_patrol",
        "pause",
        "resume",
        "stop",
    ]
    assert navigation.command_history[0].route_id == "outer-loop"


def test_mock_invalid_transitions_are_rejected() -> None:
    navigation = MockNavigationAdapter()
    with pytest.raises(NavigationTransitionError):
        navigation.pause()
    with pytest.raises(NavigationTransitionError):
        navigation.resume()
    navigation.start_patrol("route")
    with pytest.raises(NavigationTransitionError):
        navigation.start_patrol("route-again")


def test_mock_connection_loss_latches_disconnected_state() -> None:
    navigation = MockNavigationAdapter()
    published: list[tuple[str, NavigationState]] = []
    navigation.set_status_observer(
        lambda event, status: published.append((event, status.state))
    )
    navigation.start_patrol("outer-loop")
    navigation.disconnect("link down")
    status = navigation.status()
    assert status.state is NavigationState.DISCONNECTED
    assert not status.connected
    assert status.active_route_id == "outer-loop"
    assert status.last_error == "link down"
    assert published == [("disconnected", NavigationState.DISCONNECTED)]
    with pytest.raises(NavigationConnectionError):
        navigation.resume()


def test_mock_error_injection_and_explicit_safe_reconnect() -> None:
    navigation = MockNavigationAdapter()
    navigation.inject_error("planner failed")
    assert navigation.status().state is NavigationState.ERROR
    assert navigation.status().last_error == "planner failed"
    with pytest.raises(NavigationTransitionError):
        navigation.start_patrol("route")
    navigation.reconnect(state=NavigationState.STOPPED)
    assert navigation.status().state is NavigationState.STOPPED
    assert navigation.start_patrol("route").state is NavigationState.PATROLLING


def test_navigation_config_has_safe_minimal_defaults(app_config) -> None:
    config = app_config.navigation
    assert config.default_route_id == "outer-loop"
    assert config.command_timeout_s > 0
    assert config.heartbeat_timeout_s > config.heartbeat_interval_s
    assert config.auto_pause_for_reaction
