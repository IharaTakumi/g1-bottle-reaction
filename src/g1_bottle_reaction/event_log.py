from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock

from g1_bottle_reaction.reactions.models import Reaction
from g1_bottle_reaction.state.bottle_tracker import TrackingUpdate
from g1_bottle_reaction.state.events import ReactionEvent
from g1_bottle_reaction.stealth.models import GameEvent, GameUpdate


class JsonlEventLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def write(self, update: TrackingUpdate, reaction: Reaction | None) -> None:
        if update.event is None:
            return
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "vision",
            "state": update.state.value,
            "event": update.event.value,
            "confidence": update.confidence,
            "proximity_ratio": update.proximity_ratio,
            "reaction": reaction.motion if reaction is not None else None,
            "speech": reaction.speech if reaction is not None else None,
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.write("\n")

    def write_audio(
        self,
        *,
        event: ReactionEvent,
        music_score: float,
        music_state: str,
        top_labels: list[dict[str, str | float]],
        best_music_label: str,
        rms: float,
        peak_amplitude: float,
        sample_rate: int,
        buffer_duration_seconds: float,
        reaction: Reaction | None,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "audio",
            "event": event.value,
            "music_score": music_score,
            "music_state": music_state,
            "top_labels": top_labels,
            "best_music_label": best_music_label,
            "rms": rms,
            "peak_amplitude": peak_amplitude,
            "sample_rate": sample_rate,
            "buffer_duration_seconds": buffer_duration_seconds,
            "reaction": reaction.motion if reaction is not None else None,
            "speech": reaction.speech if reaction is not None else None,
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.write("\n")

    def write_game(
        self,
        *,
        update: GameUpdate,
        event: GameEvent,
        tracking_yaw_radians: float,
        reaction: Reaction | None,
    ) -> None:
        observation = update.observation
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "stealth_game",
            "event": event.value,
            "game_state": update.state.value,
            "suspicion": update.suspicion,
            "visibility_score": update.visibility_score,
            "semantic_role": (
                observation.semantic_role.value if observation is not None else None
            ),
            "raw_detector_label": (
                observation.raw_detector_label if observation is not None else None
            ),
            "confidence": observation.confidence if observation is not None else 0.0,
            "center_x_normalized": (
                observation.center_x_normalized if observation is not None else 0.0
            ),
            "bbox_area_ratio": (
                observation.bbox_area_ratio if observation is not None else 0.0
            ),
            "tracking_yaw_radians": tracking_yaw_radians,
            "reaction": reaction.motion if reaction is not None else None,
            "speech": reaction.speech if reaction is not None else None,
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.write("\n")
