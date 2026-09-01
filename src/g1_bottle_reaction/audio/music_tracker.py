from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from g1_bottle_reaction.config.loader import MusicTrackingConfig
from g1_bottle_reaction.state.events import ReactionEvent

from .models import Prediction


class MusicState(str, Enum):
    QUIET = "QUIET"
    MUSIC_CANDIDATE = "MUSIC_CANDIDATE"
    MUSIC_PLAYING = "MUSIC_PLAYING"
    COOLDOWN = "COOLDOWN"


@dataclass(frozen=True, slots=True)
class AudioTrackingUpdate:
    music_score: float
    state: MusicState
    event: ReactionEvent | None
    top_predictions: tuple[Prediction, ...] = ()
    best_music_label: str = "-"
    rms: float = 0.0
    peak_amplitude: float = 0.0
    sample_rate: int = 16000
    buffer_duration_seconds: float = 0.0
    audio_mode: str = "UNKNOWN"
    device: str = "-"


class MusicStateTracker:
    def __init__(self, config: MusicTrackingConfig) -> None:
        self.config = config
        self.state = MusicState.QUIET
        self._candidate_started_at: float | None = None
        self._lost_started_at: float | None = None
        self._cooldown_until: float | None = None

    def update(
        self,
        music_score: float,
        *,
        now: float,
        top_predictions: tuple[Prediction, ...] = (),
        best_music_label: str = "-",
        rms: float = 0.0,
        peak_amplitude: float = 0.0,
        sample_rate: int = 16000,
        buffer_duration_seconds: float = 0.0,
        audio_mode: str = "UNKNOWN",
        device: str = "-",
    ) -> AudioTrackingUpdate:
        event: ReactionEvent | None = None
        if self.state is MusicState.QUIET:
            if music_score >= self.config.music_start_threshold:
                self.state = MusicState.MUSIC_CANDIDATE
                self._candidate_started_at = now

        elif self.state is MusicState.MUSIC_CANDIDATE:
            if music_score < self.config.music_start_threshold:
                self.state = MusicState.QUIET
                self._candidate_started_at = None
            elif (
                self._candidate_started_at is not None
                and now - self._candidate_started_at
                >= self.config.music_confirm_seconds
            ):
                self.state = MusicState.MUSIC_PLAYING
                self._candidate_started_at = None
                event = ReactionEvent.MUSIC_STARTED

        elif self.state is MusicState.MUSIC_PLAYING:
            if music_score < self.config.music_stop_threshold:
                if self._lost_started_at is None:
                    self._lost_started_at = now
                elif now - self._lost_started_at >= self.config.music_lost_seconds:
                    self.state = MusicState.COOLDOWN
                    self._cooldown_until = now + self.config.music_cooldown_seconds
                    self._lost_started_at = None
                    event = ReactionEvent.MUSIC_STOPPED
            else:
                self._lost_started_at = None

        elif self.state is MusicState.COOLDOWN:
            if self._cooldown_until is not None and now >= self._cooldown_until:
                self._cooldown_until = None
                if music_score >= self.config.music_start_threshold:
                    self.state = MusicState.MUSIC_CANDIDATE
                    self._candidate_started_at = now
                else:
                    self.state = MusicState.QUIET

        return AudioTrackingUpdate(
            music_score=float(music_score),
            state=self.state,
            event=event,
            top_predictions=top_predictions,
            best_music_label=best_music_label,
            rms=float(rms),
            peak_amplitude=float(peak_amplitude),
            sample_rate=int(sample_rate),
            buffer_duration_seconds=float(buffer_duration_seconds),
            audio_mode=audio_mode,
            device=device,
        )
