from __future__ import annotations

import argparse
from collections import deque
from dataclasses import replace
from pathlib import Path
import sys
import time
from typing import Sequence

from .config import GameVisionConfig, load_game_vision_config
from .filters import FilterResult, GameVisionPipeline
from .sources import (
    EndOfStream,
    FrameSource,
    G1RgbFrameSource,
    NpzFrameSource,
    OpenCVFrameSource,
    RealSenseFrameSource,
    SyntheticFrameSource,
)
from .transport import TeleImagerProcessedFrameSource, TeleImagerZmqPublisher
from .viewer import HudState, OpenCVViewer, compose_view, draw_hud


PRESET_KEYS = {ord("1"): "close", ord("2"): "normal", ord("3"): "long"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m g1_bottle_reaction.game_vision",
        description="Standalone distance- and FOV-limited G1 game camera viewer",
    )
    parser.add_argument(
        "--source",
        choices=(
            "webcam",
            "video",
            "recording",
            "realsense",
            "g1",
            "g1-rgb",
            "synthetic",
            "dual",
            "usb-lan",
        ),
        default="webcam",
        help=(
            "frame input; g1 receives a PC2-filtered TeleImager stream, "
            "g1-rgb is the official RGB-only VideoClient"
        ),
    )
    parser.add_argument("--camera", type=int, default=0, help="OpenCV webcam index")
    parser.add_argument("--video", type=Path, help="ordinary RGB video path")
    parser.add_argument("--recording", type=Path, help="RGBD .npz recording path")
    parser.add_argument("--realsense-file", type=Path, help="librealsense .bag recording")
    parser.add_argument("--realsense-serial", help="optional D435i serial number")
    parser.add_argument("--loop", action="store_true", help="loop a finite recording")
    parser.add_argument("--network-interface", help="Unitree SDK network interface")
    parser.add_argument("--network-address", help="explicit local DDS IPv4 address")
    parser.add_argument("--usb-bind", default="127.0.0.1", help="local IPv4 for USB RTP receiver")
    parser.add_argument("--usb-host", default="127.0.0.1", help="verified robot IPv4 for sender binding")
    parser.add_argument("--usb-port", type=int, default=56000)
    parser.add_argument("--usb-width", type=int, choices=(640, 1280), default=1280)
    parser.add_argument("--usb-device", default="auto",
                        help="G1-side /dev/v4l/by-id path; auto selects one non-RealSense camera")
    parser.add_argument("--usb-rotate", type=int, choices=(0, 180), default=0, help="USB display rotation in degrees")
    parser.add_argument("--start-usb-sender", action="store_true", help="start supervised GStreamer sender over SSH")
    parser.add_argument("--ssh-target", default="g1")
    parser.add_argument("--ssh-control", help="optional existing SSH control socket")
    parser.add_argument("--gst-python", default="/usr/bin/python3", help="existing system Python with GI/GStreamer")
    parser.add_argument("--duration", type=float, help="dual/usb-lan run duration in seconds")
    parser.add_argument("--yolo", action="store_true", help="object YOLO on G1 in dual viewer; never USB")
    parser.add_argument("--yolo-model", type=Path, default=Path(__file__).resolve().parents[3] / ".runtime/models/yolo11n.pt")
    parser.add_argument("--yolo-confidence", type=float, default=0.25)
    parser.add_argument("--banana-confidence", type=float,
                        help="banana threshold; default from config/yolo_objects.yaml")
    parser.add_argument("--yolo-fps", type=float, default=15, help="maximum inference rate; latest frame only")
    parser.add_argument("--found-audio", action="store_true", help="opt-in reaction WAV after sustained G1 object detection")
    parser.add_argument("--found-duration", type=float, help="sustained person duration; default from person_found_audio.yaml")
    parser.add_argument("--detection-grace", type=float, help="brief dropout allowance in seconds")
    parser.add_argument("--audio-cooldown", type=float, help="audio re-trigger lockout in seconds")
    parser.add_argument("--rearm-absence", type=float, help="person must be absent this many seconds before rearming")
    parser.add_argument("--found-sound", help="existing WAV path; default: configured person reaction WAV")
    parser.add_argument("--found-output", choices=("g1", "pc"), help="audio output; default G1 speaker")
    parser.add_argument("--g1-stream-host", help="PC2 host publishing processed game images")
    parser.add_argument("--g1-stream-port", type=int, help="processed TeleImager ZMQ port")
    parser.add_argument(
        "--publish-processed",
        action="store_true",
        help="publish filtered frames with TeleImager ZMQ (run on the RGBD host)",
    )
    parser.add_argument("--publish-bind", help="local address for the processed publisher")
    parser.add_argument(
        "--publish-safety",
        action="store_true",
        help="also publish raw RGB on a separate commissioning port",
    )
    parser.add_argument(
        "--safety-stream-port",
        type=int,
        help="optional raw safety TeleImager port for publishing or remote safety/both view",
    )
    parser.add_argument(
        "--webp-quality",
        type=int,
        help="game WebP and optional safety JPEG quality 1-100",
    )
    parser.add_argument("--config", type=Path, help="dedicated game vision YAML override")
    parser.add_argument(
        "--vision-preset", choices=("close", "normal", "long"), help="distance preset"
    )
    parser.add_argument("--fog-mode", choices=("fade", "hard"), help="distance mask mode")
    parser.add_argument("--fov-scale", type=float, help="fully visible central span (0-1)")
    parser.add_argument("--fov-feather", type=float, help="FOV fade width (0-1)")
    parser.add_argument(
        "--display", choices=("game", "safety", "both"), help="view shown on this monitor"
    )
    fullscreen = parser.add_mutually_exclusive_group()
    fullscreen.add_argument("--fullscreen", dest="fullscreen", action="store_true")
    fullscreen.add_argument("--windowed", dest="fullscreen", action="store_false")
    parser.set_defaults(fullscreen=None)
    fps = parser.add_mutually_exclusive_group()
    fps.add_argument("--show-fps", dest="show_fps", action="store_true")
    fps.add_argument("--hide-fps", dest="show_fps", action="store_false")
    parser.set_defaults(show_fps=None)
    parser.add_argument(
        "--allow-rgb-only-g1",
        action="store_true",
        help="diagnostic only: show G1 RGB with no distance fog instead of fail-closed black",
    )
    parser.add_argument(
        "--headless", action="store_true", help="process frames without opening a window"
    )
    parser.add_argument(
        "--max-frames", type=int, help="stop after N frames (useful for smoke tests)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.found_audio and (not args.yolo or args.source != "dual"):
            raise ValueError("--found-audio requires --source dual --yolo")
        if args.yolo and args.source != "dual":
            raise ValueError("--yolo requires --source dual; USB inference is not supported")
        if args.source in {"dual", "usb-lan"}:
            from .dual import run
            return run(args)
        config = _apply_cli(load_game_vision_config(args.config), args)
        _validate_args(args, config)
        source = _create_source(args, config)
        return run_viewer(source, config, args)
    except (RuntimeError, ValueError) as exc:
        print(f"GAME VISION ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("GAME VISION INTERRUPTED", file=sys.stderr)
        return 130


def run_viewer(
    source: FrameSource,
    config: GameVisionConfig,
    args: argparse.Namespace,
) -> int:
    distance = config.selected_distance
    pipeline = GameVisionPipeline(
        clear_m=distance.clear_m,
        max_m=distance.max_m,
        fog_mode=config.fog_mode,
        fov_enabled=config.fov.enabled,
        fov_scale=config.fov.scale,
        fov_feather=config.fov.feather,
    )
    viewer = None if args.headless else OpenCVViewer(fullscreen=config.display.fullscreen)
    publisher = _create_publisher(args, config) if args.publish_processed else None
    timestamps: deque[float] = deque(maxlen=31)
    frames_seen = 0
    current_preset = config.vision_preset
    warned_missing_depth = False
    warned_remote_controls = False
    try:
        source.open()
        if publisher is not None:
            publisher.open()
            _print_publishing(publisher)
        if viewer is not None:
            viewer.open()
        while True:
            try:
                frame = source.read().validate()
            except EndOfStream:
                break
            now = time.monotonic()
            timestamps.append(now)
            fps = _measured_fps(timestamps)
            if source.is_prefiltered_game:
                missing_depth = "preview"
                filtered = FilterResult(frame.bgr, False, False)
            else:
                missing_depth = (
                    "black"
                    if source.is_g1_rgb_only and not args.allow_rgb_only_g1
                    else "preview"
                )
                filtered = pipeline.process(frame, missing_depth=missing_depth)
            if (
                frame.depth_m is None
                and not source.is_prefiltered_game
                and not warned_missing_depth
            ):
                _print_missing_depth_notice(source, missing_depth)
                warned_missing_depth = True
            if publisher is not None:
                if not filtered.depth_active:
                    raise RuntimeError(
                        "processed publishing stopped because this frame has no aligned depth"
                    )
                published_game = draw_hud(
                    filtered.bgr,
                    _hud_state(
                        fps,
                        pipeline,
                        current_preset,
                        filtered,
                        config,
                        prefiltered=False,
                    ),
                    mode="game",
                )
                publisher.publish(
                    published_game,
                    frame.bgr if args.publish_safety else None,
                )
            if frames_seen == 0:
                _print_connected(source, frame.bgr.shape, config, pipeline)
            frames_seen += 1

            if viewer is not None:
                composed = compose_view(
                    filtered.bgr,
                    frame.safety_bgr if frame.safety_bgr is not None else frame.bgr,
                    mode=config.display.mode,
                    safety_inset_scale=config.display.safety_inset_scale,
                )
                shown = draw_hud(
                    composed,
                    _hud_state(
                        fps,
                        pipeline,
                        current_preset,
                        filtered,
                        config,
                        prefiltered=source.is_prefiltered_game,
                    ),
                    mode=config.display.mode,
                )
                key = viewer.show(shown)
                if key in {27, ord("q"), ord("Q")}:
                    break
                if key in {ord("f"), ord("F")}:
                    viewer.toggle_fullscreen()
                elif key in PRESET_KEYS or key in {ord("["), ord("]")}:
                    if source.is_prefiltered_game:
                        if not warned_remote_controls:
                            print(
                                "REMOTE FILTER SETTINGS: change presets on the PC2 sender",
                                file=sys.stderr,
                            )
                            warned_remote_controls = True
                    elif key in PRESET_KEYS:
                        current_preset = PRESET_KEYS[key]
                        selected = config.presets[current_preset]
                        pipeline.set_distance(selected.clear_m, selected.max_m)
                    elif key == ord("["):
                        current_preset = "custom"
                        pipeline.adjust_max_distance(-config.controls.distance_step_m)
                    else:
                        current_preset = "custom"
                        pipeline.adjust_max_distance(config.controls.distance_step_m)

            if args.max_frames is not None and frames_seen >= args.max_frames:
                break
    finally:
        try:
            if viewer is not None:
                viewer.close()
        finally:
            try:
                if publisher is not None:
                    publisher.close()
            finally:
                source.close()
    if frames_seen == 0:
        raise RuntimeError("source ended before producing a frame")
    print(f"GAME VISION STOPPED: {frames_seen} frame(s), {_measured_fps(timestamps):.1f} FPS")
    return 0


def _apply_cli(config: GameVisionConfig, args: argparse.Namespace) -> GameVisionConfig:
    fov = config.fov
    display = config.display
    if args.fov_scale is not None:
        fov = replace(fov, scale=args.fov_scale)
    if args.fov_feather is not None:
        fov = replace(fov, feather=args.fov_feather)
    if args.fullscreen is not None:
        display = replace(display, fullscreen=args.fullscreen)
    if args.show_fps is not None:
        display = replace(display, show_fps=args.show_fps)
    if args.display is not None:
        display = replace(display, mode=args.display)
    merged = replace(
        config,
        vision_preset=args.vision_preset or config.vision_preset,
        fog_mode=args.fog_mode or config.fog_mode,
        fov=fov,
        display=display,
    )
    merged.validate()
    return merged


def _validate_args(args: argparse.Namespace, config: GameVisionConfig) -> None:
    if args.max_frames is not None and args.max_frames <= 0:
        raise ValueError("--max-frames must be positive")
    if args.source == "video" and args.video is None:
        raise ValueError("--video PATH is required with --source video")
    if args.source == "recording" and args.recording is None:
        raise ValueError("--recording PATH is required with --source recording")
    if args.source == "g1-rgb" and not (
        args.network_interface or args.network_address
    ):
        raise ValueError(
            "--network-interface or --network-address is required with --source g1-rgb"
        )
    if args.source == "g1" and any(
        value is not None
        for value in (args.vision_preset, args.fog_mode, args.fov_scale, args.fov_feather)
    ):
        raise ValueError(
            "--source g1 receives an already-filtered stream; set vision/fog/FOV "
            "options on the PC2 --publish-processed command"
        )
    if args.publish_processed and args.source not in {
        "synthetic",
        "recording",
        "realsense",
    }:
        raise ValueError(
            "--publish-processed requires a depth-capable source: "
            "synthetic, recording, or realsense"
        )
    if args.publish_safety and not args.publish_processed:
        raise ValueError("--publish-safety requires --publish-processed")


def _create_source(args: argparse.Namespace, config: GameVisionConfig) -> FrameSource:
    capture = config.capture
    if args.source == "webcam":
        return OpenCVFrameSource(
            args.camera, width=capture.width, height=capture.height, fps=capture.fps
        )
    if args.source == "video":
        assert args.video is not None
        return OpenCVFrameSource(args.video, loop=args.loop)
    if args.source == "recording":
        assert args.recording is not None
        return NpzFrameSource(args.recording, loop=args.loop)
    if args.source == "realsense":
        return RealSenseFrameSource(
            width=capture.width,
            height=capture.height,
            fps=capture.fps,
            timeout_ms=capture.timeout_ms,
            serial=args.realsense_serial,
            bag_file=args.realsense_file,
            repeat_bag=args.loop,
        )
    if args.source == "g1":
        safety_port = None
        if config.display.mode in {"safety", "both"}:
            safety_port = (
                args.safety_stream_port
                if args.safety_stream_port is not None
                else config.transport.safety_port
            )
        return TeleImagerProcessedFrameSource(
            args.g1_stream_host or config.transport.g1_host,
            port=(
                args.g1_stream_port
                if args.g1_stream_port is not None
                else config.transport.game_port
            ),
            safety_port=safety_port,
            connect_timeout_seconds=config.transport.connect_timeout_ms / 1000.0,
            stale_timeout_seconds=config.transport.stale_timeout_ms / 1000.0,
        )
    if args.source == "g1-rgb":
        return G1RgbFrameSource(
            args.network_interface,
            network_address=args.network_address,
            timeout_seconds=capture.timeout_ms / 1000.0,
        )
    return SyntheticFrameSource(
        width=capture.width,
        height=capture.height,
        fps=capture.fps,
        # A headless smoke test may run as fast as possible, but a network
        # publisher must honor the configured capture rate and not flood Wi-Fi.
        realtime=not args.headless or args.publish_processed,
    )


def _create_publisher(
    args: argparse.Namespace, config: GameVisionConfig
) -> TeleImagerZmqPublisher:
    game_port = (
        args.g1_stream_port
        if args.g1_stream_port is not None
        else config.transport.game_port
    )
    safety_port = None
    if args.publish_safety:
        safety_port = (
            args.safety_stream_port
            if args.safety_stream_port is not None
            else config.transport.safety_port
        )
    quality = (
        args.webp_quality
        if args.webp_quality is not None
        else config.transport.webp_quality
    )
    return TeleImagerZmqPublisher(
        game_port=game_port,
        safety_port=safety_port,
        bind_host=args.publish_bind or config.transport.bind_host,
        webp_quality=quality,
    )


def _hud_state(
    fps: float,
    pipeline: GameVisionPipeline,
    preset: str,
    filtered: FilterResult,
    config: GameVisionConfig,
    *,
    prefiltered: bool,
) -> HudState:
    return HudState(
        fps=fps,
        clear_m=pipeline.clear_m,
        max_m=pipeline.max_m,
        fog_mode=pipeline.fog_mode,
        fov_scale=pipeline.fov_scale,
        preset=preset,
        depth_active=filtered.depth_active,
        fail_closed=filtered.fail_closed,
        prefiltered=prefiltered,
        fov_enabled=pipeline.fov_enabled,
        show_fps=config.display.show_fps,
    )


def _measured_fps(timestamps: deque[float]) -> float:
    if len(timestamps) < 2:
        return 0.0
    elapsed = timestamps[-1] - timestamps[0]
    return 0.0 if elapsed <= 0 else (len(timestamps) - 1) / elapsed


def _print_connected(
    source: FrameSource,
    shape: tuple[int, ...],
    config: GameVisionConfig,
    pipeline: GameVisionPipeline,
) -> None:
    height, width = shape[:2]
    print(f"{source.label} CONNECTED")
    print(f"Resolution: {width}x{height}")
    print(f"FPS target: {config.capture.fps}")
    if config.display.mode == "safety":
        print("Display: UNFILTERED SAFETY (game fog/FOV is not applied to this view)")
        if source.is_prefiltered_game:
            print("Game fog/FOV: controlled on the PC2 game stream")
        else:
            fov = (
                f"{pipeline.fov_scale * 100:.0f}%"
                if pipeline.fov_enabled
                else "OFF"
            )
            print(
                f"Game-only fog: {pipeline.clear_m:.1f}m -> {pipeline.max_m:.1f}m; "
                f"FOV: {fov}"
            )
    elif source.is_prefiltered_game:
        print("Fog/FOV: already applied by the PC2 sender")
        print("Remote filter keys: disabled on this receiver")
    else:
        print(
            f"Fog: {pipeline.clear_m:.1f}m -> {pipeline.max_m:.1f}m "
            f"({pipeline.fog_mode})"
        )
        print(
            f"FOV: {pipeline.fov_scale * 100:.0f}%"
            if pipeline.fov_enabled
            else "FOV: OFF"
        )
    if config.display.mode == "both":
        print("Safety inset: UNFILTERED (commissioning only)")
    print("SAFETY: restricted video is not a collision-avoidance view; use a spotter")


def _print_publishing(publisher: TeleImagerZmqPublisher) -> None:
    print(
        "TELEIMAGER PROCESSED STREAM: "
        f"tcp://{publisher.bind_host}:{publisher.game_port} "
        f"({publisher.codec}, hidden mask reapplied)"
    )
    if publisher.safety_port is not None:
        print(
            "TELEIMAGER SAFETY STREAM: "
            f"tcp://{publisher.bind_host}:{publisher.safety_port} "
            f"({publisher.safety_codec})"
        )


def _print_missing_depth_notice(source: FrameSource, policy: str) -> None:
    if policy == "black":
        print(
            "DEPTH UNAVAILABLE: official G1 VideoClient is RGB-only; "
            "game view is fail-closed. Use --display safety for commissioning.",
            file=sys.stderr,
        )
    else:
        print(
            f"DEPTH UNAVAILABLE from {source.label}: distance fog is disabled; "
            "UI and FOV preview remain active.",
            file=sys.stderr,
        )
