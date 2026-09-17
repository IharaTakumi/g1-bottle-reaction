from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


SECTOR_NAMES = ("left", "front_left", "front", "front_right", "right")


class WanderAction(str, Enum):
    STOP = "STOP"
    FORWARD = "FORWARD"
    TURN_LEFT = "TURN_LEFT"
    TURN_RIGHT = "TURN_RIGHT"


class SafetyState(str, Enum):
    SAFE = "SAFE"
    CAUTION = "CAUTION"
    STOP_REQUIRED = "STOP_REQUIRED"
    SENSOR_STALE = "SENSOR_STALE"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class SectorObservation:
    clearance_m: float
    valid_point_count: int
    blocked: bool


@dataclass(frozen=True, slots=True)
class LocalObstacleSnapshot:
    timestamp: float
    sectors: Mapping[str, SectorObservation]
    valid: bool = True
    invalid_reason: str | None = None

    def sector(self, name: str) -> SectorObservation:
        return self.sectors[name]

    @classmethod
    def from_clearances(
        cls,
        timestamp: float,
        clearances: Mapping[str, float],
        *,
        blocked_distance_m: float,
        valid_point_count: int = 10,
        valid: bool = True,
        invalid_reason: str | None = None,
    ) -> "LocalObstacleSnapshot":
        missing = set(SECTOR_NAMES) - set(clearances)
        if missing:
            raise ValueError(f"Missing obstacle sectors: {sorted(missing)}")
        return cls(
            timestamp=timestamp,
            sectors={
                name: SectorObservation(
                    clearance_m=float(clearances[name]),
                    valid_point_count=valid_point_count,
                    blocked=float(clearances[name]) < blocked_distance_m,
                )
                for name in SECTOR_NAMES
            },
            valid=valid,
            invalid_reason=invalid_reason,
        )


@dataclass(frozen=True, slots=True)
class OdomSample:
    x: float
    y: float
    yaw: float
    timestamp: float


@dataclass(frozen=True, slots=True)
class WanderDecision:
    action: WanderAction
    safety: SafetyState
    reason: str
    scores: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class PointCloudConfig:
    forward_axis: str
    left_axis: str
    up_axis: str
    min_height_m: float
    max_height_m: float
    min_range_m: float
    max_range_m: float
    self_mask_front_m: float
    self_mask_back_m: float
    self_mask_half_width_m: float
    sector_edges_degrees: tuple[float, float, float, float, float, float]
    minimum_valid_points: int

    def validate(self) -> None:
        axes = [self.forward_axis.lstrip("+-"), self.left_axis.lstrip("+-"), self.up_axis.lstrip("+-")]
        if sorted(axes) != ["x", "y", "z"]:
            raise ValueError("wander point_cloud axes must use x, y, z exactly once")
        if not self.min_height_m < self.max_height_m:
            raise ValueError("wander point_cloud height band is invalid")
        if not 0 <= self.min_range_m < self.max_range_m:
            raise ValueError("wander point_cloud range band is invalid")
        if len(self.sector_edges_degrees) != 6 or tuple(sorted(self.sector_edges_degrees)) != self.sector_edges_degrees:
            raise ValueError("wander point_cloud sector edges must be six ascending values")
        if self.minimum_valid_points <= 0:
            raise ValueError("wander point_cloud minimum_valid_points must be positive")


@dataclass(frozen=True, slots=True)
class WanderPolicyConfig:
    blocked_distance_m: float
    forward_continuity_bonus: float
    random_bias: float
    clearance_weight: float
    revisit_penalty_weight: float
    leash_penalty_weight: float
    candidate_step_m: float
    turn_angle_degrees: float
    minimum_action_duration_s: float


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    stale_after_s: float
    odom_stale_after_s: float
    caution_distance_m: float
    hard_stop_distance_m: float


@dataclass(frozen=True, slots=True)
class TrailConfig:
    retention_s: float
    max_samples: int
    revisit_radius_m: float


@dataclass(frozen=True, slots=True)
class LeashConfig:
    soft_limit_m: float
    hard_limit_m: float


@dataclass(frozen=True, slots=True)
class WanderConfig:
    point_cloud: PointCloudConfig
    policy: WanderPolicyConfig
    safety: SafetyConfig
    trail: TrailConfig
    leash: LeashConfig

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WanderConfig":
        cloud = dict(value["point_cloud"])
        cloud["sector_edges_degrees"] = tuple(float(item) for item in cloud["sector_edges_degrees"])
        config = cls(
            point_cloud=PointCloudConfig(**cloud),
            policy=WanderPolicyConfig(**value["policy"]),
            safety=SafetyConfig(**value["safety"]),
            trail=TrailConfig(**value["trail"]),
            leash=LeashConfig(**value["leash"]),
        )
        config.validate()
        return config

    def validate(self) -> None:
        self.point_cloud.validate()
        if min(
            self.policy.blocked_distance_m,
            self.policy.clearance_weight,
            self.policy.candidate_step_m,
            self.policy.turn_angle_degrees,
            self.policy.minimum_action_duration_s,
            self.safety.stale_after_s,
            self.safety.odom_stale_after_s,
            self.safety.caution_distance_m,
            self.safety.hard_stop_distance_m,
            self.trail.retention_s,
            self.trail.revisit_radius_m,
            self.leash.soft_limit_m,
            self.leash.hard_limit_m,
        ) <= 0:
            raise ValueError("wander distances, durations, and positive weights must be positive")
        if self.safety.hard_stop_distance_m >= self.policy.blocked_distance_m:
            raise ValueError("wander hard stop distance must be below blocked distance")
        if self.leash.soft_limit_m >= self.leash.hard_limit_m:
            raise ValueError("wander soft leash must be below hard leash")
        if self.trail.max_samples <= 0:
            raise ValueError("wander trail max_samples must be positive")
