from __future__ import annotations

import pytest

from g1_bottle_reaction.game_vision.config import load_game_vision_config


def test_default_game_vision_config_loads_requested_values() -> None:
    config = load_game_vision_config()
    assert config.vision_preset == "normal"
    assert config.fog_mode == "fade"
    assert config.selected_distance.clear_m == pytest.approx(1.5)
    assert config.selected_distance.max_m == pytest.approx(2.5)
    assert config.presets["close"].clear_m == pytest.approx(1.0)
    assert config.presets["long"].max_m == pytest.approx(3.5)
    assert config.fov.enabled
    assert config.fov.scale == pytest.approx(0.55)
    assert config.display.fullscreen
    assert config.capture.width == 640
    assert config.capture.height == 480
    assert config.capture.fps == 30
    assert config.transport.g1_host == "192.168.123.164"
    assert config.transport.game_port == 55558
    assert config.transport.safety_port == 55559
    assert config.transport.bind_host == "127.0.0.1"
    assert config.transport.webp_quality == 80
    assert config.transport.connect_timeout_ms == 3000
    assert config.transport.stale_timeout_ms == 500


def test_partial_yaml_override_merges_without_touching_other_defaults(tmp_path) -> None:
    path = tmp_path / "game.yaml"
    path.write_text(
        """
game_vision:
  vision_preset: close
  fog_mode: hard
  fov:
    scale: 0.70
  display:
    fullscreen: false
""".strip(),
        encoding="utf-8",
    )
    config = load_game_vision_config(path)
    assert config.vision_preset == "close"
    assert config.fog_mode == "hard"
    assert config.fov.scale == pytest.approx(0.7)
    assert config.fov.feather == pytest.approx(0.25)
    assert not config.display.fullscreen
    assert config.capture.fps == 30


@pytest.mark.parametrize(
    "yaml_text, message",
    [
        (
            "game_vision:\n  presets:\n    normal:\n      clear_m: 3.0\n      max_m: 2.0\n",
            "greater than clear_m",
        ),
        ("game_vision:\n  fov:\n    scale: 0\n", "scale"),
        ("game_vision:\n  fog_mode: opaque\n", "fade or hard"),
        ("game_vision:\n  fvo:\n    scale: 0.5\n", "unknown game_vision key"),
        ("game_vison:\n  fog_mode: hard\n", "unknown config root key"),
        ("game_vision:\n  presets:\n    normal:\n      clear_m: .nan\n", "finite"),
    ],
)
def test_invalid_config_is_rejected(tmp_path, yaml_text, message) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_game_vision_config(path)
