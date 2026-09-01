from __future__ import annotations

import sys

from .sources import WindowsMicSource


class RawCaptureUnavailable(RuntimeError):
    pass


class _SoundDeviceWasapiRawSettings:
    """Isolated sounddevice-private bridge for PortAudio WASAPI RAW.

    sounddevice 0.5.6 publicly accepts host-specific ``extra_settings`` but its
    public ``WasapiSettings`` constructor does not expose ``streamOption``.
    Its bundled CFFI ABI does expose the official PortAudio fields and enum, so
    this object builds the same struct shape and is consumed through the public
    InputStream(extra_settings=...) parameter.
    """

    def __init__(self, sounddevice) -> None:
        try:
            ffi = sounddevice._ffi
            lib = sounddevice._lib
            raw_option = lib.eStreamOptionRaw
            media_category = lib.eAudioCategoryMedia
        except AttributeError as exc:
            raise RawCaptureUnavailable(
                "installed sounddevice/PortAudio does not expose WASAPI streamOption"
            ) from exc
        self._streaminfo = ffi.new(
            "PaWasapiStreamInfo*",
            dict(
                size=ffi.sizeof("PaWasapiStreamInfo"),
                hostApiType=lib.paWASAPI,
                version=1,
                flags=0,
                streamCategory=media_category,
                streamOption=raw_option,
            ),
        )


class WindowsRawMicSource(WindowsMicSource):
    """Shared-mode WASAPI capture requesting eStreamOptionRaw for this stream."""

    audio_mode_label = "RAW"

    def _resolve_device(self, sounddevice):
        if sys.platform != "win32":
            raise RawCaptureUnavailable("WASAPI RAW is only available on Windows")
        hostapis = sounddevice.query_hostapis()
        wasapi_indices = [
            index
            for index, info in enumerate(hostapis)
            if info["name"] == "Windows WASAPI"
        ]
        if not wasapi_indices:
            raise RawCaptureUnavailable("PortAudio has no Windows WASAPI host API")
        wasapi_index = wasapi_indices[0]

        if self.device is None:
            device = int(hostapis[wasapi_index]["default_input_device"])
            if device < 0:
                raise RawCaptureUnavailable("WASAPI has no default input endpoint")
        elif isinstance(self.device, int):
            device = self.device
        else:
            needle = self.device.casefold()
            matches = [
                index
                for index, info in enumerate(sounddevice.query_devices())
                if int(info["max_input_channels"]) > 0
                and needle in str(info["name"]).casefold()
                and int(info["hostapi"]) == wasapi_index
            ]
            if len(matches) != 1:
                raise RawCaptureUnavailable(
                    f"audio device name must match exactly one WASAPI input; matches={matches}"
                )
            device = matches[0]

        info = sounddevice.query_devices(device, "input")
        if int(info["hostapi"]) != wasapi_index:
            raise RawCaptureUnavailable(
                f"device {device} is not a Windows WASAPI input endpoint"
            )
        return device

    def _extra_settings(self, sounddevice):
        return _SoundDeviceWasapiRawSettings(sounddevice)

    def _capture_channels(self, device_info) -> int:
        # RAW endpoints can require their native channel layout. The existing
        # normalizer collapses this waveform to mono before YAMNet.
        return int(device_info["max_input_channels"])
