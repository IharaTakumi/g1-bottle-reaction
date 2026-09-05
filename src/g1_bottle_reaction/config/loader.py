from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from g1_bottle_reaction.reactions.models import EncounterVariant, Reaction


@dataclass(frozen=True, slots=True)
class VisionConfig:
    model: str
    confidence_threshold: float


@dataclass(frozen=True, slots=True)
class TrackingConfig:
    near_enter_ratio: float
    near_exit_ratio: float
    too_close_enter_ratio: float
    too_close_exit_ratio: float
    detection_confirm_seconds: float
    lost_confirm_seconds: float
    found_again_window_seconds: float

    def validate(self) -> None:
        ratios = (
            self.near_exit_ratio,
            self.near_enter_ratio,
            self.too_close_exit_ratio,
            self.too_close_enter_ratio,
        )
        if not 0 <= ratios[0] <= ratios[1] <= ratios[2] <= ratios[3] <= 1:
            raise ValueError(
                "tracking ratios must satisfy 0 <= near_exit <= near_enter "
                "<= too_close_exit <= too_close_enter <= 1"
            )
        if min(
            self.detection_confirm_seconds,
            self.lost_confirm_seconds,
            self.found_again_window_seconds,
        ) < 0:
            raise ValueError("tracking durations cannot be negative")


@dataclass(frozen=True, slots=True)
class ReactionConfig:
    cooldown_seconds: float
    items: dict[str, Reaction]
    timeline_debug: bool = False


@dataclass(frozen=True, slots=True)
class StealthTargetConfig:
    detector_label: str
    semantic_role: str
    confidence_threshold: float
    detection_confirm_seconds: float
    lost_grace_seconds: float


