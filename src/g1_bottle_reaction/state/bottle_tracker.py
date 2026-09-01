from __future__ import annotations

from dataclasses import dataclass

from g1_bottle_reaction.config.loader import TrackingConfig
from g1_bottle_reaction.state.events import BottleEvent, BottleState


@dataclass(frozen=True, slots=True)
class TrackingUpdate:
    state: BottleState
    event: BottleEvent | None
    confidence: float
    proximity_ratio: float
    encounter_count: int


class BottleTracker:
    """Time-based bottle debounce plus proximity hysteresis."""

    def __init__(self, config: TrackingConfig) -> None:
        self.config = config
        self.state = BottleState.NO_BOTTLE
        self.encounter_count = 0
        self._detection_started_at: float | None = None
        self._missing_started_at: float | None = None
        self._last_lost_at: float | None = None

    def update(
        self,
        *,
        detected: bool,
        confidence: float = 0.0,
        proximity_ratio: float = 0.0,
        now: float,
    ) -> TrackingUpdate:
        if detected:
            event = self._on_detected(proximity_ratio, now)
        else:
            confidence = 0.0
            proximity_ratio = 0.0
            event = self._on_missing(now)
        return TrackingUpdate(
            state=self.state,
            event=event,
            confidence=confidence,
            proximity_ratio=proximity_ratio,
            encounter_count=self.encounter_count,
        )

    @property
    def _visible(self) -> bool:
        return self.state in {
            BottleState.FAR,
            BottleState.NEAR,
            BottleState.TOO_CLOSE,
        }

    def _on_detected(self, ratio: float, now: float) -> BottleEvent | None:
        self._missing_started_at = None
        if not self._visible:
            if self._detection_started_at is None:
                self._detection_started_at = now
            if now - self._detection_started_at < self.config.detection_confirm_seconds:
                return None

            was_seen_before = self.encounter_count > 0
            is_recent_return = (
                was_seen_before
                and self._last_lost_at is not None
                and now - self._last_lost_at
                <= self.config.found_again_window_seconds
            )
            self.state = self._initial_proximity_state(ratio)
            self.encounter_count += 1
            self._detection_started_at = None
            return BottleEvent.FOUND_AGAIN if is_recent_return else BottleEvent.FOUND

        self._detection_started_at = None
        return self._update_proximity(ratio)

    def _on_missing(self, now: float) -> BottleEvent | None:
        self._detection_started_at = None
        if not self._visible:
            return None
        if self._missing_started_at is None:
            self._missing_started_at = now
        if now - self._missing_started_at < self.config.lost_confirm_seconds:
            return None
        self.state = BottleState.LOST
        self._last_lost_at = now
        self._missing_started_at = None
        return BottleEvent.LOST

    def _initial_proximity_state(self, ratio: float) -> BottleState:
        if ratio >= self.config.too_close_enter_ratio:
            return BottleState.TOO_CLOSE
        if ratio >= self.config.near_enter_ratio:
            return BottleState.NEAR
        return BottleState.FAR

    def _update_proximity(self, ratio: float) -> BottleEvent | None:
        if self.state is BottleState.FAR:
            if ratio >= self.config.too_close_enter_ratio:
                self.state = BottleState.TOO_CLOSE
                return BottleEvent.TOO_CLOSE
            if ratio >= self.config.near_enter_ratio:
                self.state = BottleState.NEAR
                return BottleEvent.NEAR
            return None

        if self.state is BottleState.NEAR:
            if ratio >= self.config.too_close_enter_ratio:
                self.state = BottleState.TOO_CLOSE
                return BottleEvent.TOO_CLOSE
            if ratio < self.config.near_exit_ratio:
                self.state = BottleState.FAR
            return None

        if self.state is BottleState.TOO_CLOSE:
            if ratio < self.config.too_close_exit_ratio:
                if ratio >= self.config.near_exit_ratio:
                    self.state = BottleState.NEAR
                    return BottleEvent.NEAR
                self.state = BottleState.FAR
            return None

        return None
