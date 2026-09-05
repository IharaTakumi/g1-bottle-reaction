from __future__ import annotations

import threading
import time
from typing import Callable

from g1_bottle_reaction.navigation.models import (
    NavigationCommandRecord,
    NavigationState,
    NavigationStatus,
    Pose2D,
)

from .navigation import (
    NavigationAdapter,
    NavigationConnectionError,
    NavigationStatusObserver,
    NavigationTransitionError,
)


class MockNavigationAdapter(NavigationAdapter):
    """Deterministic in-memory navigation capability for Windows tests."""

    CAPABILITIES = frozenset(
        {"health", "status", "pose", "patrol", "pause", "resume", "stop"}
    )

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._status = NavigationStatus(NavigationState.IDLE, connected=True)
        self._pose: Pose2D | None = None
        self._lock = threading.RLock()
        self._status_observer: NavigationStatusObserver | None = None
        self.command_history: list[NavigationCommandRecord] = []

    def health(self) -> bool:
        with self._lock:
            return self._status.connected and self._status.state is not NavigationState.ERROR

    def capabilities(self) -> frozenset[str]:
        return self.CAPABILITIES

    def status(self) -> NavigationStatus:
        with self._lock:
            return self._status

    def pose(self) -> Pose2D | None:
        with self._lock:
            return self._pose

    def start_patrol(self, route_id: str) -> NavigationStatus:
        route_id = route_id.strip()
        if not route_id:
            raise ValueError("route_id cannot be empty")
        with self._lock:
            self._require_connected()
            self._require_state(NavigationState.IDLE, NavigationState.STOPPED)
            self._record("start_patrol", route_id=route_id)
            self._status = NavigationStatus(
                NavigationState.PATROLLING,
                connected=True,
                active_route_id=route_id,
            )
            return self._status

    def pause(self, reason: str = "operator") -> NavigationStatus:
        reason = reason.strip() or "operator"
        with self._lock:
            self._require_connected()
            if self._status.state is NavigationState.PAUSED:
                self._record(
                    "pause",
                    route_id=self._status.active_route_id,
                    pause_reason=reason,
                )
                self._status = NavigationStatus(
                    NavigationState.PAUSED,
                    connected=True,
                    active_route_id=self._status.active_route_id,
                    pause_reason=reason,
                )
                return self._status
            self._require_state(NavigationState.PATROLLING)
            self._record(
                "pause",
                route_id=self._status.active_route_id,
                pause_reason=reason,
            )
            self._status = NavigationStatus(
                NavigationState.PAUSED,
                connected=True,
                active_route_id=self._status.active_route_id,
                pause_reason=reason,
            )
            return self._status

    def resume(self) -> NavigationStatus:
        with self._lock:
            self._require_connected()
            self._require_state(NavigationState.PAUSED)
            route_id = self._status.active_route_id
            if route_id is None:
                raise NavigationTransitionError("Cannot resume without an active route")
            self._record("resume", route_id=route_id)
            self._status = NavigationStatus(
                NavigationState.PATROLLING,
                connected=True,
                active_route_id=route_id,
            )
            return self._status

    def stop(self) -> NavigationStatus:
        with self._lock:
            self._require_connected()
            self._record("stop", route_id=self._status.active_route_id)
            self._status = NavigationStatus(NavigationState.STOPPED, connected=True)
            return self._status

    def update_pose(self, pose: Pose2D) -> None:
        with self._lock:
            self._pose = pose

    def disconnect(self, error: str = "mock connection lost") -> None:
        with self._lock:
            self._record("disconnect", route_id=self._status.active_route_id)
            self._status = NavigationStatus(
                NavigationState.DISCONNECTED,
                connected=False,
                active_route_id=self._status.active_route_id,
                last_error=error,
            )
            status = self._status
        self._publish_status("disconnected", status)

    def reconnect(self, *, state: NavigationState = NavigationState.IDLE) -> None:
        if state in {NavigationState.DISCONNECTED, NavigationState.ERROR, NavigationState.PATROLLING}:
            raise ValueError("Mock reconnect must enter a non-moving safe state")
        with self._lock:
            self._record("reconnect")
            self._status = NavigationStatus(state, connected=True)
            status = self._status
        self._publish_status("reconnected", status)

    def inject_error(self, error: str = "mock navigation error") -> None:
        with self._lock:
            self._record("error", route_id=self._status.active_route_id)
            self._status = NavigationStatus(
                NavigationState.ERROR,
                connected=True,
                active_route_id=self._status.active_route_id,
                last_error=error,
            )
            status = self._status
        self._publish_status("error", status)

    def close(self) -> None:
        return None

    def set_status_observer(
        self, observer: NavigationStatusObserver | None
    ) -> None:
        with self._lock:
            self._status_observer = observer

    def _publish_status(self, event: str, status: NavigationStatus) -> None:
        with self._lock:
            observer = self._status_observer
        if observer is not None:
            try:
                observer(event, status)
            except Exception:
                return

    def _record(
        self,
        action: str,
        *,
        route_id: str | None = None,
        pause_reason: str | None = None,
    ) -> None:
        self.command_history.append(
            NavigationCommandRecord(
                action=action,
                timestamp=self._clock(),
                route_id=route_id,
                pause_reason=pause_reason,
            )
        )

    def _require_connected(self) -> None:
        if not self._status.connected:
            raise NavigationConnectionError(self._status.last_error or "Navigation disconnected")
        if self._status.state is NavigationState.ERROR:
            raise NavigationTransitionError(self._status.last_error or "Navigation is in ERROR")

    def _require_state(self, *states: NavigationState) -> None:
        if self._status.state not in states:
            expected = ", ".join(state.value for state in states)
            raise NavigationTransitionError(
                f"Navigation state {self._status.state.value} does not allow this command; "
                f"expected {expected}"
            )
