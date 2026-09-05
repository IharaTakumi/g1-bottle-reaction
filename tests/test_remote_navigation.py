from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from g1_bottle_reaction.adapters.navigation import (
    NavigationConnectionError,
    NavigationSafetyError,
)
from g1_bottle_reaction.adapters.remote_navigation import RemoteNavigationAdapter
from g1_bottle_reaction.main import _create_navigation, build_parser
from g1_bottle_reaction.navigation.coordinator import NavigationCoordinator
from g1_bottle_reaction.navigation.models import NavigationState


class FakeNavigationTransport:
    def __init__(self) -> None:
        self.state = NavigationState.IDLE
        self.connected = True
        self.route_id: str | None = None
        self.pause_reason: str | None = None
        self.calls: list[tuple[str, dict[str, Any], str, float, str | None]] = []
        self.fail_operation: str | None = None
        self.heartbeat_ok = True
        self.closed = False

    def request(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        endpoint: str,
        timeout_s: float,
        command_id: str | None = None,
    ) -> Mapping[str, Any]:
        self.calls.append((operation, dict(payload), endpoint, timeout_s, command_id))
        if operation == self.fail_operation:
            raise TimeoutError(f"{operation} timeout")
        if operation == "health":
            return {"healthy": self.connected}
        if operation == "capabilities":
            return {"capabilities": ["pose", "patrol", "pause", "resume", "stop"]}
        if operation == "pose":
            return {
                "x": 1.25,
                "y": -0.5,
                "yaw_rad": 0.2,
                "frame_id": "map",
                "source_timestamp": 42.0,
            }
        if operation == "start_patrol":
            self.state = NavigationState.PATROLLING
            self.route_id = str(payload["route_id"])
        elif operation == "pause":
            self.state = NavigationState.PAUSED
            self.pause_reason = str(payload["reason"])
        elif operation == "resume":
            self.state = NavigationState.PATROLLING
            self.pause_reason = None
        elif operation == "stop":
            self.state = NavigationState.STOPPED
            self.route_id = None
            self.pause_reason = None
        elif operation == "heartbeat":
            return {"ok": self.heartbeat_ok}
        return self._status()

    def close(self) -> None:
        self.closed = True

    def _status(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "connected": self.connected,
            "active_route_id": self.route_id,
            "pause_reason": self.pause_reason,
            "last_error": None,
        }


def adapter(transport: FakeNavigationTransport, *, enabled: bool = False):
    return RemoteNavigationAdapter(
        "bridge:test",
        transport,
        real_navigation_enabled=enabled,
        command_id_factory=lambda: "command-1",
    )


def test_remote_read_only_operations_need_no_motion_opt_in() -> None:
    transport = FakeNavigationTransport()
    navigation = adapter(transport)
    assert navigation.health()
    assert navigation.status().state is NavigationState.IDLE
    assert navigation.pose().frame_id == "map"
    assert "patrol" in navigation.capabilities()


def test_remote_motion_gate_allows_pause_and_stop_but_blocks_start_and_resume() -> None:
    transport = FakeNavigationTransport()
    navigation = adapter(transport)
    with pytest.raises(NavigationSafetyError, match="--enable-real-navigation"):
        navigation.start_patrol("outer-loop")
    transport.state = NavigationState.PATROLLING
    assert navigation.pause("safety").state is NavigationState.PAUSED
    with pytest.raises(NavigationSafetyError, match="--enable-real-navigation"):
        navigation.resume()
    assert navigation.stop().state is NavigationState.STOPPED


def test_remote_enabled_command_mapping_and_idempotency_key() -> None:
    transport = FakeNavigationTransport()
    navigation = adapter(transport, enabled=True)
    assert navigation.start_patrol("outer-loop").state is NavigationState.PATROLLING
    assert navigation.pause("reaction:1").state is NavigationState.PAUSED
    assert navigation.resume().state is NavigationState.PATROLLING
    assert navigation.stop().state is NavigationState.STOPPED
    command_calls = [call for call in transport.calls if call[0] in {"start_patrol", "pause", "resume", "stop"}]
    assert [call[0] for call in command_calls] == ["start_patrol", "pause", "resume", "stop"]
    assert all(call[4] == "command-1" for call in command_calls)


def test_heartbeat_failure_latches_disconnect_and_blocks_automatic_resume() -> None:
    transport = FakeNavigationTransport()
    navigation = adapter(transport, enabled=True)
    navigation.start_patrol("outer-loop")
    navigation.pause("reaction:1")
    transport.fail_operation = "heartbeat"
    with pytest.raises(NavigationConnectionError, match="heartbeat"):
        navigation.heartbeat()
    assert navigation.status().state is NavigationState.DISCONNECTED
    assert navigation.resume_inhibited
    transport.fail_operation = None
    transport.state = NavigationState.PAUSED
    with pytest.raises(NavigationSafetyError, match="explicit reconnect"):
        navigation.resume()
    assert navigation.reconnect().state is NavigationState.PAUSED
    # Reconnection itself never sends resume.
    assert transport.state is NavigationState.PAUSED
    assert navigation.resume().state is NavigationState.PATROLLING


def test_remote_timeout_marks_status_disconnected() -> None:
    transport = FakeNavigationTransport()
    transport.fail_operation = "pose"
    navigation = adapter(transport)
    with pytest.raises(NavigationConnectionError, match="pose failed"):
        navigation.pose()
    assert navigation.status().state is NavigationState.DISCONNECTED


def test_rejected_heartbeat_and_remote_error_both_latch_movement() -> None:
    transport = FakeNavigationTransport()
    navigation = adapter(transport, enabled=True)
    transport.heartbeat_ok = False
    with pytest.raises(NavigationConnectionError, match="rejected"):
        navigation.heartbeat()
    assert navigation.resume_inhibited

    transport.heartbeat_ok = True
    transport.state = NavigationState.IDLE
    navigation.reconnect()
    transport.state = NavigationState.ERROR
    assert navigation.status().state is NavigationState.ERROR
    assert navigation.resume_inhibited
    with pytest.raises(NavigationSafetyError, match="explicit reconnect"):
        navigation.start_patrol("outer-loop")


def test_heartbeat_disconnect_is_published_for_event_logging() -> None:
    transport = FakeNavigationTransport()
    navigation = adapter(transport, enabled=True)
    events: list[tuple[str, NavigationState]] = []
    coordinator = NavigationCoordinator(
        navigation,
        command_timeout_s=0.1,
        status_poll_interval_s=0.01,
        event_sink=lambda event, status, details: events.append(
            (event, status.state)
        ),
    )
    transport.fail_operation = "heartbeat"

    with pytest.raises(NavigationConnectionError):
        navigation.heartbeat()

    assert ("disconnected", NavigationState.DISCONNECTED) in events
    coordinator.close()


def test_navigation_cli_gate_is_independent_from_robot_gate(app_config) -> None:
    transport = FakeNavigationTransport()
    args = build_parser().parse_args(
        [
            "--robot",
            "mock",
            "--navigation",
            "remote",
            "--navigation-endpoint",
            "bridge:test",
            "--enable-real-navigation",
        ]
    )
    navigation = _create_navigation(
        args, app_config, transport=transport, start_heartbeat=False
    )
    assert isinstance(navigation, RemoteNavigationAdapter)
    assert navigation.real_navigation_enabled
