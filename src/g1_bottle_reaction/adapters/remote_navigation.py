from __future__ import annotations

from collections.abc import Mapping
import threading
from typing import Any, Callable
import uuid

from g1_bottle_reaction.navigation.models import NavigationState, NavigationStatus, Pose2D

from .navigation import (
    NavigationAdapter,
    NavigationConnectionError,
    NavigationSafetyError,
    NavigationStatusObserver,
    NavigationTransport,
    NavigationTransitionError,
)


class RemoteNavigationAdapter(NavigationAdapter):
    """Protocol-neutral client boundary for a future Ubuntu SLAM bridge."""

    def __init__(
        self,
        endpoint: str,
        transport: NavigationTransport,
        *,
        real_navigation_enabled: bool = False,
        command_timeout_s: float = 2.0,
        heartbeat_interval_s: float = 0.5,
        heartbeat_timeout_s: float = 2.0,
        command_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        if not endpoint.strip():
            raise ValueError("Remote navigation endpoint cannot be empty")
        if min(command_timeout_s, heartbeat_interval_s, heartbeat_timeout_s) <= 0:
            raise ValueError("Remote navigation timeouts must be positive")
        self.endpoint = endpoint
        self.transport = transport
        self.real_navigation_enabled = real_navigation_enabled
        self.command_timeout_s = command_timeout_s
        self.heartbeat_interval_s = heartbeat_interval_s
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self._command_id_factory = command_id_factory
        self._lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._status = NavigationStatus(
            NavigationState.DISCONNECTED,
            connected=False,
            last_error="Remote status has not been read",
        )
        self._resume_inhibited = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_worker: threading.Thread | None = None
        self._heartbeat_enabled = False
        self._status_observer: NavigationStatusObserver | None = None

    def health(self) -> bool:
        response = self._request("health", {}, timeout_s=self.command_timeout_s)
        return bool(response.get("healthy", response.get("ok", False)))

    def capabilities(self) -> frozenset[str]:
        response = self._request("capabilities", {}, timeout_s=self.command_timeout_s)
        values = response.get("capabilities", ())
        if not isinstance(values, (list, tuple, set, frozenset)):
            message = "Remote capabilities response is invalid"
            self._mark_disconnected(message)
            raise NavigationConnectionError(message)
        return frozenset(str(value) for value in values)

    def status(self) -> NavigationStatus:
        with self._lock:
            if self._resume_inhibited and self._status.state is NavigationState.DISCONNECTED:
                return self._status
        response = self._request("status", {}, timeout_s=self.command_timeout_s)
        return self._store_status_response(response)

    def pose(self) -> Pose2D | None:
        response = self._request("pose", {}, timeout_s=self.command_timeout_s)
        if response.get("available") is False:
            return None
        try:
            return _parse_pose(response)
        except NavigationConnectionError as exc:
            self._mark_disconnected(str(exc))
            raise

    def start_patrol(self, route_id: str) -> NavigationStatus:
        self._require_motion_allowed("start_patrol")
        route_id = route_id.strip()
        if not route_id:
            raise ValueError("route_id cannot be empty")
        return self._command("start_patrol", {"route_id": route_id})

    def pause(self, reason: str = "operator") -> NavigationStatus:
        return self._command("pause", {"reason": reason.strip() or "operator"})

    def resume(self) -> NavigationStatus:
        self._require_motion_allowed("resume")
        return self._command("resume", {})

    def stop(self) -> NavigationStatus:
        return self._command("stop", {})

    def heartbeat(self) -> bool:
        try:
            response = self._request(
                "heartbeat",
                {},
                timeout_s=self.heartbeat_timeout_s,
            )
            if not bool(response.get("ok", False)):
                raise NavigationConnectionError("Remote heartbeat was rejected")
            return True
        except NavigationConnectionError as exc:
            self._mark_disconnected(f"Remote heartbeat failed: {exc}")
            raise
        except Exception as exc:
            self._mark_disconnected(f"Remote heartbeat failed: {exc}")
            raise NavigationConnectionError(str(exc)) from exc

    def start_heartbeat(self) -> None:
        with self._lock:
            self._heartbeat_enabled = True
            if self._heartbeat_worker is not None:
                return
            self._heartbeat_stop.clear()
            self._heartbeat_worker = threading.Thread(
                target=self._heartbeat_loop,
                name="navigation-heartbeat",
                daemon=True,
            )
            self._heartbeat_worker.start()

    def reconnect(self) -> NavigationStatus:
        """Explicitly clear the post-fault movement latch only in a safe state."""

        response = self._request(
            "status", {}, timeout_s=self.command_timeout_s, allow_latched=True
        )
        try:
            status = _parse_status(response)
        except NavigationConnectionError as exc:
            self._mark_disconnected(str(exc))
            raise
        if not status.connected or status.state in {
            NavigationState.DISCONNECTED,
            NavigationState.ERROR,
            NavigationState.PATROLLING,
        }:
            message = (
                "Remote reconnect must report a connected non-moving state; "
                f"received {status.state.value}"
            )
            self._mark_disconnected(message)
            raise NavigationTransitionError(message)
        with self._lock:
            self._status = status
            self._resume_inhibited = False
        self._publish_status("reconnected", status)
        with self._lock:
            restart_heartbeat = (
                self._heartbeat_enabled and self._heartbeat_worker is None
            )
        if restart_heartbeat:
            self.start_heartbeat()
        return status

    def close(self) -> None:
        worker: threading.Thread | None
        with self._lock:
            worker = self._heartbeat_worker
            self._heartbeat_worker = None
            self._heartbeat_enabled = False
            self._heartbeat_stop.set()
        if worker is not None:
            worker.join(timeout=max(1.0, self.heartbeat_interval_s * 2))
        self.transport.close()

    @property
    def resume_inhibited(self) -> bool:
        with self._lock:
            return self._resume_inhibited

    def set_status_observer(
        self, observer: NavigationStatusObserver | None
    ) -> None:
        with self._lock:
            self._status_observer = observer

    def _command(self, operation: str, payload: Mapping[str, Any]) -> NavigationStatus:
        command_id = self._command_id_factory()
        response = self._request(
            operation,
            payload,
            timeout_s=self.command_timeout_s,
            command_id=command_id,
            allow_latched=operation in {"pause", "stop"},
        )
        return self._store_status_response(response)

    def _store_status_response(
        self, response: Mapping[str, Any]
    ) -> NavigationStatus:
        try:
            status = _parse_status(response)
        except NavigationConnectionError as exc:
            self._mark_disconnected(str(exc))
            raise
        with self._lock:
            previous = self._status
            self._status = status
            if not status.connected or status.state in {
                NavigationState.DISCONNECTED,
                NavigationState.ERROR,
            }:
                self._resume_inhibited = True
        if status != previous:
            event = (
                "error"
                if status.state is NavigationState.ERROR
                else "disconnected"
                if not status.connected or status.state is NavigationState.DISCONNECTED
                else "state_changed"
            )
            self._publish_status(event, status)
        return status

    def _request(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        timeout_s: float,
        command_id: str | None = None,
        allow_latched: bool = False,
    ) -> Mapping[str, Any]:
        if not allow_latched:
            with self._lock:
                if self._resume_inhibited and operation in {"start_patrol", "resume"}:
                    raise NavigationSafetyError(
                        "Navigation movement is inhibited after a connection fault; "
                        "explicit reconnect is required"
                    )
        try:
            # Heartbeats and application commands share one transport instance.
            # Keep the minimal contract usable by transports that are not
            # themselves thread-safe.
            with self._request_lock:
                response = self.transport.request(
                    operation,
                    payload,
                    endpoint=self.endpoint,
                    timeout_s=timeout_s,
                    command_id=command_id,
                )
        except (TimeoutError, OSError, ConnectionError, NavigationConnectionError) as exc:
            self._mark_disconnected(f"Remote {operation} failed: {exc}")
            raise NavigationConnectionError(f"Remote {operation} failed: {exc}") from exc
        if not isinstance(response, Mapping):
            self._mark_disconnected(f"Remote {operation} returned an invalid response")
            raise NavigationConnectionError(
                f"Remote {operation} returned an invalid response"
            )
        return response

    def _require_motion_allowed(self, operation: str) -> None:
        if not self.real_navigation_enabled:
            raise NavigationSafetyError(
                f"Remote {operation} requires --enable-real-navigation"
            )
        with self._lock:
            if self._resume_inhibited:
                raise NavigationSafetyError(
                    "Navigation movement is inhibited after a connection fault; "
                    "explicit reconnect is required"
                )

    def _mark_disconnected(self, error: str) -> None:
        with self._lock:
            previous = self._status
            self._resume_inhibited = True
            self._status = NavigationStatus(
                NavigationState.DISCONNECTED,
                connected=False,
                active_route_id=self._status.active_route_id,
                last_error=error,
            )
            status = self._status
        if status != previous:
            self._publish_status("disconnected", status)

    def _publish_status(self, event: str, status: NavigationStatus) -> None:
        with self._lock:
            observer = self._status_observer
        if observer is not None:
            try:
                observer(event, status)
            except Exception:
                # State reporting must not alter navigation safety behavior.
                return

    def _heartbeat_loop(self) -> None:
        try:
            while not self._heartbeat_stop.wait(self.heartbeat_interval_s):
                try:
                    self.heartbeat()
                except NavigationConnectionError:
                    return
        finally:
            with self._lock:
                if self._heartbeat_worker is threading.current_thread():
                    self._heartbeat_worker = None
                restart = self._heartbeat_enabled and not self._resume_inhibited
            if restart:
                self.start_heartbeat()


def _parse_status(value: Mapping[str, Any]) -> NavigationStatus:
    try:
        state = NavigationState(str(value["state"]))
        connected = bool(value["connected"])
    except (KeyError, TypeError, ValueError) as exc:
        raise NavigationConnectionError("Remote navigation status response is invalid") from exc
    return NavigationStatus(
        state=state,
        connected=connected,
        active_route_id=_optional_string(value.get("active_route_id")),
        pause_reason=_optional_string(value.get("pause_reason")),
        last_error=_optional_string(value.get("last_error")),
    )


def _parse_pose(value: Mapping[str, Any]) -> Pose2D:
    try:
        return Pose2D(
            x=float(value["x"]),
            y=float(value["y"]),
            yaw_rad=float(value["yaw_rad"]),
            frame_id=str(value["frame_id"]),
            source_timestamp=float(value["source_timestamp"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise NavigationConnectionError("Remote navigation pose response is invalid") from exc


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)
