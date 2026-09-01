from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
import wave

import numpy as np

from g1_bottle_reaction.config.loader import AudioConfig

from .classifiers import AudioClassifier
from .debug import format_audio_debug
from .music_tracker import AudioTrackingUpdate, MusicStateTracker
from .normalization import normalize_audio

if TYPE_CHECKING:
    from g1_bottle_reaction.app import BottleReactionApp


def classify_wav_file(
    path: str | Path,
    *,
    config: AudioConfig,
    classifier: AudioClassifier,
    app: BottleReactionApp | None = None,
) -> list[AudioTrackingUpdate]:
    wav_path = Path(path)
    if not wav_path.is_file():
        raise ValueError(f"Audio file does not exist: {wav_path}")
    try:
        source_rate, source_waveform = _read_pcm_wav(wav_path)
    except (OSError, ValueError, wave.Error) as exc:
        raise ValueError(f"Could not read WAV file {wav_path}: {exc}") from exc

    waveform = normalize_audio(
        source_waveform,
        source_sample_rate=int(source_rate),
        target_sample_rate=config.target_sample_rate,
    )
    if not waveform.size:
        raise ValueError(f"WAV file contains no audio samples: {wav_path}")

    window_samples = round(config.target_sample_rate * config.yamnet.window_seconds)
    hop_samples = round(
        config.target_sample_rate * config.yamnet.inference_interval_seconds
    )
    if window_samples <= 0 or hop_samples <= 0:
        raise ValueError("audio window and inference interval must be positive")
    starts = list(range(0, max(1, waveform.size - window_samples + 1), hop_samples))
    if not starts:
        starts = [0]

    tracker = MusicStateTracker(config.music_tracking)
    updates: list[AudioTrackingUpdate] = []
    print(
        f"[AUDIO FILE] {wav_path.resolve()} source_rate={source_rate} "
        f"duration={waveform.size / config.target_sample_rate:.3f}s",
        flush=True,
    )
    for start in starts:
        window = waveform[start : start + window_samples]
        if window.size < window_samples:
            window = np.pad(window, (0, window_samples - window.size))
        result = classifier.classify(window, sample_rate=config.target_sample_rate)
        rms = float(np.sqrt(np.mean(np.square(window, dtype=np.float64))))
        peak = float(np.max(np.abs(window)))
        now = start / config.target_sample_rate
        update = tracker.update(
            result.music_score,
            now=now,
            top_predictions=result.top_predictions,
            best_music_label=result.best_music_label,
            rms=rms,
            peak_amplitude=peak,
            sample_rate=config.target_sample_rate,
            buffer_duration_seconds=window.size / config.target_sample_rate,
            audio_mode="FILE",
            device=str(wav_path.resolve()),
        )
        updates.append(update)
        if app is not None:
            app.process_audio(update, now=now)
        print(f"  WINDOW START: {now:.3f} s", flush=True)
        print(format_audio_debug(update), flush=True)
    return updates


def _read_pcm_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as wav_file:
        if wav_file.getcomptype() != "NONE":
            raise ValueError("only uncompressed PCM WAV files are supported")
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())
    if channels <= 0:
        raise ValueError("WAV channel count must be positive")

    if sample_width == 1:
        samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif sample_width == 2:
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768
    elif sample_width == 3:
        bytes_ = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = bytes_[:, 0] | (bytes_[:, 1] << 8) | (bytes_[:, 2] << 16)
        values = (values ^ 0x800000) - 0x800000
        samples = values.astype(np.float32) / 8388608
    elif sample_width == 4:
        samples = np.frombuffer(frames, dtype="<i4").astype(np.float64) / 2147483648
    else:
        raise ValueError(f"unsupported PCM sample width: {sample_width} bytes")
    return sample_rate, samples.reshape(-1, channels)
