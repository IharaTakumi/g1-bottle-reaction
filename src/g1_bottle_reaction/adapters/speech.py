from __future__ import annotations

from abc import ABC, abstractmethod
import logging
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from g1_bottle_reaction.config.loader import AivisSpeechConfig

LOGGER = logging.getLogger(__name__)


class SpeechBackend(ABC):
    @abstractmethod
    def speak(self, text: str, *, voice_profile: str = "neutral") -> None:
        """Speak or otherwise emit text."""


class ConsoleSpeechBackend(SpeechBackend):
    def speak(self, text: str, *, voice_profile: str = "neutral") -> None:
        del voice_profile
        print(f"[SPEECH] {text}", flush=True)


class MuteSpeechBackend(SpeechBackend):
    def speak(self, text: str, *, voice_profile: str = "neutral") -> None:
        del text, voice_profile
        return None


class WindowsTtsSpeechBackend(SpeechBackend):
    """Windows System.Speech backend with console fallback."""

    _SPEAK_SCRIPT = (
        "Add-Type -AssemblyName System.Speech; "
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$v=@($s.GetInstalledVoices() | Where-Object "
        "{$_.VoiceInfo.Culture.Name -like 'ja-*'}); "
        "$s.SelectVoice($v[0].VoiceInfo.Name); "
        "$t=[Console]::In.ReadToEnd(); $s.Speak($t)"
    )
    _JAPANESE_VOICE_SCRIPT = (
        "Add-Type -AssemblyName System.Speech; "
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$v=@($s.GetInstalledVoices() | Where-Object "
        "{$_.VoiceInfo.Culture.Name -like 'ja-*'}); "
        "if($v.Count -gt 0){exit 0}else{exit 2}"
    )

    def __init__(self, fallback: SpeechBackend | None = None) -> None:
        self.fallback = fallback or ConsoleSpeechBackend()
        if sys.platform != "win32" or not self.is_available():
            raise RuntimeError("A Japanese Windows TTS voice is not available")

    @classmethod
    def is_available(cls) -> bool:
        if sys.platform != "win32":
            return False
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    cls._JAPANESE_VOICE_SCRIPT,
                ],
                check=False,
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def speak(self, text: str, *, voice_profile: str = "neutral") -> None:
        del voice_profile
        try:
            subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    self._SPEAK_SCRIPT,
                ],
                input=text,
                text=True,
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            self.fallback.speak(text)


def create_speech_backend(
    mode: str,
    *,
    aivis_config: "AivisSpeechConfig | None" = None,
    debug: bool = False,
) -> SpeechBackend:
    if mode == "console":
        return ConsoleSpeechBackend()
    if mode == "mute":
        return MuteSpeechBackend()
    if mode == "aivis":
        if aivis_config is None:
            raise ValueError("AivisSpeech configuration is required")
        from .aivis_speech import AivisSpeechBackend

        return AivisSpeechBackend(aivis_config, debug=debug)
    if mode != "auto":
        raise ValueError(f"Unknown speech mode: {mode}")
    fallback: SpeechBackend
    try:
        fallback = WindowsTtsSpeechBackend()
    except RuntimeError:
        fallback = ConsoleSpeechBackend()
    if aivis_config is not None:
        from .aivis_speech import AivisSpeechBackend, AivisSpeechError

        try:
            return AivisSpeechBackend(
                aivis_config, fallback=fallback, debug=debug
            )
        except (AivisSpeechError, RuntimeError) as exc:
            LOGGER.info("AivisSpeech unavailable (%s); using fallback speech", exc)
    return fallback
