from __future__ import annotations

import importlib
from pathlib import Path
import wave

import numpy as np

from g1_bottle_reaction.adapters.g1_audio import (
    G1AudioOutput,
    wav_to_pcm16_mono_16k,
)


class FakeAudioClient:
    def __init__(self) -> None:
        self.volume: int | None = None
        self.chunks: list[tuple[str, str, bytes]] = []
        self.stopped: list[str] = []

    def SetVolume(self, volume: int) -> int:
        self.volume = volume
        return 0

    def PlayStream(self, app_name: str, stream_id: str, pcm: bytes):
        self.chunks.append((app_name, stream_id, pcm))
        return 0, None

    def PlayStop(self, app_name: str) -> int:
        self.stopped.append(app_name)
        return 0


class FakeRuntime:
    def __init__(self) -> None:
        self.client = FakeAudioClient()
        self.calls: list[tuple[str, float]] = []

    def create_audio_client(self, interface: str, timeout: float):
        self.calls.append((interface, timeout))
        return self.client


def _write_stereo_wav(path: Path, sample_rate: int = 48_000) -> None:
    frames = sample_rate // 10
    left = np.full(frames, 16384, dtype="<i2")
    right = np.full(frames, -8192, dtype="<i2")
    stereo = np.column_stack((left, right)).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(stereo)


def test_wav_conversion_is_16khz_mono_pcm16(tmp_path: Path) -> None:
    path = tmp_path / "stereo-48k.wav"
    _write_stereo_wav(path)
    pcm = wav_to_pcm16_mono_16k(path)
    samples = np.frombuffer(pcm, dtype="<i2")
    assert samples.shape == (1600,)
    assert samples.dtype == np.dtype("<i2")
    assert np.allclose(samples.mean(), 4096, atol=2)


def test_g1_audio_output_uses_official_stream_calls(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _write_stereo_wav(path)
    runtime = FakeRuntime()
    output = G1AudioOutput(
        "eth-test",
        volume=72,
        timeout_seconds=4.0,
        chunk_bytes=1000,
        chunk_delay_seconds=0,
        runtime=runtime,
    )
    output.play_wav(path)
    assert runtime.calls == [("eth-test", 4.0)]
    assert runtime.client.volume == 72
    assert len(runtime.client.chunks) == 4
    assert {chunk[0] for chunk in runtime.client.chunks} == {"g1_bottle_reaction"}
    assert len({chunk[1] for chunk in runtime.client.chunks}) == 1
    assert runtime.client.stopped == ["g1_bottle_reaction"]


def test_g1_audio_module_import_does_not_require_unitree_sdk() -> None:
    module = importlib.import_module("g1_bottle_reaction.adapters.g1_audio")
    assert module.G1AudioOutput is not None


def test_g1_audio_can_preserve_existing_volume(tmp_path: Path) -> None:
    path = tmp_path / "voice.wav"
    _write_stereo_wav(path)
    runtime = FakeRuntime()
    output = G1AudioOutput("eth-test", volume=None, chunk_delay_seconds=0, runtime=runtime)
    output.play_wav(path)
    assert runtime.client.volume is None
    assert runtime.client.chunks
