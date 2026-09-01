from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from g1_bottle_reaction.adapters import aivis_speech
from g1_bottle_reaction.adapters.aivis_speech import (
    AivisSpeaker,
    AivisSpeechBackend,
    AivisSpeechClient,
    AivisSpeechError,
    AivisStyle,
    AudioOutput,
    SpeechCache,
    apply_voice_profile,
    parse_speakers,
    resolve_default_style,
    resolve_profile_style,
)
from g1_bottle_reaction.adapters.speech import (
    ConsoleSpeechBackend,
    create_speech_backend,
)


WAV = b"RIFF" + (4).to_bytes(4, "little") + b"WAVE"
SPEAKER_PAYLOAD = [
    {
        "name": "Anneli",
        "speaker_uuid": "speaker-1",
        "styles": [
            {"name": "ノーマル", "id": 100, "type": "talk"},
            {"name": "上機嫌", "id": 101, "type": "talk"},
        ],
    },
    {
        "name": "Second",
        "speaker_uuid": "speaker-2",
        "styles": [{"name": "ノーマル", "id": 200}],
    },
]


class FakeTransport:
    def __init__(self, *, unavailable: bool = False, wav: bytes = WAV) -> None:
        self.unavailable = unavailable
        self.wav = wav
        self.calls: list[tuple[str, str, bytes | None]] = []

    def request(self, method, url, *, data=None, headers=None, timeout):
        del headers, timeout
        self.calls.append((method, url, data))
        if self.unavailable:
            raise AivisSpeechError("connection refused")
        path = urlparse(url).path
        if path == "/speakers":
            return json.dumps(SPEAKER_PAYLOAD).encode()
        if path == "/audio_query":
            return json.dumps(
                {
                    "speedScale": 1.0,
                    "intonationScale": 1.0,
                    "tempoDynamicsScale": 1.0,
                    "pitchScale": 0.0,
                    "volumeScale": 1.0,
                }
            ).encode()
        if path == "/synthesis":
            return self.wav
        raise AssertionError(path)


class RecordingOutput(AudioOutput):
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def play_wav(self, path: Path) -> None:
        self.paths.append(path)


def _config(app_config, tmp_path: Path):
    return replace(
        app_config.speech.aivis,
        cache_dir=tmp_path / "tts",
        speaker_name="Anneli",
        style_name="ノーマル",
    )


def test_speakers_parsing() -> None:
    speakers = parse_speakers(SPEAKER_PAYLOAD)
    assert speakers[0].name == "Anneli"
    assert speakers[0].speaker_uuid == "speaker-1"
    assert speakers[0].styles[1] == AivisStyle("上機嫌", 101, "talk")


def test_speaker_style_resolution_and_style_id_priority(app_config, tmp_path) -> None:
    speakers = parse_speakers(SPEAKER_PAYLOAD)
    config = _config(app_config, tmp_path)
    speaker, style = resolve_default_style(speakers, config)
    assert (speaker.name, style.id) == ("Anneli", 100)
    speaker, style = resolve_default_style(
        speakers, replace(config, speaker_name="missing", style_id=200)
    )
    assert (speaker.name, style.id) == ("Second", 200)


def test_default_speaker_and_calm_style(app_config) -> None:
    config = app_config.speech.aivis
    assert config.speaker_name == "阿井田 茂"
    assert config.style_name == "Calm"
    assert config.style_id is None
    assert config.style_fallback_names == ("Mid",)
    assert {
        name: profile.style_name
        for name, profile in config.voice_profiles.items()
    } == {
        "neutral": "Calm",
        "happy": "Mid",
        "surprised": "Surprise",
        "curious": "Calm",
        "serious": "Heavy",
        "shout": "Shout",
    }


def test_default_style_falls_back_calm_then_mid_then_first(
    app_config,
) -> None:
    speaker = AivisSpeaker(
        "阿井田 茂",
        "speaker-shigeru",
        (AivisStyle("First", 1), AivisStyle("Mid", 2)),
    )
    config = replace(
        app_config.speech.aivis,
        speaker_name="阿井田 茂",
        style_name="Calm",
        style_fallback_names=("Mid",),
    )
    _, style = resolve_default_style((speaker,), config)
    assert style.name == "Mid"
    _, style = resolve_default_style(
        (replace(speaker, styles=(AivisStyle("First", 1),)),), config
    )
    assert style.name == "First"


def test_profile_style_fallback() -> None:
    speaker = AivisSpeaker(
        "Anneli",
        "uuid",
        (AivisStyle("ノーマル", 100), AivisStyle("上機嫌", 101)),
    )
    warnings: list[str] = []
    selected = resolve_profile_style(
        speaker, speaker.styles[0], "未インストール", warn=warnings.append
    )
    assert selected.id == 100
    assert "Using 'ノーマル'" in warnings[0]


