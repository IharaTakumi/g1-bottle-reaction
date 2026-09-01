from __future__ import annotations

from g1_bottle_reaction.config.loader import StealthGameConfig, VisibilityScoreConfig

from .models import GameEvent, GameState, GameUpdate, TargetObservation, TargetRole


def calculate_visibility_score(
    observation: TargetObservation, config: VisibilityScoreConfig
) -> float:
    """Weighted confidence, apparent area, and horizontal centrality in [0, 1]."""

    if not observation.visible:
        return 0.0
    total_weight = config.confidence_weight + config.area_weight + config.center_weight
    area_score = min(1.0, observation.bbox_area_ratio / config.area_reference_ratio)
    centrality = 1.0 - min(1.0, abs(observation.center_x_normalized))
    weighted = (
        config.confidence_weight * max(0.0, min(1.0, observation.confidence))
        + config.area_weight * area_score
        + config.center_weight * centrality
    )
    return max(0.0, min(1.0, weighted / total_weight))


class StealthGameEngine:
    """Time-based stealth rules independent of YOLO, reactions, speech, and robots."""

    def __init__(self, config: StealthGameConfig) -> None:
        self.config = config
        self.state = GameState.UNAWARE
        self.suspicion = 0.0
        self._last_update_at: float | None = None
        self._candidate_since: float | None = None
        self._missing_since: float | None = None
        self._confirmed_visible = False
        self._last_observation: TargetObservation | None = None
        self._found_at: float | None = None
        self._game_over_at: float | None = None

    def update(self, observation: TargetObservation, *, now: float) -> GameUpdate:
        if self._last_update_at is not None and now < self._last_update_at:
            raise ValueError("stealth game timestamps must be monotonic")
        dt = 0.0 if self._last_update_at is None else now - self._last_update_at
        self._last_update_at = now

        if (
            self.state is GameState.GAME_OVER
            and self.config.round.auto_reset
            and self._game_over_at is not None
            and now - self._game_over_at >= self.config.round.auto_reset_seconds
        ):
            self.reset(now=now)
            dt = 0.0

        events: list[GameEvent] = []
        if self.state is GameState.GAME_OVER:
            return self._snapshot(events=events, visibility_score=0.0)
        if self.state is GameState.FOUND:
            if (
                self._found_at is not None
                and now - self._found_at + 1e-9
                >= self.config.found.game_over_delay_seconds
            ):
                self.state = GameState.GAME_OVER
                self._game_over_at = now
                events.append(GameEvent.GAME_OVER)
            return self._snapshot(events=events, visibility_score=0.0)

        raw_visible = (
            observation.visible
            and observation.semantic_role.value == self.config.target.semantic_role
            and observation.confidence >= self.config.target.confidence_threshold
        )
        exposure_dt = dt

        if raw_visible:
            self._last_observation = observation
            self._missing_since = None
            if not self._confirmed_visible:
                if self._candidate_since is None:
                    self._candidate_since = now
                if now - self._candidate_since >= self.config.target.detection_confirm_seconds:
                    exposure_dt = max(dt, now - self._candidate_since)
                    self._confirmed_visible = True
                    self._candidate_since = None
        else:
            self._candidate_since = None
            if self._confirmed_visible:
                if self._missing_since is None:
                    self._missing_since = now
                if now - self._missing_since >= self.config.target.lost_grace_seconds:
                    self._confirmed_visible = False
                    self._missing_since = None
                    events.append(GameEvent.PLAYER_LOST)

        visibility_score = (
            calculate_visibility_score(observation, self.config.visibility_score)
            if raw_visible and self._confirmed_visible
            else 0.0
        )

        if self.state not in (GameState.FOUND, GameState.GAME_OVER):
            previous_state = self.state
            if raw_visible and self._confirmed_visible:
                self.suspicion += (
                    self.config.suspicion.gain_per_second
                    * visibility_score
                    * exposure_dt
                )
            elif not self._confirmed_visible:
                self.suspicion -= self.config.suspicion.decay_per_second * dt
            self.suspicion = max(0.0, min(100.0, self.suspicion))
            self.state = self._state_for_suspicion(self.suspicion)
            self._append_transition_events(previous_state, events, now=now)

        return self._snapshot(events=events, visibility_score=visibility_score)

    def _snapshot(
        self, *, events: list[GameEvent], visibility_score: float
    ) -> GameUpdate:
        return GameUpdate(
            state=self.state,
            suspicion=self.suspicion,
            visibility_score=visibility_score,
            target_visible=self._confirmed_visible,
            observation=self._last_observation,
            events=tuple(events),
        )

    def reset(self, *, now: float | None = None) -> None:
        self.state = GameState.UNAWARE
        self.suspicion = 0.0
        self._last_update_at = now
        self._candidate_since = None
        self._missing_since = None
        self._confirmed_visible = False
        self._last_observation = None
        self._found_at = None
        self._game_over_at = None

    def _state_for_suspicion(self, suspicion: float) -> GameState:
        thresholds = self.config.suspicion
        if suspicion >= thresholds.found_threshold:
            return GameState.FOUND
        if suspicion >= thresholds.alert_threshold:
            return GameState.ALERT
        if suspicion >= thresholds.suspicious_threshold:
            return GameState.SUSPICIOUS
        return GameState.UNAWARE

    def _append_transition_events(
        self, previous: GameState, events: list[GameEvent], *, now: float
    ) -> None:
        if previous is GameState.UNAWARE and self.state in (
            GameState.SUSPICIOUS,
            GameState.ALERT,
            GameState.FOUND,
        ):
            events.append(GameEvent.SUSPICION_STARTED)
        if previous in (GameState.UNAWARE, GameState.SUSPICIOUS) and self.state in (
            GameState.ALERT,
            GameState.FOUND,
        ):
            events.append(GameEvent.ALERT_STARTED)
        if self.state is GameState.FOUND and previous is not GameState.FOUND:
            self._found_at = now
            events.append(GameEvent.PLAYER_FOUND)
        elif self.state is GameState.UNAWARE and previous in (
            GameState.SUSPICIOUS,
            GameState.ALERT,
        ):
            events.append(GameEvent.RETURNED_TO_UNAWARE)
