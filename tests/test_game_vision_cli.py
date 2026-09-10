from __future__ import annotations

from g1_bottle_reaction.game_vision.app import (
    _apply_cli,
    _create_source,
    build_parser,
    main,
    run_viewer,
)
from g1_bottle_reaction.game_vision.config import load_game_vision_config
from g1_bottle_reaction.game_vision.sources import G1RgbFrameSource
from g1_bottle_reaction.game_vision.frames import RgbdFrame
from g1_bottle_reaction.game_vision.sources import FrameSource
from g1_bottle_reaction.game_vision.sources import SyntheticFrameSource
from g1_bottle_reaction.game_vision.transport import TeleImagerProcessedFrameSource


def test_zero_hardware_headless_smoke(capsys) -> None:
    result = main(["--source", "synthetic", "--headless", "--max-frames", "3"])
    captured = capsys.readouterr()
    assert result == 0
    assert "SYNTHETIC RGBD CONNECTED" in captured.out
    assert "Fog: 1.5m -> 2.5m" in captured.out
    assert "GAME VISION STOPPED: 3 frame(s)" in captured.out


def test_safety_startup_explicitly_says_filters_are_not_applied(capsys) -> None:
    result = main(
        [
            "--source",
            "synthetic",
            "--display",
            "safety",
            "--headless",
            "--max-frames",
            "1",
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert "Display: UNFILTERED SAFETY" in captured.out
    assert "Game-only fog" in captured.out


def test_video_requires_a_path(capsys) -> None:
    assert main(["--source", "video", "--headless", "--max-frames", "1"]) == 2
    assert "--video PATH is required" in capsys.readouterr().err


def test_g1_rgb_requires_explicit_network_selection(capsys) -> None:
    assert main(["--source", "g1-rgb", "--headless", "--max-frames", "1"]) == 2
    assert "--network-interface or --network-address" in capsys.readouterr().err


def test_processed_g1_rejects_silently_ignored_filter_overrides(capsys) -> None:
    assert main(["--source", "g1", "--vision-preset", "normal", "--headless"]) == 2
    assert "already-filtered stream" in capsys.readouterr().err


def test_g1_source_routing_distinguishes_processed_and_official_rgb() -> None:
    parser = build_parser()
    processed_args = parser.parse_args(["--source", "g1", "--display", "both"])
    processed_config = _apply_cli(load_game_vision_config(), processed_args)
    processed = _create_source(processed_args, processed_config)
    assert isinstance(processed, TeleImagerProcessedFrameSource)
    assert processed.safety_port == 55559

    rgb_args = parser.parse_args(
        ["--source", "g1-rgb", "--network-interface", "eth-test"]
    )
    rgb_config = _apply_cli(load_game_vision_config(), rgb_args)
    rgb = _create_source(rgb_args, rgb_config)
    assert isinstance(rgb, G1RgbFrameSource)


def test_prefiltered_g1_stream_bypasses_local_depth_pipeline(monkeypatch) -> None:
    import numpy as np

    class PrefilteredSource(FrameSource):
        is_prefiltered_game = True
        label = "TEST REMOTE"

        def open(self):
            pass

        def read(self):
            return RgbdFrame(np.full((8, 10, 3), 90, dtype=np.uint8), None, 1.0)

        def close(self):
            pass

    def forbidden(*args, **kwargs):
        raise AssertionError("the receiver must not filter an already-filtered stream")

    monkeypatch.setattr(
        "g1_bottle_reaction.game_vision.app.GameVisionPipeline.process", forbidden
    )
    args = build_parser().parse_args(
        ["--source", "g1", "--headless", "--max-frames", "1"]
    )
    config = _apply_cli(load_game_vision_config(), args)
    assert run_viewer(PrefilteredSource(), config, args) == 0


def test_headless_synthetic_publisher_is_still_realtime_paced() -> None:
    args = build_parser().parse_args(
        ["--source", "synthetic", "--headless", "--publish-processed"]
    )
    config = _apply_cli(load_game_vision_config(), args)
    source = _create_source(args, config)
    assert isinstance(source, SyntheticFrameSource)
    assert source.realtime


def test_publishing_rejects_rgb_only_input(capsys) -> None:
    assert main(["--source", "webcam", "--publish-processed", "--headless"]) == 2
    assert "requires a depth-capable source" in capsys.readouterr().err


def test_malformed_yaml_has_a_clean_cli_error(tmp_path, capsys) -> None:
    path = tmp_path / "malformed.yaml"
    path.write_text("game_vision: [", encoding="utf-8")
    assert main(["--config", str(path), "--source", "synthetic", "--headless"]) == 2
    assert "could not read game vision config" in capsys.readouterr().err


def test_cli_exposes_requested_presets_and_display_modes() -> None:
    args = build_parser().parse_args(
        [
            "--source",
            "webcam",
            "--vision-preset",
            "long",
            "--fog-mode",
            "hard",
            "--fov-scale",
            "0.6",
            "--display",
            "both",
            "--windowed",
            "--g1-stream-host",
            "192.0.2.10",
            "--g1-stream-port",
            "45678",
            "--webp-quality",
            "75",
        ]
    )
    assert args.vision_preset == "long"
    assert args.fog_mode == "hard"
    assert args.fov_scale == 0.6
    assert args.display == "both"
    assert args.fullscreen is False
    assert args.g1_stream_host == "192.0.2.10"
    assert args.g1_stream_port == 45678
    assert args.webp_quality == 75
