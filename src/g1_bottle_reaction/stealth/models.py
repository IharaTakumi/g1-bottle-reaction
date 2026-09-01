from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TargetRole(str, Enum):
    PLAYER = "PLAYER"


class GameState(str, Enum):
    UNAWARE = "UNAWARE"
    SUSPICIOUS = "SUSPICIOUS"
    ALERT = "ALERT"
    FOUND = "FOUND"
    GAME_OVER = "GAME_OVER"


class GameEvent(str, Enum):
    SUSPICION_STARTED = "SUSPICION_STARTED"
    ALERT_STARTED = "ALERT_STARTED"
    PLAYER_LOST = "PLAYER_LOST"
    RETURNED_TO_UNAWARE = "RETURNED_TO_UNAWARE"
    PLAYER_FOUND = "PLAYER_FOUND"
    GAME_OVER = "GAME_OVER"


@dataclass(frozen=True, slots=True)
class TargetObservation:
    visible: bool
    confidence: float
    bbox: tuple[int, int, int, int] | None
    center_x_normalized: float
    center_y_normalized: float
    bbox_area_ratio: float
    timestamp: float
    raw_detector_label: str
    semantic_role: TargetRole

    @classmethod
    def missing(
        cls,
        *,
        timestamp: float,
        raw_detector_label: str,
        semantic_role: TargetRole = TargetRole.PLAYER,
    ) -> "TargetObservation":
        return cls(
            visible=False,
            confidence=0.0,
            bbox=None,
            center_x_normalized=0.0,
            center_y_normalized=0.0,
            bbox_area_ratio=0.0,
            timestamp=timestamp,
            raw_detector_label=raw_detector_label,
            semantic_role=semantic_role,
        )


@dataclass(frozen=True, slots=True)
class GameUpdate:
    state: GameState
    suspicion: float
    visibility_score: float
    target_visible: bool
    observation: TargetObservation | None
    events: tuple[GameEvent, ...]