@dataclass(frozen=True, slots=True)
class VisibilityScoreConfig:
    confidence_weight: float
    area_weight: float
    center_weight: float
    area_reference_ratio: float

    def validate(self) -> None:
        if min(self.confidence_weight, self.area_weight, self.center_weight) < 0:
            raise ValueError("visibility score weights cannot be negative")
        if self.confidence_weight + self.area_weight + self.center_weight <= 0:
            raise ValueError("at least one visibility score weight must be positive")
        if not 0 < self.area_reference_ratio <= 1:
            raise ValueError("area_reference_ratio must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class SuspicionConfig:
    suspicious_threshold: float
    alert_threshold: float
    found_threshold: float
    gain_per_second: float
    decay_per_second: float

    def validate(self) -> None:
        if not 0 <= self.suspicious_threshold < self.alert_threshold < self.found_threshold <= 100:
            raise ValueError(
                "suspicion thresholds must satisfy 0 <= suspicious < alert < found <= 100"
            )
        if min(self.gain_per_second, self.decay_per_second) < 0:
            raise ValueError("suspicion gain and decay cannot be negative")


@dataclass(frozen=True, slots=True)
class StealthFoundConfig:
    game_over_delay_seconds: float


@dataclass(frozen=True, slots=True)
class StealthRoundConfig:
    auto_reset: bool
    auto_reset_seconds: float


@dataclass(frozen=True, slots=True)
class StealthTrackingConfig:
    enabled: bool
    deadzone: float
    max_preview_yaw_degrees: float
    invert_x: bool
    lost_hold_seconds: float
    suspicious_response_speed: float
    alert_response_speed: float
    found_response_speed: float
    recenter_response_speed: float


@dataclass(frozen=True, slots=True)
class StealthGameConfig:
    enabled: bool
    target: StealthTargetConfig
    visibility_score: VisibilityScoreConfig
    suspicion: SuspicionConfig
    found: StealthFoundConfig
    round: StealthRoundConfig
    tracking: StealthTrackingConfig

    def validate(self) -> None:
        if not 0 <= self.target.confidence_threshold <= 1:
            raise ValueError("stealth target confidence_threshold must be between 0 and 1")
        if min(
            self.target.detection_confirm_seconds,
            self.target.lost_grace_seconds,
            self.found.game_over_delay_seconds,
            self.round.auto_reset_seconds,
        ) < 0:
            raise ValueError("stealth game durations cannot be negative")
        if not 0 <= self.tracking.deadzone < 1:
            raise ValueError("stealth tracking deadzone must be in [0, 1)")
        if self.tracking.max_preview_yaw_degrees < 0:
            raise ValueError("max_preview_yaw_degrees cannot be negative")
        if min(
            self.tracking.suspicious_response_speed,
            self.tracking.alert_response_speed,
            self.tracking.found_response_speed,
            self.tracking.recenter_response_speed,
            self.tracking.lost_hold_seconds,
        ) < 0:
            raise ValueError("stealth tracking speeds and lost hold cannot be negative")
        self.visibility_score.validate()
        self.suspicion.validate()


@dataclass(frozen=True, slots=True)
class YamnetConfig:
    model_url: str
    cache_dir: Path
    window_seconds: float
    inference_interval_seconds: float
    music_labels: tuple[str, ...]
    top_n: int


@dataclass(frozen=True, slots=True)
class MusicTrackingConfig:
    music_start_threshold: float
    music_stop_threshold: float
    music_confirm_seconds: float
    music_lost_seconds: float
    music_cooldown_seconds: float

    def validate(self) -> None:
        if not 0 <= self.music_stop_threshold <= self.music_start_threshold <= 1:
            raise ValueError(
                "music thresholds must satisfy 0 <= stop <= start <= 1"
            )
        if min(
            self.music_confirm_seconds,
            self.music_lost_seconds,
            self.music_cooldown_seconds,
        ) < 0:
            raise ValueError("music tracking durations cannot be negative")


@dataclass(frozen=True, slots=True)
class AudioConfig:
    enabled: bool
    source: str
    mode: str
    target_sample_rate: int
    queue_max_chunks: int
    debug_record_seconds: float
    yamnet: YamnetConfig
    music_tracking: MusicTrackingConfig


@dataclass(frozen=True, slots=True)
class VoiceProfileConfig:
    style_name: str | None
    intonation_scale: float
    tempo_dynamics_scale: float
    speed_scale: float
    volume_scale: float
    pitch_scale: float = 0.0
    pre_phoneme_length: float | None = None

    def validate(self, name: str) -> None:
        ranges = {
            "intonation_scale": (self.intonation_scale, 0.0, 2.0),
            "tempo_dynamics_scale": (self.tempo_dynamics_scale, 0.0, 2.0),
            "speed_scale": (self.speed_scale, 0.5, 2.0),
            "volume_scale": (self.volume_scale, 0.0, 2.0),
            "pitch_scale": (self.pitch_scale, -0.15, 0.15),
        }
        for field_name, (value, minimum, maximum) in ranges.items():
            if not minimum <= value <= maximum:
                raise ValueError(
                    f"voice profile '{name}' {field_name} must be between "
                    f"{minimum} and {maximum}"
                )
        if self.pre_phoneme_length is not None and not (
            0.0 <= self.pre_phoneme_length <= 1.0
        ):
            raise ValueError(
                f"voice profile '{name}' pre_phoneme_length must be between 0 and 1"
            )


@dataclass(frozen=True, slots=True)
class AivisSpeechConfig:
    base_url: str
    speaker_name: str | None
    style_name: str | None
    style_fallback_names: tuple[str, ...]
    style_id: int | None
    timeout_seconds: float
    cache_dir: Path
    debug: bool
    voice_profiles: dict[str, VoiceProfileConfig]


@dataclass(frozen=True, slots=True)
class SpeechConfig:
    aivis: AivisSpeechConfig


@dataclass(frozen=True, slots=True)
class G1Config:
    client_timeout_seconds: float
    arm_release_delay_seconds: float
    speaker_volume: int
    audio_chunk_bytes: int
    audio_chunk_delay_seconds: float
    camera_frame_timeout_seconds: float
    camera_reconnect_attempts: int
    camera_reconnect_delay_seconds: float

    def validate(self) -> None:
        if self.client_timeout_seconds <= 0:
            raise ValueError("g1 client_timeout_seconds must be positive")
        if self.arm_release_delay_seconds < 0:
            raise ValueError("g1 arm_release_delay_seconds cannot be negative")
        if not 0 <= self.speaker_volume <= 100:
            raise ValueError("g1 speaker_volume must be between 0 and 100")
        if self.audio_chunk_bytes <= 0 or self.audio_chunk_delay_seconds < 0:
            raise ValueError("g1 audio chunk settings are invalid")
        if self.camera_frame_timeout_seconds <= 0:
            raise ValueError("g1 camera_frame_timeout_seconds must be positive")
        if self.camera_reconnect_attempts < 1:
            raise ValueError("g1 camera_reconnect_attempts must be at least 1")
        if self.camera_reconnect_delay_seconds < 0:
            raise ValueError("g1 camera_reconnect_delay_seconds cannot be negative")


@dataclass(frozen=True, slots=True)
class NavigationConfig:
    default_route_id: str
    command_timeout_s: float
    status_poll_interval_s: float
    heartbeat_interval_s: float
    heartbeat_timeout_s: float
    reaction_completion_timeout_s: float
    auto_pause_for_reaction: bool

    def validate(self) -> None:
        if not self.default_route_id.strip():
            raise ValueError("navigation default_route_id cannot be empty")
        if min(
            self.command_timeout_s,
            self.status_poll_interval_s,
            self.heartbeat_interval_s,
            self.heartbeat_timeout_s,
            self.reaction_completion_timeout_s,
        ) <= 0:
            raise ValueError("navigation timeouts and intervals must be positive")
        if self.heartbeat_timeout_s <= self.heartbeat_interval_s:
            raise ValueError(
                "navigation heartbeat_timeout_s must exceed heartbeat_interval_s"
            )


@dataclass(frozen=True, slots=True)
class AppConfig:
    vision: VisionConfig
    tracking: TrackingConfig
    reaction: ReactionConfig
    stealth_game: StealthGameConfig
    audio: AudioConfig
    speech: SpeechConfig
    g1: G1Config
    navigation: NavigationConfig
    event_log: Path
    simulation_realtime_scale: float


def default_config_path() -> Path:
    repository_config = Path(__file__).resolve().parents[3] / "config" / "default.yaml"
    if repository_config.is_file():
        return repository_config
    return Path(__file__).with_name("default.yaml")


def _reaction_from_mapping(name: str, value: dict[str, Any]) -> Reaction:
    variants = tuple(
        EncounterVariant(
            min_encounter_count=int(item["min_encounter_count"]),
            speech=str(item["speech"]),
        )
        for item in value.get("encounter_variants", [])
    )
    return Reaction(
        name=name,
        motion=str(value["motion"]),
        speech=str(value["speech"]),
        speech_delay_seconds=float(value["speech_delay_seconds"]),
        voice_profile=str(value.get("voice_profile", "neutral")),
        priority=int(value.get("priority", 0)),
        bypass_cooldown=bool(value.get("bypass_cooldown", False)),
        encounter_variants=variants,
    )


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path is not None else default_config_path()
    with config_path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)

    tracking = TrackingConfig(**raw["tracking"])
    tracking.validate()
    reaction_raw = raw["reaction"]
    reactions = {
        name: _reaction_from_mapping(name, item)
        for name, item in reaction_raw["items"].items()
    }
    stealth_raw = raw["stealth_game"]
    stealth_game = StealthGameConfig(
        enabled=bool(stealth_raw["enabled"]),
        target=StealthTargetConfig(**stealth_raw["target"]),
        visibility_score=VisibilityScoreConfig(**stealth_raw["visibility_score"]),
        suspicion=SuspicionConfig(**stealth_raw["suspicion"]),
        found=StealthFoundConfig(**stealth_raw["found"]),
        round=StealthRoundConfig(**stealth_raw["round"]),
        tracking=StealthTrackingConfig(**stealth_raw["tracking"]),
    )
    stealth_game.validate()
    audio_raw = raw["audio"]
    yamnet_raw = audio_raw["yamnet"]
    music_tracking = MusicTrackingConfig(**audio_raw["music_tracking"])
    music_tracking.validate()
    if int(audio_raw["target_sample_rate"]) != 16000:
        raise ValueError("YAMNet target_sample_rate must be 16000")
    if float(audio_raw["debug_record_seconds"]) <= 0:
        raise ValueError("audio debug_record_seconds must be positive")
    if not yamnet_raw["music_labels"]:
        raise ValueError("audio yamnet.music_labels cannot be empty")
    if str(audio_raw["mode"]) not in {"normal", "raw", "auto"}:
        raise ValueError("audio mode must be normal, raw, or auto")
    aivis_raw = raw["speech"]["aivis"]
    profiles = {
        str(name): VoiceProfileConfig(
            style_name=(
                str(item["style_name"])
                if item.get("style_name") is not None
                else None
            ),
            intonation_scale=float(item.get("intonation_scale", 1.0)),
            tempo_dynamics_scale=float(item.get("tempo_dynamics_scale", 1.0)),
            speed_scale=float(item.get("speed_scale", 1.0)),
            volume_scale=float(item.get("volume_scale", 1.0)),
            pitch_scale=float(item.get("pitch_scale", 0.0)),
            pre_phoneme_length=(
                float(item["pre_phoneme_length"])
                if item.get("pre_phoneme_length") is not None
                else None
            ),
        )
        for name, item in aivis_raw["voice_profiles"].items()
    }
    if "neutral" not in profiles:
        raise ValueError("speech.aivis.voice_profiles must define neutral")
    for name, profile in profiles.items():
        profile.validate(name)
    if float(aivis_raw.get("timeout_seconds", 30.0)) <= 0:
        raise ValueError("speech.aivis.timeout_seconds must be positive")
    g1 = G1Config(**raw["g1"])
    g1.validate()
    navigation = NavigationConfig(**raw["navigation"])
    navigation.validate()
    return AppConfig(
        vision=VisionConfig(**raw["vision"]),
        tracking=tracking,
        reaction=ReactionConfig(
            cooldown_seconds=float(reaction_raw["cooldown_seconds"]),
            items=reactions,
            timeline_debug=bool(reaction_raw.get("timeline_debug", False)),
        ),
        stealth_game=stealth_game,
        audio=AudioConfig(
            enabled=bool(audio_raw["enabled"]),
            source=str(audio_raw["source"]),
            mode=str(audio_raw["mode"]),
            target_sample_rate=int(audio_raw["target_sample_rate"]),
            queue_max_chunks=int(audio_raw["queue_max_chunks"]),
            debug_record_seconds=float(audio_raw["debug_record_seconds"]),
            yamnet=YamnetConfig(
                model_url=str(yamnet_raw["model_url"]),
                cache_dir=Path(yamnet_raw["cache_dir"]),
                window_seconds=float(yamnet_raw["window_seconds"]),
                inference_interval_seconds=float(
                    yamnet_raw["inference_interval_seconds"]
                ),
                music_labels=tuple(str(item) for item in yamnet_raw["music_labels"]),
                top_n=int(yamnet_raw["top_n"]),
            ),
            music_tracking=music_tracking,
        ),
        speech=SpeechConfig(
            aivis=AivisSpeechConfig(
                base_url=str(aivis_raw["base_url"]).rstrip("/"),
                speaker_name=(
                    str(aivis_raw["speaker_name"])
                    if aivis_raw.get("speaker_name") is not None
                    else None
                ),
                style_name=(
                    str(aivis_raw["style_name"])
                    if aivis_raw.get("style_name") is not None
                    else None
                ),
                style_fallback_names=tuple(
                    str(item)
                    for item in aivis_raw.get("style_fallback_names", [])
                ),
                style_id=(
                    int(aivis_raw["style_id"])
                    if aivis_raw.get("style_id") is not None
                    else None
                ),
                timeout_seconds=float(aivis_raw.get("timeout_seconds", 30.0)),
                cache_dir=Path(aivis_raw.get("cache_dir", ".cache/tts")),
                debug=bool(aivis_raw.get("debug", False)),
                voice_profiles=profiles,
            )
        ),
        g1=g1,
        navigation=navigation,
        event_log=Path(raw["logging"]["event_log"]),
        simulation_realtime_scale=float(raw["simulation"].get("realtime_scale", 0)),
    )
