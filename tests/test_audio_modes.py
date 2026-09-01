from __future__ import annotations

import importlib

import numpy as np

from g1_bottle_reaction.audio.models import AudioChunk
from g1_bottle_reaction.audio.normalization import normalize_audio
from g1_bottle_reaction.audio.sources import (
    AudioSource,
    FallbackAudioSource,
    WindowsMicSource,
    create_windows_mic_source,
)
from g1_bottle_reaction.main import _debug_record_path, build_parser


class FailingRawSource(AudioSource):
    audio_mode_label = "RAW"
    device_label = "raw endpoint"

    def start(self, consumer) -> None:
        del consumer
        raise RuntimeError("raw unsupported")

    def stop(self) -> None:
        return None


class FakeNormalSource(AudioSource):
    audio_mode_label = "NORMAL"
    device_label = "normal endpoint"
    sample_rate = 48000

    def start(self, consumer) -> None:
        consumer(
            AudioChunk(np.ones((4800, 2), dtype=np.float32) * 0.25, 48000)
        )

    def stop(self) -> None:
        return None


def test_audio_mode_config_defaults_to_auto(app_config) -> None:
    assert app_config.audio.mode == "auto"


def test_normal_raw_auto_source_selection() -> None:
    normal = create_windows_mic_source(
        "normal", device=None, target_sample_rate=16000
    )
    raw = create_windows_mic_source("raw", device=None, target_sample_rate=16000)
    auto = create_windows_mic_source("auto", device=None, target_sample_rate=16000)
    assert isinstance(normal, WindowsMicSource)
    assert isinstance(raw, FallbackAudioSource)
    assert isinstance(auto, FallbackAudioSource)


def test_raw_failure_falls_back_and_preserves_downstream_contract() -> None:
    source = FallbackAudioSource(FailingRawSource(), FakeNormalSource())
    chunks: list[AudioChunk] = []
    source.start(chunks.append)
    assert source.audio_mode_label == "NORMAL (RAW unavailable)"
    assert source.device_label == "normal endpoint"
    normalized = normalize_audio(
        chunks[0].waveform,
        source_sample_rate=chunks[0].sample_rate,
        target_sample_rate=16000,
    )
    assert normalized.shape == (1600,)
    assert normalized.dtype == np.float32
    assert float(normalized.min()) >= -1
    assert float(normalized.max()) <= 1


def test_windows_raw_module_import_is_hardware_independent() -> None:
    module = importlib.import_module("g1_bottle_reaction.audio.windows_raw")
    assert module.WindowsRawMicSource is not None


def test_mode_specific_default_recording_paths() -> None:
    parser = build_parser()
    normal = parser.parse_args(
        ["--audio-mode", "normal", "--record-audio-debug"]
    )
    raw = parser.parse_args(["--audio-mode", "raw", "--record-audio-debug"])
    auto = parser.parse_args(["--audio-mode", "auto", "--record-audio-debug"])
    assert _debug_record_path(normal).as_posix() == "debug/audio_normal.wav"
    assert _debug_record_path(raw).as_posix() == "debug/audio_raw.wav"
    assert _debug_record_path(auto).as_posix() == "debug/audio_auto.wav"