def test_voice_profile_config_and_audio_query_modification(app_config) -> None:
    happy = app_config.speech.aivis.voice_profiles["happy"]
    assert happy.intonation_scale == pytest.approx(1.1)
    assert happy.tempo_dynamics_scale == pytest.approx(1.1)
    assert happy.pitch_scale == 0.0
    query: dict[str, float] = {}
    apply_voice_profile(query, happy)
    assert query == {
        "intonationScale": 1.1,
        "tempoDynamicsScale": 1.1,
        "speedScale": 1.0,
        "volumeScale": 1.0,
        "pitchScale": 0.0,
    }
    curious = app_config.speech.aivis.voice_profiles["curious"]
    curious_query: dict[str, float] = {}
    apply_voice_profile(curious_query, curious)
    assert curious_query["prePhonemeLength"] == pytest.approx(0.08)


def test_custom_notice_speech_is_precached_and_reused(app_config, tmp_path) -> None:
    transport = FakeTransport()
    client = AivisSpeechClient(
        "http://127.0.0.1:10101", timeout_seconds=1, transport=transport
    )
    output = RecordingOutput()
    backend = AivisSpeechBackend(
        _config(app_config, tmp_path), client=client, output=output
    )
    _, first_hit = backend.precache("ん？", voice_profile="curious")
    _, second_hit = backend.precache("ん？", voice_profile="curious")
    assert not first_hit
    assert second_hit
    assert sum("/synthesis?" in url for _, url, _ in transport.calls) == 1
    playback_starts = []
    backend.speak_timed(
        "ん？",
        voice_profile="curious",
        on_playback_start=lambda: playback_starts.append(len(output.paths)),
    )
    assert playback_starts == [0]
    assert len(output.paths) == 1


def test_http_synthesis_and_cached_wav_reuse(app_config, tmp_path) -> None:
    transport = FakeTransport()
    client = AivisSpeechClient(
        "http://127.0.0.1:10101", timeout_seconds=1, transport=transport
    )
    output = RecordingOutput()
    backend = AivisSpeechBackend(
        _config(app_config, tmp_path), client=client, output=output
    )
    first_path, first_hit = backend.precache("お、いいね", voice_profile="happy")
    second_path, second_hit = backend.precache("お、いいね", voice_profile="happy")
    assert first_path == second_path
    assert not first_hit
    assert second_hit
    assert first_path.read_bytes() == WAV
    assert sum("/audio_query?" in url for _, url, _ in transport.calls) == 1
    assert sum("/synthesis?" in url for _, url, _ in transport.calls) == 1
    synthesis_body = next(
        data for _, url, data in transport.calls if "/synthesis?" in url
    )
    assert json.loads(synthesis_body)["tempoDynamicsScale"] == pytest.approx(1.1)
    backend.speak("お、いいね", voice_profile="happy")
    assert output.paths == [first_path]


def test_synthesis_rejects_non_wav() -> None:
    client = AivisSpeechClient(
        "http://127.0.0.1:10101",
        timeout_seconds=1,
        transport=FakeTransport(wav=b'{"detail":"error"}'),
    )
    with pytest.raises(AivisSpeechError, match="WAV"):
        client.synthesis({}, 100)


def test_engine_unavailable_has_clear_message(app_config, tmp_path) -> None:
    client = AivisSpeechClient(
        "http://127.0.0.1:10101",
        timeout_seconds=1,
        transport=FakeTransport(unavailable=True),
    )
    with pytest.raises(AivisSpeechError, match="Please start AivisSpeech"):
        AivisSpeechBackend(
            _config(app_config, tmp_path), client=client, output=RecordingOutput()
        )


def test_auto_falls_back_when_aivis_is_unavailable(
    app_config, monkeypatch
) -> None:
    class UnavailableBackend:
        def __init__(self, *args, **kwargs):
            raise AivisSpeechError("offline")

    monkeypatch.setattr(aivis_speech, "AivisSpeechBackend", UnavailableBackend)
    monkeypatch.setattr(
        "g1_bottle_reaction.adapters.speech.WindowsTtsSpeechBackend",
        lambda: ConsoleSpeechBackend(),
    )
    backend = create_speech_backend(
        "auto", aivis_config=app_config.speech.aivis
    )
    assert isinstance(backend, ConsoleSpeechBackend)


def test_cache_key_is_stable_and_parameter_sensitive(app_config, tmp_path) -> None:
    cache = SpeechCache(tmp_path)
    happy = app_config.speech.aivis.voice_profiles["happy"]
    neutral = app_config.speech.aivis.voice_profiles["neutral"]
    assert cache.path_for("お、いいね", "阿井田 茂", "uuid-1", 101, happy) == cache.path_for(
        "お、いいね", "阿井田 茂", "uuid-1", 101, happy
    )
    assert cache.path_for("お、いいね", "阿井田 茂", "uuid-1", 101, happy) != cache.path_for(
        "お、いいね", "まお", "uuid-2", 101, happy
    )
    assert cache.path_for("お、いいね", "阿井田 茂", "uuid-1", 101, happy) != cache.path_for(
        "お、いいね", "阿井田 茂", "uuid-1", 100, neutral
    )


def test_aivis_module_import_requires_no_running_engine() -> None:
    assert aivis_speech.AivisSpeechBackend is not None
