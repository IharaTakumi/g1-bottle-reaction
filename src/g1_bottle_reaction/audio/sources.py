from __future__ import annotations

from abc import ABC, abstractmethod
import logging
import threading
from typing import Callable, Iterable

import numpy as np

from .models import AudioChunk

LOGGER = logging.getLogger(__name__)
AudioConsumer = Callable[[AudioChunk], None]


class AudioSource(ABC):
    audio_mode_label = "UNKNOWN"
    device_label = "-"

    @abstractmethod
    def start(self, consumer: AudioConsumer) -> None:
        """Begin producing PCM chunks without running inference in callbacks."""

    @abstractmethod
    def stop(self) -> None:
        """Stop producing chunks and release the input device."""


class WindowsMicSource(AudioSource):
    audio_mode_label = "NORMAL"

    def __init__(self, *, device: int | str | None, target_sample_rate: int) -> None:
        self.device = device
        self.target_sample_rate = target_sample_rate
        self.sample_rate: int | None = None
        self.channels = 1
        self._stream: object | None = None
        self.device_label = "default input"

    @staticmethod
    def _sounddevice():
        try:
            import sounddevice
        except ImportError as exc:
            raise RuntimeError(
                'Windows microphone support requires: pip install -e ".[audio]"'
            ) from exc
        return sounddevice

    def start(self, consumer: AudioConsumer) -> None:
        sounddevice = self._sounddevice()
        if self._stream is not None:
            raise RuntimeError("Audio source is already running")
        try:
            resolved_device = self._resolve_device(sounddevice)
            extra_settings = self._extra_settings(sounddevice)
            device_info = sounddevice.query_devices(resolved_device, "input")
            self.device_label = f"{resolved_device}: {device_info['name']}"
            capture_channels = self._capture_channels(device_info)
            fallback_rate = round(float(device_info["default_samplerate"]))
            try:
                sounddevice.check_input_settings(
                    device=resolved_device,
                    channels=capture_channels,
                    dtype="float32",
                    samplerate=self.target_sample_rate,
                    extra_settings=extra_settings,
                )
                sample_rate = self.target_sample_rate
            except (ValueError, sounddevice.PortAudioError):
                sample_rate = fallback_rate

            def callback(indata, frames, time_info, status) -> None:
                del frames, time_info
                if status:
                    LOGGER.warning("Microphone status: %s", status)
                consumer(
                    AudioChunk(
                        waveform=np.asarray(indata, dtype=np.float32).copy(),
                        sample_rate=sample_rate,
                    )
                )

            self._stream = sounddevice.InputStream(
                device=resolved_device,
                channels=capture_channels,
                samplerate=sample_rate,
                dtype="float32",
                callback=callback,
                blocksize=max(1, round(sample_rate * 0.1)),
                extra_settings=extra_settings,
            )
            self._stream.start()
            self.sample_rate = sample_rate
            self.channels = capture_channels
            LOGGER.info(
                "Opened %s float32 audio input %s at %d Hz, %d channel(s)%s",
                self.audio_mode_label,
                self.device_label,
                sample_rate,
                capture_channels,
                " (resampling to 16000 Hz)"
                if sample_rate != self.target_sample_rate
                else "",
            )
        except (ValueError, sounddevice.PortAudioError) as exc:
            stream = self._stream
            self._stream = None
            if stream is not None:
                try:
                    stream.close()
                except sounddevice.PortAudioError:
                    LOGGER.debug("Failed to close rejected audio stream", exc_info=True)
            target = "default input" if self.device is None else repr(self.device)
            raise RuntimeError(f"Could not open audio input device {target}: {exc}") from exc

    def _resolve_device(self, sounddevice):
        del sounddevice
        return self.device

    def _extra_settings(self, sounddevice):
        del sounddevice
        return None

    def _capture_channels(self, device_info) -> int:
        del device_info
        return 1

    def stop(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.stop()
            stream.close()


class SimulatedAudioSource(AudioSource):
    audio_mode_label = "SIMULATED"
    device_label = "simulated"

    def __init__(self, chunks: Iterable[AudioChunk]) -> None:
        self._chunks = tuple(chunks)
        self._thread: threading.Thread | None = None

    def start(self, consumer: AudioConsumer) -> None:
        if self._thread is not None:
            raise RuntimeError("Audio source is already running")

        def emit() -> None:
            for chunk in self._chunks:
                consumer(chunk)

        self._thread = threading.Thread(target=emit, name="simulated-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None


class G1MicSource(AudioSource):
    """Placeholder boundary; no Unitree microphone API is guessed here."""

    audio_mode_label = "G1"
    device_label = "G1 microphone (not implemented)"

    def start(self, consumer: AudioConsumer) -> None:
        del consumer
        raise RuntimeError(
            "G1MicSource is not implemented; verify the official microphone API on hardware"
        )

    def stop(self) -> None:
        return None


class FallbackAudioSource(AudioSource):
    """Tries RAW once, then keeps a normal source active for this run only."""

    def __init__(self, primary: AudioSource, fallback: AudioSource) -> None:
        self.primary = primary
        self.fallback = fallback
        self._active: AudioSource | None = None
        self._sample_rate: int | None = None
        self.audio_mode_label = "RAW requested"
        self.device_label = "-"

    @property
    def sample_rate(self) -> int | None:
        if self._active is not None:
            return getattr(self._active, "sample_rate", None)
        return self._sample_rate

    def start(self, consumer: AudioConsumer) -> None:
        self._active = self.primary
        try:
            self.primary.start(consumer)
            self.audio_mode_label = self.primary.audio_mode_label
        except RuntimeError as exc:
            LOGGER.warning(
                "WASAPI RAW capture is unavailable (%s). Falling back to normal Windows microphone capture.",
                exc,
            )
            self._active = self.fallback
            self.fallback.start(consumer)
            self.audio_mode_label = "NORMAL (RAW unavailable)"
        self.device_label = self._active.device_label
        self._sample_rate = getattr(self._active, "sample_rate", None)

    def stop(self) -> None:
        if self._active is not None:
            self._active.stop()
            self._active = None


def create_windows_mic_source(
    mode: str,
    *,
    device: int | str | None,
    target_sample_rate: int,
) -> AudioSource:
    normal = WindowsMicSource(
        device=device, target_sample_rate=target_sample_rate
    )
    if mode == "normal":
        return normal
    if mode not in {"raw", "auto"}:
        raise ValueError(f"Unknown Windows audio mode: {mode}")
    from .windows_raw import WindowsRawMicSource

    raw = WindowsRawMicSource(
        device=device, target_sample_rate=target_sample_rate
    )
    return FallbackAudioSource(raw, normal)


def list_input_devices() -> list[str]:
    sounddevice = WindowsMicSource._sounddevice()
    try:
        devices = sounddevice.query_devices()
        hostapis = sounddevice.query_hostapis()
        default_input = int(sounddevice.default.device[0])
    except (ValueError, sounddevice.PortAudioError) as exc:
        raise RuntimeError(f"Could not query audio devices: {exc}") from exc
    lines: list[str] = []
    for index, info in enumerate(devices):
        if int(info["max_input_channels"]) <= 0:
            continue
        marker = "*" if index == default_input else " "
        lines.append(
            f"{marker} {index}: {info['name']} "
            f"(inputs={int(info['max_input_channels'])}, "
            f"default_rate={round(float(info['default_samplerate']))}, "
            f"host_api={hostapis[int(info['hostapi'])]['name']})"
        )
    return lines
