from __future__ import annotations

from collections import deque
import math
import random

from .models import (
    LocalObstacleSnapshot,
    OdomSample,
    SafetyState,
    WanderAction,
    WanderConfig,
    WanderDecision,
)


class TrailMemory:
    """Bounded short-term odometry memory; it is deliberately not a map."""

    def __init__(self, config) -> None:
        self.config = config
        self._samples: deque[OdomSample] = deque(maxlen=config.max_samples)

    def add(self, sample: OdomSample) -> None:
        self._samples.append(sample)
        self.prune(sample.timestamp)

    def prune(self, now: float) -> None:
        cutoff = now - self.config.retention_s
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()

    def revisit_penalty(self, x: float, y: float, *, now: float) -> float:
        self.prune(now)
        if not self._samples:
            return 0.0
        nearest = min(math.hypot(x - item.x, y - item.y) for item in self._samples)
        return max(0.0, 1.0 - nearest / self.config.revisit_radius_m)

    @property
    def samples(self) -> tuple[OdomSample, ...]:
        return tuple(self._samples)


class SafetySupervisor:
    def __init__(self, config: WanderConfig) -> None:
        self.config = config

    def evaluate(
        self,
        snapshot: LocalObstacleSnapshot | None,
        pose: OdomSample | None,
        *,
        now: float,
        control_heartbeat_ok: bool = True,
    ) -> tuple[SafetyState, str]:
        if not control_heartbeat_ok:
            return SafetyState.STOP_REQUIRED, "control heartbeat lost"
        if snapshot is None:
            return SafetyState.INVALID, "required point cloud missing"
        if not snapshot.valid:
            return SafetyState.INVALID, snapshot.invalid_reason or "invalid point cloud"
        if set(snapshot.sectors) != {"left", "front_left", "front", "front_right", "right"}:
            return SafetyState.INVALID, "required obstacle sectors missing"
        age = now - snapshot.timestamp
        if age < 0 or age > self.config.safety.stale_after_s:
            return SafetyState.SENSOR_STALE, f"point cloud age {age:.2f}s"
        if pose is None or not all(math.isfinite(value) for value in (pose.x, pose.y, pose.yaw)):
            return SafetyState.INVALID, "required odometry missing or invalid"
        odom_age = now - pose.timestamp
        if odom_age < 0 or odom_age > self.config.safety.odom_stale_after_s:
            return SafetyState.SENSOR_STALE, f"odometry age {odom_age:.2f}s"
        distance_from_start = math.hypot(pose.x, pose.y)
        if distance_from_start >= self.config.leash.hard_limit_m:
            return SafetyState.STOP_REQUIRED, f"hard leash {distance_from_start:.2f}m"
        nearest = min(item.clearance_m for item in snapshot.sectors.values())
        if nearest < self.config.safety.hard_stop_distance_m:
            return SafetyState.STOP_REQUIRED, f"obstacle hard stop {nearest:.2f}m"
        if nearest < self.config.safety.caution_distance_m:
            return SafetyState.CAUTION, f"near obstacle {nearest:.2f}m"
        return SafetyState.SAFE, "sensors fresh and clearance safe"


class WanderPolicy:
    def __init__(self, config: WanderConfig, *, seed: int | None = None) -> None:
        self.config = config
        self._random = random.Random(seed)
        self._last_action = WanderAction.STOP
        self._last_action_at = float("-inf")

    def choose(
        self,
        snapshot: LocalObstacleSnapshot,
        pose: OdomSample,
        trail: TrailMemory,
        *,
        now: float,
        safety: SafetyState,
    ) -> WanderDecision:
        clearances = {
            WanderAction.FORWARD: snapshot.sector("front").clearance_m,
            # v1 turns are in place. The destination side must be clear; the
            # supervisor independently stops for hard-stop proximity in every
            # sector, including the front diagonals swept by the body.
            WanderAction.TURN_LEFT: snapshot.sector("left").clearance_m,
            WanderAction.TURN_RIGHT: snapshot.sector("right").clearance_m,
        }
        candidates = {
            action: clearance
            for action, clearance in clearances.items()
            if clearance >= self.config.policy.blocked_distance_m
        }
        if not candidates:
            self._remember(WanderAction.STOP, now)
            return WanderDecision(WanderAction.STOP, safety, "dead_end_no_safe_direction", {})

        scores = {
            action: self._score(action, clearance, pose, trail, now=now)
            for action, clearance in candidates.items()
        }
        if (
            self._last_action in candidates
            and now - self._last_action_at < self.config.policy.minimum_action_duration_s
        ):
            action = self._last_action
            reason = "minimum_action_duration"
        else:
            action = max(scores, key=scores.get)
            reason = "front_clear" if action is WanderAction.FORWARD else "best_safe_clearance"
        self._remember(action, now)
        return WanderDecision(
            action,
            safety,
            reason,
            {item.value: round(score, 4) for item, score in scores.items()},
        )

    def _score(
        self,
        action: WanderAction,
        clearance: float,
        pose: OdomSample,
        trail: TrailMemory,
        *,
        now: float,
    ) -> float:
        angle = pose.yaw
        turn = math.radians(self.config.policy.turn_angle_degrees)
        if action is WanderAction.TURN_LEFT:
            angle += turn
        elif action is WanderAction.TURN_RIGHT:
            angle -= turn
        step = self.config.policy.candidate_step_m
        candidate_x = pose.x + step * math.cos(angle)
        candidate_y = pose.y + step * math.sin(angle)
        score = min(clearance, self.config.point_cloud.max_range_m) * self.config.policy.clearance_weight
        if action is WanderAction.FORWARD:
            score += self.config.policy.forward_continuity_bonus
        score -= trail.revisit_penalty(candidate_x, candidate_y, now=now) * self.config.policy.revisit_penalty_weight
        current_distance = math.hypot(pose.x, pose.y)
        if current_distance >= self.config.leash.soft_limit_m:
            outward = max(0.0, math.hypot(candidate_x, candidate_y) - current_distance)
            score -= outward * self.config.policy.leash_penalty_weight
        score += self._random.uniform(-self.config.policy.random_bias, self.config.policy.random_bias)
        return score

    def _remember(self, action: WanderAction, now: float) -> None:
        if action is not self._last_action:
            self._last_action_at = now
        self._last_action = action


class MaplessWanderCore:
    """Fail-closed decision-only core. It never sends robot commands."""

    def __init__(self, config: WanderConfig, *, seed: int | None = None) -> None:
        self.config = config
        self.trail = TrailMemory(config.trail)
        self.safety = SafetySupervisor(config)
        self.policy = WanderPolicy(config, seed=seed)

    def decide(
        self,
        snapshot: LocalObstacleSnapshot | None,
        pose: OdomSample | None,
        *,
        now: float,
        control_heartbeat_ok: bool = True,
    ) -> WanderDecision:
        try:
            safety, reason = self.safety.evaluate(
                snapshot,
                pose,
                now=now,
                control_heartbeat_ok=control_heartbeat_ok,
            )
            if safety in {
                SafetyState.INVALID,
                SafetyState.SENSOR_STALE,
                SafetyState.STOP_REQUIRED,
            }:
                return WanderDecision(WanderAction.STOP, safety, reason, {})
            assert snapshot is not None and pose is not None
            decision = self.policy.choose(
                snapshot,
                pose,
                self.trail,
                now=now,
                safety=safety,
            )
            self.trail.add(pose)
            return decision
        except Exception as exc:
            return WanderDecision(
                WanderAction.STOP,
                SafetyState.INVALID,
                f"internal exception: {type(exc).__name__}: {exc}",
                {},
            )
