from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from g1_bottle_reaction.adapters.navigation import (
    NavigationAdapter,
    NavigationConnectionError,
    NavigationError,
    NavigationTransitionError,
)
from g1_bottle_reaction.reactions.engine import ReactionJob

from .models import NavigationState, NavigationStatus

NavigationEventSink = Callable[[str, NavigationStatus, dict[str, object]], None]
LOGGER = logging.getLogger(__name__)


class NavigationCoordinator:
    """Small safety coordinator between accepted reactions and navigation."""

    def __init__(
        self,
        navigation: NavigationAdapter,
        *,
        command_timeout_s: float,
        status_poll_interval_s: float,
        auto_pause_for_reaction: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        event_sink: NavigationEventSink | None = None,
    ) -> None:
        if command_timeout_s <= 0 or status_poll_interval_s <= 0:
            raise ValueError("Navigation coordinator timeouts must be positive")
        self.navigation = navigation
        self.command_timeout_s = command_timeout_s
        self.status_poll_interval_s = status_poll_interval_s
        self.auto_pause_for_reaction = auto_pause_for_reaction
        self._clock = clock
        self._sleep = sleep
        self._event_sink = event_sink
        self._navigation_lock = threading.Lock()
        self._lock = threading.RLock()
        self._pending_reactions = 0
        self._owned_pause_reason: str | None = None
        self._reaction_batch_failed = False
        self._last_error: str | None = None
        self._last_status = NavigationStatus(
            NavigationState.DISCONNECTED,
            connected=False,
            last_error="Navigation status has not been read",
        )
        self.navigation.set_status_observer(self._on_adapter_status)

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def owns_reaction_pause(self) -> bool:
        with self._lock:
            return self._owned_pause_reason is not None

    def set_event_sink(self, sink: NavigationEventSink | None) -> None:
        with self._lock:
            self._event_sink = sink

    def status(self) -> NavigationStatus:
        status = self.navigation.status()
        self._remember_status(status)
        return status

    def start_patrol(self, route_id: str) -> NavigationStatus:
        with self._navigation_lock:
            status = self.navigation.start_patrol(route_id)
            with self._lock:
                self._last_status = status
                self._owned_pause_reason = None
                self._reaction_batch_failed = False
                self._last_error = None
        self._emit("patrol_started", status, {"route_id": route_id})
        return status

    def pause(self, reason: str = "operator") -> NavigationStatus:
        with self._navigation_lock:
            status = self.navigation.pause(reason)
            with self._lock:
                self._last_status = status
                self._owned_pause_reason = None
        self._emit("paused", status, {"pause_reason": reason})
        return status

    def resume(self) -> NavigationStatus:
        with self._navigation_lock:
            status = self.navigation.resume()
            with self._lock:
                self._last_status = status
                self._owned_pause_reason = None
                self._reaction_batch_failed = False
                self._last_error = None
        self._emit("resumed", status, {})
        return status

    def stop(self) -> NavigationStatus:
        with self._navigation_lock:
            status = self.navigation.stop()
            with self._lock:
                self._last_status = status
                self._owned_pause_reason = None
                self._reaction_batch_failed = True
        self._emit("stopped", status, {})
        return status

    def on_accepted(self, job: ReactionJob) -> None:
        with self._lock:
            self._pending_reactions += 1
            status = self._last_status
        # This callback runs on the detector/audio caller. Use the cached snapshot
        # so accepting a reaction never performs remote I/O on those hot paths.
        self._emit("reaction_accepted", status, {"reaction_id": job.id})

    def before_start(self, job: ReactionJob) -> None:
        if not self.auto_pause_for_reaction:
            return
        with self._navigation_lock:
            status = self.navigation.status()
            self._remember_status(status)
            if status.state is not NavigationState.PATROLLING:
                return
            with self._lock:
                owns_pause = self._owned_pause_reason is not None
            if not owns_pause:
                reason = f"reaction:{job.id}"
                with self._lock:
                    self._owned_pause_reason = reason
                paused = self.navigation.pause(reason)
                self._remember_status(paused)
                self._emit(
                    "reaction_pause_requested",
                    paused,
                    {"reaction_id": job.id, "pause_reason": reason},
                )
                confirmed = self._wait_for_pause(reason)
                self._emit(
                    "reaction_paused",
                    confirmed,
                    {"reaction_id": job.id, "pause_reason": reason},
                )

    def on_started(self, job: ReactionJob) -> None:
        self._emit("reaction_started", self._status_snapshot(), {"reaction_id": job.id})

    def on_completed(self, job: ReactionJob) -> None:
        with self._lock:
            self._pending_reactions = max(0, self._pending_reactions - 1)
            should_consider_resume = (
                self._pending_reactions == 0
                and self._owned_pause_reason is not None
                and not self._reaction_batch_failed
            )
            reason = self._owned_pause_reason
            status = self._last_status
        if not should_consider_resume:
            self._emit(
                "reaction_completed",
                status,
                {"reaction_id": job.id, "auto_resume": False},
            )
            return
        try:
            with self._navigation_lock:
                # Re-check ownership after waiting for any operator command.
                with self._lock:
                    ownership_is_current = (
                        self._owned_pause_reason == reason
                        and not self._reaction_batch_failed
                    )
                if not ownership_is_current:
                    self._emit(
                        "reaction_completed",
                        self._status_snapshot(),
                        {"reaction_id": job.id, "auto_resume": False},
                    )
                    return
                status = self.navigation.status()
                self._remember_status(status)
                safe_to_resume = (
                    status.connected
                    and status.state is NavigationState.PAUSED
                    and status.pause_reason == reason
                    and status.last_error is None
                )
                if safe_to_resume:
                    resumed = self.navigation.resume()
                    self._remember_status(resumed)
                else:
                    resumed = None
        except Exception as exc:
            with self._lock:
                self._owned_pause_reason = None
                self._reaction_batch_failed = True
                self._last_error = str(exc)
            self._emit(
                "navigation_error",
                self._safe_status(),
                {"reaction_id": job.id, "error": str(exc)},
            )
            return
        if resumed is None:
            with self._lock:
                self._owned_pause_reason = None
                self._reaction_batch_failed = True
                self._last_error = (
                    "Reaction completed but navigation pause ownership was lost; "
                    "automatic resume was inhibited"
                )
            self._emit(
                "reaction_completed",
                status,
                {"reaction_id": job.id, "auto_resume": False},
            )
            return
        with self._lock:
            self._owned_pause_reason = None
        self._emit(
            "reaction_completed",
            resumed,
            {"reaction_id": job.id, "auto_resume": True},
        )

    def on_failed(self, job: ReactionJob, error: Exception) -> None:
        with self._lock:
            self._pending_reactions = max(0, self._pending_reactions - 1)
            self._owned_pause_reason = None
            self._reaction_batch_failed = True
            self._last_error = str(error)
        try:
            status = self.status()
        except NavigationError:
            status = NavigationStatus(
                NavigationState.DISCONNECTED,
                connected=False,
                last_error=str(error),
            )
        self._emit(
            "reaction_failed",
            status,
            {"reaction_id": job.id, "error": str(error)},
        )

    def close(self) -> None:
        try:
            try:
                status = self.status()
            except NavigationError as exc:
                with self._lock:
                    self._last_error = str(exc)
                return
            if status.connected and status.state in {
                NavigationState.PATROLLING,
                NavigationState.PAUSED,
            }:
                try:
                    self.stop()
                except NavigationError as exc:
                    self._last_error = str(exc)
        finally:
            self.navigation.set_status_observer(None)
            self.navigation.close()

    def _wait_for_pause(self, reason: str) -> NavigationStatus:
        deadline = self._clock() + self.command_timeout_s
        while True:
            status = self.navigation.status()
            self._remember_status(status)
            if not status.connected or status.state in {
                NavigationState.DISCONNECTED,
                NavigationState.ERROR,
                NavigationState.STOPPED,
            }:
                raise NavigationConnectionError(
                    status.last_error or "Navigation failed while waiting for pause"
                )
            if status.state is NavigationState.PAUSED:
                if status.pause_reason != reason:
                    raise NavigationTransitionError(
                        "Navigation paused without the coordinator ownership reason"
                    )
                return status
            if self._clock() >= deadline:
                raise TimeoutError("Navigation did not confirm PAUSED before timeout")
            self._sleep(self.status_poll_interval_s)

    def _emit(
        self,
        event: str,
        status: NavigationStatus,
        details: dict[str, object],
    ) -> None:
        sink = self._event_sink
        if sink is not None:
            try:
                sink(event, status, details)
            except Exception:
                # Observability must never change motion safety or reaction state.
                LOGGER.exception("Navigation event sink failed for %s", event)

    def _safe_status(self) -> NavigationStatus:
        try:
            return self.status()
        except NavigationError as exc:
            status = NavigationStatus(
                NavigationState.DISCONNECTED,
                connected=False,
                last_error=str(exc),
            )
            self._remember_status(status)
            return status

    def _remember_status(self, status: NavigationStatus) -> None:
        with self._lock:
            self._last_status = status

    def _status_snapshot(self) -> NavigationStatus:
        with self._lock:
            return self._last_status

    def _on_adapter_status(self, event: str, status: NavigationStatus) -> None:
        self._remember_status(status)
        self._emit(event, status, {"origin": "adapter"})
