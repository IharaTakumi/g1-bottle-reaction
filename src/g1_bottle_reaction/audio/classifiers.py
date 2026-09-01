from __future__ import annotations

from abc import ABC, abstractmethod
import csv
import os
from pathlib import Path
from typing import Iterable

import numpy as np

from .models import ClassificationResult, Prediction


class AudioClassifier(ABC):
    @abstractmethod
    def classify(self, waveform: np.ndarray, *, sample_rate: int) -> ClassificationResult:
        """Classify normalized mono PCM."""


class YamnetClassifier(AudioClassifier):
    """Official TensorFlow Hub YAMNet with mean frame-score aggregation."""

    def __init__(
        self,
        *,
        model_url: str,
        cache_dir: str | Path,
        music_labels: Iterable[str],
        top_n: int = 3,
    ) -> None:
        os.environ.setdefault("TFHUB_CACHE_DIR", str(Path(cache_dir).resolve()))
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        try:
            import tensorflow as tf
            import tensorflow_hub as hub
        except ImportError as exc:
            raise RuntimeError(
                'YAMNet support requires: pip install -e ".[audio]"'
            ) from exc

        self._tf = tf
        self._model = hub.load(model_url)
        class_map_path = self._model.class_map_path().numpy()
        if isinstance(class_map_path, bytes):
            class_map_path = class_map_path.decode("utf-8")
        with tf.io.gfile.GFile(class_map_path, "r") as csv_file:
            self.class_names = tuple(
                row["display_name"] for row in csv.DictReader(csv_file)
            )
        labels = tuple(music_labels)
        missing = [label for label in labels if label not in self.class_names]
        if missing:
            raise ValueError(f"YAMNet labels not found in official class map: {missing}")
        self._music_indices = tuple(self.class_names.index(label) for label in labels)
        self.top_n = max(1, min(int(top_n), len(self.class_names)))

    @property
    def class_count(self) -> int:
        return len(self.class_names)

    def classify(self, waveform: np.ndarray, *, sample_rate: int) -> ClassificationResult:
        samples = np.asarray(waveform)
        if sample_rate != 16000:
            raise ValueError("YAMNet requires a 16000 Hz waveform")
        if samples.ndim != 1 or samples.dtype != np.float32:
            raise ValueError("YAMNet requires mono float32 waveform")
        if samples.size == 0 or float(samples.min()) < -1 or float(samples.max()) > 1:
            raise ValueError("YAMNet waveform must be non-empty and in [-1, 1]")

        scores, _embeddings, _spectrogram = self._model(samples)
        # Official YAMNet guidance aggregates frame scores to clip scores by mean.
        clip_scores = np.asarray(self._tf.reduce_mean(scores, axis=0), dtype=np.float32)
        best_music_index = max(
            self._music_indices, key=lambda index: float(clip_scores[index])
        )
        music_score = float(clip_scores[best_music_index])
        best_music_label = self.class_names[best_music_index]
        top_indices = np.argsort(clip_scores)[-self.top_n :][::-1]
        predictions = tuple(
            Prediction(self.class_names[int(index)], float(clip_scores[int(index)]))
            for index in top_indices
        )
        return ClassificationResult(music_score, best_music_label, predictions)


class FakeAudioClassifier(AudioClassifier):
    """Deterministic classifier for simulation and tests; imports no TensorFlow."""

    def __init__(self, music_scores: Iterable[float]) -> None:
        self._scores = iter(music_scores)

    def classify(self, waveform: np.ndarray, *, sample_rate: int) -> ClassificationResult:
        del waveform, sample_rate
        score = float(next(self._scores))
        return ClassificationResult(score, "Music", (Prediction("Music", score),))
