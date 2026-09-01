from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
from pathlib import Path
import sys
from typing import Any, Protocol
from urllib import error, parse, request

from g1_bottle_reaction.config.loader import AivisSpeechConfig, VoiceProfileConfig

from .speech import SpeechBackend

LOGGER = logging.getLogger(__name__)


class AivisSpeechError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AivisStyle:
    name: str
    id: int
    type: str | None = None


@dataclass(frozen=True, slots=True)
class AivisSpeaker:
    name: str
    speaker_uuid: str
    styles: tuple[AivisStyle, ...]


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> bytes: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> bytes:
        call = request.Request(
            url, data=data, headers=headers or {}, method=method
        )
        try:
            with request.urlopen(call, timeout=timeout) as response:
                return response.read()
        except (error.HTTPError, error.URLError, TimeoutError, OSError) as exc:
            raise AivisSpeechError(f"AivisSpeech HTTP request failed: {exc}") from exc


class AivisSpeechClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float,
        transport: HttpTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport or UrllibTransport()

    def speakers(self) -> tuple[AivisSpeaker, ...]:
        raw = self.transport.request(
            "GET", f"{self.base_url}/speakers", timeout=self.timeout_seconds
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
            return parse_speakers(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise AivisSpeechError(f"Invalid /speakers response: {exc}") from exc

    def audio_query(self, text: str, style_id: int) -> dict[str, Any]:
        query = parse.urlencode({"text": text, "speaker": style_id})
        raw = self.transport.request(
            "POST",
            f"{self.base_url}/audio_query?{query}",
            data=b"",
            timeout=self.timeout_seconds,
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AivisSpeechError(f"Invalid /audio_query response: {exc}") from exc
        if not isinstance(payload, dict):
            raise AivisSpeechError("Invalid /audio_query response: expected JSON object")
        return payload

    def synthesis(self, query: dict[str, Any], style_id: int) -> bytes:
        params = parse.urlencode({"speaker": style_id})
        raw = self.transport.request(
            "POST",
            f"{self.base_url}/synthesis?{params}",
            data=json.dumps(query, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout_seconds,
        )
        validate_wav(raw)
        return raw


class AudioOutput(ABC):
    @abstractmethod
    def play_wav(self, path: Path) -> None:
        """Play an already synthesized WAV at the selected destination."""


class WindowsWaveOutput(AudioOutput):
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("AivisSpeech WAV playback currently requires Windows")

    def play_wav(self, path: Path) -> None:
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME)


class SpeechCache:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def path_for(
        self,
        text: str,
        speaker_name: str,
        speaker_uuid: str,
        style_id: int,
        profile: VoiceProfileConfig,
    ) -> Path:
        payload = {
            "schema": 2,
            "text": text,
            "speaker_name": speaker_name,
            "speaker_uuid": speaker_uuid,
            "style_id": style_id,
            "profile": asdict(profile),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self.directory / f"{hashlib.sha256(encoded).hexdigest()}.wav"

    def store(self, path: Path, wav: bytes) -> None:
        validate_wav(wav)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".wav.tmp")
        temporary.write_bytes(wav)
        temporary.replace(path)


class AivisSpeechBackend(SpeechBackend):
    def __init__(
        self,
        config: AivisSpeechConfig,
        *,
        client: AivisSpeechClient | None = None,
        output: AudioOutput | None = None,
        fallback: SpeechBackend | None = None,
        debug: bool = False,
    ) -> None:
        self.config = config
        self.client = client or AivisSpeechClient(
            config.base_url, timeout_seconds=config.timeout_seconds
        )
        self.output = output or WindowsWaveOutput()
        self.fallback = fallback
        self.debug = debug or config.debug
        self.cache = SpeechCache(config.cache_dir)
        try:
            self.speakers = self.client.speakers()
        except AivisSpeechError as exc:
            raise AivisSpeechError(
                f"AivisSpeech Engine is not reachable at {config.base_url}. "
                "Please start AivisSpeech and try again."
            ) from exc
        self.default_speaker, self.default_style = resolve_default_style(
            self.speakers, config
        )

    def speak(self, text: str, *, voice_profile: str = "neutral") -> None:
        try:
            path, cache_hit, style, profile_name, profile = self._prepare(
                text, voice_profile
            )
            self._log_debug(cache_hit, style, profile_name, profile)
            self.output.play_wav(path)
        except (AivisSpeechError, OSError, RuntimeError) as exc:
            if self.fallback is None:
                raise
            LOGGER.warning("AivisSpeech failed (%s); using fallback speech", exc)
            self.fallback.speak(text, voice_profile=voice_profile)

    def precache(
        self, text: str, *, voice_profile: str = "neutral"
    ) -> tuple[Path, bool]:
        path, cache_hit, style, profile_name, profile = self._prepare(
            text, voice_profile
        )
        self._log_debug(cache_hit, style, profile_name, profile)
        return path, cache_hit

    def _prepare(
        self, text: str, voice_profile: str
    ) -> tuple[Path, bool, AivisStyle, str, VoiceProfileConfig]:
        profile_name, profile = resolve_profile(
            voice_profile, self.config.voice_profiles
        )
        style = resolve_profile_style(
            self.default_speaker,
            self.default_style,
            profile.style_name,
            warn=LOGGER.warning,
        )
        path = self.cache.path_for(
            text,
            self.default_speaker.name,
            self.default_speaker.speaker_uuid,
            style.id,
            profile,
        )
        if path.is_file():
            return path, True, style, profile_name, profile
        query = self.client.audio_query(text, style.id)
        apply_voice_profile(query, profile)
        wav = self.client.synthesis(query, style.id)
        self.cache.store(path, wav)
        return path, False, style, profile_name, profile

    def _log_debug(
        self,
        cache_hit: bool,
        style: AivisStyle,
        profile_name: str,
        profile: VoiceProfileConfig,
    ) -> None:
        if not self.debug:
            return
        LOGGER.info(
            "[SPEECH] backend=aivis profile=%s style=%s intonation=%.2f "
            "tempo_dynamics=%.2f cache=%s",
            profile_name,
            style.name,
            profile.intonation_scale,
            profile.tempo_dynamics_scale,
            "hit" if cache_hit else "miss",
        )


def parse_speakers(payload: Any) -> tuple[AivisSpeaker, ...]:
    if not isinstance(payload, list):
        raise ValueError("expected a list")
    speakers: list[AivisSpeaker] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("styles"), list):
            raise ValueError("speaker entry is missing styles")
        styles = tuple(
            AivisStyle(
                name=str(style["name"]),
                id=int(style["id"]),
                type=str(style["type"]) if style.get("type") is not None else None,
            )
            for style in item["styles"]
        )
        speakers.append(
            AivisSpeaker(
                name=str(item["name"]),
                speaker_uuid=str(item["speaker_uuid"]),
                styles=styles,
            )
        )
    return tuple(speakers)


def resolve_default_style(
    speakers: tuple[AivisSpeaker, ...], config: AivisSpeechConfig
) -> tuple[AivisSpeaker, AivisStyle]:
    if not speakers:
        raise AivisSpeechError("AivisSpeech Engine has no installed speakers")
    if config.style_id is not None:
        for speaker in speakers:
            for style in speaker.styles:
                if style.id == config.style_id:
                    return speaker, style
        raise AivisSpeechError(
            f"Configured Aivis style ID {config.style_id} was not found.\n"
            + format_speakers(speakers)
        )
    speaker = speakers[0]
    if config.speaker_name is not None:
        match = next(
            (
                item
                for item in speakers
                if item.name.casefold() == config.speaker_name.casefold()
            ),
            None,
        )
        if match is None:
            raise AivisSpeechError(
                f"Configured Aivis speaker '{config.speaker_name}' was not found.\n"
                + format_speakers(speakers)
            )
        speaker = match
    if not speaker.styles:
        raise AivisSpeechError(f"Aivis speaker '{speaker.name}' has no styles")
    if config.style_name is not None:
        preferred_names = (config.style_name, *config.style_fallback_names)
        for preferred_name in preferred_names:
            style = _find_style(speaker, preferred_name)
            if style is not None:
                if preferred_name != config.style_name:
                    LOGGER.warning(
                        "Configured Aivis style '%s' is unavailable; using '%s'.",
                        config.style_name,
                        style.name,
                    )
                return speaker, style
        if config.style_fallback_names:
            style = speaker.styles[0]
            LOGGER.warning(
                "Configured Aivis styles %s are unavailable; using first style '%s'.",
                ", ".join(preferred_names),
                style.name,
            )
            return speaker, style
        else:
            raise AivisSpeechError(
                f"Configured Aivis style '{config.style_name}' was not found for "
                f"speaker '{speaker.name}'.\n{format_speakers(speakers)}"
            )
    return speaker, _normal_style(speaker) or speaker.styles[0]


def resolve_profile(
    requested: str, profiles: dict[str, VoiceProfileConfig]
) -> tuple[str, VoiceProfileConfig]:
    profile = profiles.get(requested)
    if profile is not None:
        return requested, profile
    LOGGER.warning("Voice profile '%s' is not configured; using neutral", requested)
    return "neutral", profiles["neutral"]


def resolve_profile_style(
    speaker: AivisSpeaker,
    default_style: AivisStyle,
    requested_name: str | None,
    *,
    warn,
) -> AivisStyle:
    if requested_name is None:
        return default_style
    style = _find_style(speaker, requested_name)
    if style is not None:
        return style
    fallback = default_style or _normal_style(speaker) or speaker.styles[0]
    warn(
        f"Aivis style '{requested_name}' is not available for speaker "
        f"'{speaker.name}'. Using '{fallback.name}'."
    )
    return fallback


def apply_voice_profile(
    query: dict[str, Any], profile: VoiceProfileConfig
) -> None:
    query["intonationScale"] = profile.intonation_scale
    query["tempoDynamicsScale"] = profile.tempo_dynamics_scale
    query["speedScale"] = profile.speed_scale
    query["volumeScale"] = profile.volume_scale
    query["pitchScale"] = profile.pitch_scale


def format_speakers(speakers: tuple[AivisSpeaker, ...]) -> str:
    lines: list[str] = []
    for speaker in speakers:
        lines.append(f"Speaker: {speaker.name}")
        lines.append(f"UUID: {speaker.speaker_uuid}")
        for style in speaker.styles:
            lines.append(f"  Style: {style.name}")
            lines.append(f"  ID: {style.id}")
    return "\n".join(lines)


def validate_wav(data: bytes) -> None:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AivisSpeechError("AivisSpeech synthesis did not return a WAV file")


def _find_style(speaker: AivisSpeaker, name: str) -> AivisStyle | None:
    return next(
        (style for style in speaker.styles if style.name.casefold() == name.casefold()),
        None,
    )


def _normal_style(speaker: AivisSpeaker) -> AivisStyle | None:
    return next(
        (
            style
            for style in speaker.styles
            if style.name.casefold() in {"ノーマル", "normal"}
        ),
        None,
    )
