from __future__ import annotations

import math
from pathlib import Path
import time
from typing import Any
import wave

import numpy as np

from .aivis_speech import AudioOutput
from .g1_robot import DEFAULT_UNITREE_RUNTIME, ensure_unitree_success

TARGET_SAMPLE_RATE = 16_000


class G1AudioOutput(AudioOutput):
    """Play synthesized WAV files through the official G1 AudioClient."""

    def __init__(
        self,
        network_interface: str,
        *,
        network_address: str | None = None,
        volume: int = 85,
        timeout_seconds: float = 10.0,
        chunk_bytes: int = 96_000,
        chunk_delay_seconds: float = 1.0,
        runtime: Any | None = None,
    ) -> None:
        if not (network_interface or network_address):
            raise ValueError(
                "--network-interface or --network-address is required for G1 speaker output"
            )
        if not 0 <= volume <= 100:
            raise ValueError("G1 speaker volume must be between 0 and 100")
        if chunk_bytes <= 0 or chunk_delay_seconds < 0:
            raise ValueError("G1 audio chunk settings are invalid")
        self.network_interface = network_interface
        self.network_address = network_address
        self.volume = volume
        self.timeout_seconds = timeout_seconds
        self.chunk_bytes = chunk_bytes
        self.chunk_delay_seconds = chunk_delay_seconds
        self.runtime = runtime or DEFAULT_UNITREE_RUNTIME
        self._client: Any | None = None

    def initialize(self) -> None:
        if self._client is not None:
            return
        if self.network_address is None:
            client = self.runtime.create_audio_client(
                self.network_interface, self.timeout_seconds
            )
        else:
            client = self.runtime.create_audio_client(
                self.network_interface,
                self.timeout_seconds,
                self.network_address,
            )
        ensure_unitree_success(client.SetVolume(self.volume), "SetVolume")
        self._client = client

    def play_wav(self, path: Path) -> None:
        pcm = wav_to_pcm16_mono_16k(path)
        self.initialize()
        assert self._client is not None
        app_name = "g1_bottle_reaction"
        stream_id = str(time.time_ns() // 1_000_000)
        try:
            for offset in range(0, len(pcm), self.chunk_bytes):
                chunk = pcm[offset : offset + self.chunk_bytes]
                result = self._client.PlayStream(app_name, stream_id, chunk)
                ensure_unitree_success(result, "PlayStream")
                if self.chunk_delay_seconds:
                    time.sleep(self.chunk_delay_seconds)
        finally:
            if self._client is not None:
                ensure_unitree_success(self._client.PlayStop(app_name), "PlayStop")


def wav_to_pcm16_mono_16k(path: str | Path) -> bytes:
    """Decode PCM WAV and return 16 kHz mono signed little-endian int16."""

    wav_path = Path(path)
    try:
        with wave.open(str(wav_path), "rb") as source:
            if source.getcomptype() != "NONE":
                raise ValueError("compressed WAV is not supported")
            channels = source.getnchannels()
            sample_rate = source.getframerate()
            sample_width = source.getsampwidth()
            frame_count = source.getnframes()
            raw = source.readframes(frame_count)
    except (OSError, EOFError, wave.Error) as exc:
        raise RuntimeError(f"Could not read WAV file {wav_path}: {exc}") from exc
    if channels < 1 or sample_rate < 1 or sample_width not in {1, 2, 3, 4}:
        raise ValueError(
            f"Unsupported WAV format: {sample_rate} Hz, {channels} channel(s), "
            f"{sample_width * 8}-bit PCM"
        )
    samples = _decode_pcm(raw, sample_width)
    if samples.size % channels:
        raise ValueError("WAV PCM data is not aligned to its channel count")
    mono = samples.reshape(-1, channels).mean(axis=1, dtype=np.float32)
    if sample_rate != TARGET_SAMPLE_RATE and mono.size:
        mono = _resample(mono, sample_rate, TARGET_SAMPLE_RATE)
    pcm16 = np.rint(np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2")
    return pcm16.tobytes()


def _resample(waveform: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    try:
        from scipy.signal import resample_poly
    except ImportError:
        output_count = max(1, round(waveform.size * target_rate / source_rate))
        source_positions = np.arange(waveform.size, dtype=np.float64)
        output_positions = np.arange(output_count, dtype=np.float64) * (
            source_rate / target_rate
        )
        return np.interp(output_positions, source_positions, waveform).astype(
            np.float32
        )
    divisor = math.gcd(source_rate, target_rate)
    return np.asarray(
        resample_poly(
            waveform,
            target_rate // divisor,
            source_rate // divisor,
            padtype="line",
        ),
        dtype=np.float32,
    )


def _decode_pcm(raw: bytes, sample_width: int) -> np.ndarray:
    if sample_width == 1:
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if sample_width == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if sample_width == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    packed = np.frombuffer(raw, dtype=np.uint8)
    if packed.size % 3:
        raise ValueError("24-bit WAV data has an incomplete sample")
    triplets = packed.reshape(-1, 3).astype(np.int32)
    values = triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)
    values = np.where(values & 0x800000, values - 0x1000000, values)
    return values.astype(np.float32) / 8388608.0
