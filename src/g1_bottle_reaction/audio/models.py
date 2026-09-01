from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class AudioChunk:
    waveform: np.ndarray
    sample_rate: int


@dataclass(frozen=True, slots=True)
class Prediction:
    label: str
    score: float


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    music_score: float
    best_music_label: str
    top_predictions: tuple[Prediction, ...]
