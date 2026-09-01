from __future__ import annotations

import math

from g1_bottle_reaction.adapters.robot import RobotAdapter, TrackingCommand
from g1_bottle_reaction.config.loader import StealthTrackingConfig

from .models import GameState, TargetObservation


def calculate_target_yaw(center_x: float, config: StealthTrackingConfig) -> float:
    """Map normalized image X through a deadzone to a bounded preview yaw."""

    center_x = max(-1.0, min(1.0, center_x))
    if config.invert_x:
        center_x = -center_x
    magnitude = abs(center_x)
    if magnitude <= config.deadzone:
        return 0.0
    scaled = (magnitude - config.deadzone) / (1.0 - config.deadzone)
    maximum = math.radians(config.max_preview_yaw_degrees)
    return max(-maximum, min(maximum, math.copysign(maximum * scaled, center_x)))


class TargetTrackingController:
    """FPS-independent continuous attention kept outside the Reaction Engine."""

    def __init__(self, config: StealthTrackingConfig, robot: RobotAdapter) -> None:
        self.config = config
        self.robot = robot
        self.current_yaw = 0.0
        self.target_yaw = 0.0
        self.last_seen_center_x: float | None = None
        self.last_seen_at: float | None = None
        self._locked_yaw: float | None = None
        self._last_update_at: float | None = None
        self._previous_state = GameState.UNAWARE
        self.last_command = self._command(
            state=GameState.UNAWARE,
            status="OFF",
            target_visible=False,
            now=0.0,
        )

    def update(
        self,
        observation: TargetObservation | None,
        *,
        state: GameState,
        now: float,
    ) -> TrackingCommand:
        if self._last_update_at is not None and now < self._last_update_at:
            raise ValueError("tracking timestamps must be monotonic")
        dt = 0.0 if self._last_update_at is None else now - self._last_update_at
        self._last_update_at = now
        target_visible = observation is not None and observation.visible

        if target_visible and state not in (GameState.FOUND, GameState.GAME_OVER):
            assert observation is not None
            self.last_seen_center_x = observation.center_x_normalized
            self.last_seen_at = now

        if not self.config.enabled:
            status = "OFF"
            speed = self.config.recenter_response_speed
            self.target_yaw = 0.0
        elif state is GameState.UNAWARE:
            status = "RECENTER" if abs(self.current_yaw) > 1e-6 else "OFF"
            speed = self.config.recenter_response_speed
            self.target_yaw = 0.0
            self._locked_yaw = None
        elif state in (GameState.FOUND, GameState.GAME_OVER):
            if self._previous_state not in (GameState.FOUND, GameState.GAME_OVER):
                if observation is not None and observation.visible:
                    self.last_seen_center_x = observation.center_x_normalized
                    self.last_seen_at = now
                self._locked_yaw = self._last_seen_yaw(default=self.target_yaw)
            if self._locked_yaw is None:
                self._locked_yaw = self.target_yaw
            self.target_yaw = self._locked_yaw
            status = "LOCKED"
            speed = self.config.found_response_speed
        elif target_visible:
            assert observation is not None
            self.target_yaw = calculate_target_yaw(
                observation.center_x_normalized, self.config
            )
            status = "ACTIVE"
            speed = self._state_response_speed(state)
        elif (
            self.last_seen_at is not None
            and now - self.last_seen_at <= self.config.lost_hold_seconds
        ):
            self.target_yaw = self._last_seen_yaw(default=self.target_yaw)
            status = "HOLD"
            speed = self._state_response_speed(state)
        else:
            self.target_yaw = 0.0
            status = "RECENTER"
            speed = self.config.recenter_response_speed

        alpha = 1.0 - math.exp(-speed * dt) if speed > 0 else 0.0
        self.current_yaw += (self.target_yaw - self.current_yaw) * alpha
        maximum = math.radians(self.config.max_preview_yaw_degrees)
        self.current_yaw = max(-maximum, min(maximum, self.current_yaw))
        if abs(self.current_yaw) < 1e-6:
            self.current_yaw = 0.0

        command = self._command(
            state=state,
            status=status,
            target_visible=target_visible,
            now=now,
        )
        self.robot.apply_tracking(command)
        self.last_command = command
        self._previous_state = state
        return command

    def reset(self, *, now: float | None = None, immediate: bool = False) -> TrackingCommand:
        self.target_yaw = 0.0
        if immediate:
            self.current_yaw = 0.0
        self.last_seen_center_x = None
        self.last_seen_at = None
        self._locked_yaw = None
        self._last_update_at = now
        self._previous_state = GameState.UNAWARE
        command = self._command(
            state=GameState.UNAWARE,
            status="OFF" if immediate or self.current_yaw == 0 else "RECENTER",
            target_visible=False,
            now=0.0 if now is None else now,
        )
        self.robot.apply_tracking(command)
        self.last_command = command
        return command

    def _state_response_speed(self, state: GameState) -> float:
        if state is GameState.SUSPICIOUS:
            return self.config.suspicious_response_speed
        return self.config.alert_response_speed

    def _last_seen_yaw(self, *, default: float) -> float:
        if self.last_seen_center_x is None:
            return default
        return calculate_target_yaw(self.last_seen_center_x, self.config)

    def _command(
        self,
        *,
        state: GameState,
        status: str,
        target_visible: bool,
        now: float,
    ) -> TrackingCommand:
        strengths = {
            GameState.UNAWARE: 0.0,
            GameState.SUSPICIOUS: 0.35,
            GameState.ALERT: 0.70,
            GameState.FOUND: 1.0,
            GameState.GAME_OVER: 1.0,
        }
        last_seen_seconds = (
            None if self.last_seen_at is None else max(0.0, now - self.last_seen_at)
        )
        return TrackingCommand(
            target_visible=target_visible,
            active=status in {"ACTIVE", "HOLD", "LOCKED"},
            desired_yaw_rad=self.target_yaw,
            actual_yaw_rad=self.current_yaw,
            strength=strengths[state],
            game_state=state.value,
            status=status,
            timestamp=now,
            last_seen_seconds=last_seen_seconds,
        )
