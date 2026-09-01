from __future__ import annotations

from enum import Enum


class BottleState(str, Enum):
    NO_BOTTLE = "NO_BOTTLE"
    FAR = "FAR"
    NEAR = "NEAR"
    TOO_CLOSE = "TOO_CLOSE"
    LOST = "LOST"


class ReactionEvent(str, Enum):
    FOUND = "FOUND"
    NEAR = "NEAR"
    TOO_CLOSE = "TOO_CLOSE"
    LOST = "LOST"
    FOUND_AGAIN = "FOUND_AGAIN"
    MUSIC_STARTED = "MUSIC_STARTED"
    MUSIC_STOPPED = "MUSIC_STOPPED"
    SUSPICION_STARTED = "SUSPICION_STARTED"
    ALERT_STARTED = "ALERT_STARTED"
    PLAYER_LOST = "PLAYER_LOST"
    RETURNED_TO_UNAWARE = "RETURNED_TO_UNAWARE"
    PLAYER_FOUND = "PLAYER_FOUND"
    GAME_OVER = "GAME_OVER"


# Backward-compatible name retained for the existing bottle tracker and tests.
BottleEvent = ReactionEvent
