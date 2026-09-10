from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
import math
from pathlib import Path
from typing import Any, Mapping

import yaml


@dataclass(frozen=True, slots=True)
class DistancePreset:
    clear_m: float
    max_m: float

    def validate(self, name: str = "distance") -> None:
        if not math.isfinite(self.clear_m) or not math.isfinite(self.max_m):
            raise ValueError(f"{name} values must be finite")
        if self.clear_m < 0:
            raise ValueError(f"{name}.clear_m must be non-negative")
        if self.max_m <= self.clear_m:
            raise ValueError(f"{name}.max_m must be greater than clear_m")


@dataclass(frozen=True, slots=True)
class FovConfig:
    enabled: bool
    scale: float
    feather: float

    def validate(self) -> None:
        if not math.isfinite(self.scale) or not math.isfinite(self.feather):
            raise ValueError("game_vision.fov values must be finite")
        if not 0 < self.scale <= 1:
            raise ValueError("game_vision.fov.scale must be in (0, 1]")
        if not 0 < self.feather <= 1:
            raise ValueError("game_vision.fov.feather must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class DisplayConfig:
    fullscreen: bool
    show_fps: bool
    mode: str
    safety_inset_scale: float

    def validate(self) -> None:
        if self.mode not in {"game", "safety", "both"}:
            raise ValueError("game_vision.display.mode must be game, safety, or both")
        if not math.isfinite(self.safety_inset_scale):
            raise ValueError("game_vision.display.safety_inset_scale must be finite")
        if not 0.1 <= self.safety_inset_scale <= 0.5:
            raise ValueError(
                "game_vision.display.safety_inset_scale must be between 0.1 and 0.5"
            )


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    width: int
    height: int
    fps: int
    timeout_ms: int

    def validate(self) -> None:
        if min(self.width, self.height, self.fps, self.timeout_ms) <= 0:
            raise ValueError("game_vision.capture values must all be positive")


@dataclass(frozen=True, slots=True)
class ControlsConfig:
    distance_step_m: float

    def validate(self) -> None:
        if not math.isfinite(self.distance_step_m):
            raise ValueError("game_vision.controls.distance_step_m must be finite")
        if self.distance_step_m <= 0:
            raise ValueError("game_vision.controls.distance_step_m must be positive")


@dataclass(frozen=True, slots=True)
class TransportConfig:
    g1_host: str
    game_port: int
    safety_port: int
    bind_host: str
    webp_quality: int
    connect_timeout_ms: int
    stale_timeout_ms: int

    def validate(self) -> None:
        if not self.g1_host or not self.bind_host:
            raise ValueError("game_vision.transport hosts must be non-empty")
        for name, port in (("game_port", self.game_port), ("safety_port", self.safety_port)):
            if not 1 <= port <= 65535:
                raise ValueError(f"game_vision.transport.{name} must be in [1, 65535]")
        if self.game_port == self.safety_port:
            raise ValueError("game and safety transport ports must differ")
        if not 1 <= self.webp_quality <= 100:
            raise ValueError("game_vision.transport.webp_quality must be in [1, 100]")
        if min(self.connect_timeout_ms, self.stale_timeout_ms) <= 0:
            raise ValueError("game_vision.transport timeouts must be positive")


@dataclass(frozen=True, slots=True)
class GameVisionConfig:
    vision_preset: str
    fog_mode: str
    presets: Mapping[str, DistancePreset]
    fov: FovConfig
    display: DisplayConfig
    capture: CaptureConfig
    transport: TransportConfig
    controls: ControlsConfig

    def validate(self) -> None:
        if self.fog_mode not in {"fade", "hard"}:
            raise ValueError("game_vision.fog_mode must be fade or hard")
        if self.vision_preset not in self.presets:
            raise ValueError(
                f"unknown vision preset {self.vision_preset!r}; "
                f"choose from {', '.join(sorted(self.presets))}"
            )
        required = {"close", "normal", "long"}
        missing = required.difference(self.presets)
        if missing:
            raise ValueError(
                "game_vision.presets is missing: " + ", ".join(sorted(missing))
            )
        for name, preset in self.presets.items():
            preset.validate(f"game_vision.presets.{name}")
        self.fov.validate()
        self.display.validate()
        self.capture.validate()
        self.transport.validate()
        self.controls.validate()

    @property
    def selected_distance(self) -> DistancePreset:
        return self.presets[self.vision_preset]


def load_game_vision_config(path: str | Path | None = None) -> GameVisionConfig:
    """Load the bundled defaults and optionally merge a small override YAML."""

    default_text = files(__package__).joinpath("default.yaml").read_text(encoding="utf-8")
    raw = yaml.safe_load(default_text)
    if path is not None:
        override_path = Path(path).expanduser()
        try:
            override = yaml.safe_load(override_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"could not read game vision config {override_path}: {exc}") from exc
        if override is None:
            override = {}
        if not isinstance(override, dict):
            raise ValueError("game vision config root must be a mapping")
        raw = _deep_merge(raw, override)
    return _parse_config(raw)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _parse_config(raw: Any) -> GameVisionConfig:
    if not isinstance(raw, dict) or not isinstance(raw.get("game_vision"), dict):
        raise ValueError("config must contain a game_vision mapping")
    _check_keys(raw, {"game_vision"}, "config root")
    root = raw["game_vision"]
    _check_keys(
        root,
        {
            "vision_preset",
            "fog_mode",
            "presets",
            "fov",
            "display",
            "capture",
            "transport",
            "controls",
        },
        "game_vision",
    )
    presets_raw = _mapping(root, "presets", "game_vision")
    presets: dict[str, DistancePreset] = {}
    for name, value in presets_raw.items():
        if not isinstance(value, dict):
            raise ValueError(f"game_vision.presets.{name} must be a mapping")
        _check_keys(value, {"clear_m", "max_m"}, f"game_vision.presets.{name}")
        presets[str(name)] = DistancePreset(
            clear_m=_float(value, "clear_m", f"game_vision.presets.{name}"),
            max_m=_float(value, "max_m", f"game_vision.presets.{name}"),
        )

    fov_raw = _mapping(root, "fov", "game_vision")
    _check_keys(fov_raw, {"enabled", "scale", "feather"}, "game_vision.fov")
    display_raw = _mapping(root, "display", "game_vision")
    _check_keys(
        display_raw,
        {"fullscreen", "show_fps", "mode", "safety_inset_scale"},
        "game_vision.display",
    )
    capture_raw = _mapping(root, "capture", "game_vision")
    _check_keys(capture_raw, {"width", "height", "fps", "timeout_ms"}, "game_vision.capture")
    transport_raw = _mapping(root, "transport", "game_vision")
    _check_keys(
        transport_raw,
        {
            "g1_host",
            "game_port",
            "safety_port",
            "bind_host",
            "webp_quality",
            "connect_timeout_ms",
            "stale_timeout_ms",
        },
        "game_vision.transport",
    )
    controls_raw = _mapping(root, "controls", "game_vision")
    _check_keys(controls_raw, {"distance_step_m"}, "game_vision.controls")

    config = GameVisionConfig(
        vision_preset=_string(root, "vision_preset", "game_vision"),
        fog_mode=_string(root, "fog_mode", "game_vision"),
        presets=presets,
        fov=FovConfig(
            enabled=_bool(fov_raw, "enabled", "game_vision.fov"),
            scale=_float(fov_raw, "scale", "game_vision.fov"),
            feather=_float(fov_raw, "feather", "game_vision.fov"),
        ),
        display=DisplayConfig(
            fullscreen=_bool(display_raw, "fullscreen", "game_vision.display"),
            show_fps=_bool(display_raw, "show_fps", "game_vision.display"),
            mode=_string(display_raw, "mode", "game_vision.display"),
            safety_inset_scale=_float(
                display_raw, "safety_inset_scale", "game_vision.display"
            ),
        ),
        capture=CaptureConfig(
            width=_int(capture_raw, "width", "game_vision.capture"),
            height=_int(capture_raw, "height", "game_vision.capture"),
            fps=_int(capture_raw, "fps", "game_vision.capture"),
            timeout_ms=_int(capture_raw, "timeout_ms", "game_vision.capture"),
        ),
        transport=TransportConfig(
            g1_host=_string(transport_raw, "g1_host", "game_vision.transport"),
            game_port=_int(transport_raw, "game_port", "game_vision.transport"),
            safety_port=_int(transport_raw, "safety_port", "game_vision.transport"),
            bind_host=_string(transport_raw, "bind_host", "game_vision.transport"),
            webp_quality=_int(
                transport_raw, "webp_quality", "game_vision.transport"
            ),
            connect_timeout_ms=_int(
                transport_raw, "connect_timeout_ms", "game_vision.transport"
            ),
            stale_timeout_ms=_int(
                transport_raw, "stale_timeout_ms", "game_vision.transport"
            ),
        ),
        controls=ControlsConfig(
            distance_step_m=_float(
                controls_raw, "distance_step_m", "game_vision.controls"
            )
        ),
    )
    config.validate()
    return config


def _mapping(data: Mapping[str, Any], key: str, parent: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{parent}.{key} must be a mapping")
    return value


def _string(data: Mapping[str, Any], key: str, parent: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{parent}.{key} must be a non-empty string")
    return value


def _bool(data: Mapping[str, Any], key: str, parent: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{parent}.{key} must be true or false")
    return value


def _float(data: Mapping[str, Any], key: str, parent: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{parent}.{key} must be a number")
    return float(value)


def _int(data: Mapping[str, Any], key: str, parent: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{parent}.{key} must be an integer")
    return value


def _check_keys(data: Mapping[str, Any], allowed: set[str], parent: str) -> None:
    unknown = set(data).difference(allowed)
    if unknown:
        raise ValueError(f"unknown {parent} key(s): {', '.join(sorted(unknown))}")
