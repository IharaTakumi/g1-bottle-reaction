from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Callable, Protocol

from g1_bottle_reaction.navigation.models import NavigationStatus, Pose2D

NavigationStatusObserver = Callable[[str, NavigationStatus], None]


class NavigationError(RuntimeError):
    """Base error for navigation capability failures."""


class NavigationConnectionError(NavigationError):
    """The remote navigation capability could not be reached."""


class NavigationTransitionError(NavigationError):
    """A command is invalid for the current navigation state."""


class NavigationSafetyError(NavigationError):
    """A motion-capable command was blocked by a safety gate."""


class NavigationAdapter(ABC):
    """Robot-independent navigation capability used by the game application."""

    @abstractmethod
    def health(self) -> bool:
        """Read whether the navigation backend is healthy."""

    @abstractmethod
    def capabilities(self) -> frozenset[str]:
        """Read backend capabilities without changing robot state."""

    @abstractmethod
    def status(self) -> NavigationStatus:
        """Read the latest navigation state."""

    @abstractmethod
    def pose(self) -> Pose2D | None:
        """Read the latest 2D pose when available."""

    @abstractmethod
    def start_patrol(self, route_id: str) -> NavigationStatus:
        """Start a route. This operation may move the robot."""

    @abstractmethod
    def pause(self, reason: str = "operator") -> NavigationStatus:
        """Request a fail-safe pause."""

    @abstractmethod
    def resume(self) -> NavigationStatus:
        """Resume a paused route. This operation may move the robot."""

    @abstractmethod
    def stop(self) -> NavigationStatus:
        """Request a fail-safe stop."""

    @abstractmethod
    def close(self) -> None:
        """Release adapter-side resources without implicitly resuming."""

    def set_status_observer(
        self, observer: NavigationStatusObserver | None
    ) -> None:
        """Optionally publish autonomous state changes such as heartbeat faults."""

        del observer


class NavigationTransport(Protocol):
    """Protocol-neutral request boundary for a future Ubuntu bridge."""

    def request(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        endpoint: str,
        timeout_s: float,
        command_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    def close(self) -> None: ...
