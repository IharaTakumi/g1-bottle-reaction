from __future__ import annotations

import logging
from pathlib import Path
import wave

import numpy as np

from .music_tracker import AudioTrackingUpdate

LOGGER = logging.getLogger(__name__)


class AudioDebugRecorder:
    """Records the exact normalized PCM stream presented to audio windowing."""

    def __init__(self, path: str | Path, *, sample_rate: int, duration_seconds: float) -> None:
        if sample_rate <= 0 or duration_seconds <= 0:
            raise ValueError("debug recording sample rate and duration must be positive")
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.target_samples = round(sample_rate * duration_seconds)
        self._chunks: list[np.ndarray] = []
        self._sample_count = 0
        self._saved = False

    @property
    def complete(self) -> bool:
        return self._saved

    def add(self, waveform: np.ndarray) -> None:
        if self._saved:
            return
        remaining = self.target_samples - self._sample_count
        if remaining <= 0:
            self._save()
            return
        chunk = np.asarray(waveform, dtype=np.float32).reshape(-1)[:remaining].copy()
        if chunk.size:
            self._chunks.append(chunk)
            self._sample_count += chunk.size
        if self._sample_count >= self.target_samples:
            self._save()

    def close(self) -> None:
        if not self._saved and self._sample_count:
            self._save()

    def _save(self) -> None:
        samples = np.concatenate(self._chunks) if self._chunks else np.empty(0)
        pcm16 = np.rint(np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self.path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(pcm16.tobytes())
        self._saved = True
        self._chunks.clear()
        LOGGER.info(
            "Saved YAMNet input debug WAV: %s (%.2f s, %d Hz mono)",
            self.path.resolve(),
            pcm16.size / self.sample_rate,
            self.sample_rate,
        )


def format_audio_debug(update: AudioTrackingUpdate) -> str:
    lines = [
        "[AUDIO DEBUG]",
        f"  AUDIO MODE: {update.audio_mode}",
        f"  DEVICE: {update.device}",
        f"  MUSIC SCORE: {update.music_score:.4f}",
        f"  BEST MUSIC LABEL: {update.best_music_label}",
        f"  MUSIC STATE: {update.state.value}",
        f"  RMS: {update.rms:.6f}",
        f"  PEAK: {update.peak_amplitude:.6f}",
        f"  SAMPLE RATE: {update.sample_rate} Hz",
        f"  BUFFER DURATION: {update.buffer_duration_seconds:.3f} s",
        "  TOP YAMNET PREDICTIONS:",
    ]
    lines.extend(
        f"    {index:2d}. {prediction.label}: {prediction.score:.4f}"
        for index, prediction in enumerate(update.top_predictions[:10], start=1)
    )
    return "\n".join(lines)
