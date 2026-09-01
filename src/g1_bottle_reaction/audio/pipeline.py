from __future__ import annotations

from dataclasses import replace
import logging
import queue
import threading
import time
from typing import Callable

import numpy as np

from g1_bottle_reaction.config.loader import AudioConfig

from .classifiers import AudioClassifier
from .debug import AudioDebugRecorder
from .models import AudioChunk
from .music_tracker import AudioTrackingUpdate, MusicStateTracker
from .normalization import normalize_audio
from .sources import AudioSource

LOGGER = logging.getLogger(__name__)


class AudioProcessor:
    """Windowing and inference; called only from an audio worker, never a callback."""

    def __init__(
        self,
        config: AudioConfig,
        classifier: AudioClassifier,
        *,
        debug_recorder: AudioDebugRecorder | None = None,
    ) -> None:
        self.config = config
        self.classifier = classifier
        self.tracker = MusicStateTracker(config.music_tracking)
        self.debug_recorder = debug_recorder
        self._window_samples = round(
            config.target_sample_rate * config.yamnet.window_seconds
        )
        if self._window_samples <= 0:
            raise ValueError("audio window must contain at least one sample")
        self._buffer = np.empty(0, dtype=np.float32)
        self._last_inference_at = float("-inf")

    def process_chunk(
        self, chunk: AudioChunk, *, now: float
    ) -> AudioTrackingUpdate | None:
        normalized = normalize_audio(
            chunk.waveform,
            source_sample_rate=chunk.sample_rate,
            target_sample_rate=self.config.target_sample_rate,
        )
        if self.debug_recorder is not None:
            self.debug_recorder.add(normalized)
        self._buffer = np.concatenate((self._buffer, normalized))[-self._window_samples :]
        if self._buffer.size < self._window_samples:
            return None
        if (
            now - self._last_inference_at
            < self.config.yamnet.inference_interval_seconds
        ):
            return None
        self._last_inference_at = now
        result = self.classifier.classify(
            self._buffer.copy(), sample_rate=self.config.target_sample_rate
        )
        rms = float(np.sqrt(np.mean(np.square(self._buffer, dtype=np.float64))))
        peak = float(np.max(np.abs(self._buffer)))
        return self.tracker.update(
            result.music_score,
            now=now,
            top_predictions=result.top_predictions,
            best_music_label=result.best_music_label,
            rms=rms,
            peak_amplitude=peak,
            sample_rate=self.config.target_sample_rate,
            buffer_duration_seconds=(
                self._buffer.size / self.config.target_sample_rate
            ),
        )

    def close(self) -> None:
        if self.debug_recorder is not None:
            self.debug_recorder.close()


class AudioMonitor:
    """Bounded callback queue plus a single inference worker."""

    def __init__(
        self,
        source: AudioSource,
        processor: AudioProcessor,
        on_update: Callable[[AudioTrackingUpdate, float], None],
        *,
        queue_max_chunks: int,
    ) -> None:
        self.source = source
        self.processor = processor
        self.on_update = on_update
        self._queue: queue.Queue[tuple[AudioChunk, float] | None] = queue.Queue(
            maxsize=max(1, queue_max_chunks)
        )
        self._worker: threading.Thread | None = None
        self.last_update: AudioTrackingUpdate | None = None
        self.error: Exception | None = None

    def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("Audio monitor is already running")
        self._worker = threading.Thread(
            target=self._run, name="audio-inference-worker", daemon=True
        )
        self._worker.start()
        try:
            self.source.start(self.submit)
        except Exception:
            self._stop_worker()
            raise

    def submit(self, chunk: AudioChunk) -> None:
        item = (chunk, time.monotonic())
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # Favor fresh audio; never block the PortAudio callback.
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                pass

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                chunk, now = item
                update = self.processor.process_chunk(chunk, now=now)
                if update is not None:
                    update = replace(
                        update,
                        audio_mode=self.source.audio_mode_label,
                        device=self.source.device_label,
                    )
                    self.last_update = update
                    self.on_update(update, now)
            except Exception as exc:
                self.error = exc
                LOGGER.exception("Audio inference failed: %s", exc)
            finally:
                self._queue.task_done()

    def stop(self) -> None:
        self.source.stop()
        self._stop_worker()
        self.processor.close()

    def _stop_worker(self) -> None:
        worker = self._worker
        if worker is None:
            return
        while True:
            try:
                self._queue.put_nowait(None)
                break
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    pass
        worker.join(timeout=5)
        self._worker = None
