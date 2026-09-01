from __future__ import annotations

import wave

import numpy as np

from g1_bottle_reaction.audio.classifiers import FakeAudioClassifier
from g1_bottle_reaction.audio.debug import AudioDebugRecorder, format_audio_debug
from g1_bottle_reaction.audio.models import AudioChunk
from g1_bottle_reaction.audio.offline import classify_wav_file
from g1_bottle_reaction.audio.pipeline import AudioProcessor


def test_debug_recorder_writes_16k_mono_pcm_wav(tmp_path) -> None:
    path = tmp_path / "debug.wav"
    recorder = AudioDebugRecorder(path, sample_rate=16000, duration_seconds=0.1)
    recorder.add(np.full(800, 0.25, dtype=np.float32))
    recorder.add(np.full(800, -0.25, dtype=np.float32))
    assert recorder.complete

    with wave.open(str(path), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getnframes() == 1600


def test_processor_exposes_signal_metrics_and_best_music_label(app_config) -> None:
    processor = AudioProcessor(app_config.audio, FakeAudioClassifier([0.7]))
    update = processor.process_chunk(
        AudioChunk(np.full(16000, 0.25, dtype=np.float32), 16000), now=0.0
    )
    assert update is not None
    assert update.rms == 0.25
    assert update.peak_amplitude == 0.25
    assert update.sample_rate == 16000
    assert update.buffer_duration_seconds == 1.0
    assert update.best_music_label == "Music"
    assert "BEST MUSIC LABEL: Music" in format_audio_debug(update)


def test_recorded_wav_can_be_reclassified_without_tensorflow(app_config, tmp_path) -> None:
    path = tmp_path / "replay.wav"
    recorder = AudioDebugRecorder(path, sample_rate=16000, duration_seconds=2.0)
    recorder.add(np.full(32000, 0.2, dtype=np.float32))

    updates = classify_wav_file(
        path,
        config=app_config.audio,
        classifier=FakeAudioClassifier([0.1, 0.6, 0.7]),
    )
    assert len(updates) == 3
    assert all(update.sample_rate == 16000 for update in updates)
    assert all(update.buffer_duration_seconds == 1.0 for update in updates)
    assert updates[-1].best_music_label == "Music"


def test_default_music_label_candidates_are_configured(app_config) -> None:
    assert app_config.audio.yamnet.music_labels == (
        "Music",
        "Musical instrument",
        "Singing",
        "Pop music",
        "Rock music",
        "Hip hop music",
        "Electronic music",
        "Dance music",
        "Background music",
        "Classical music",
    )
    assert app_config.audio.yamnet.top_n == 10
