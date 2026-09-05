from __future__ import annotations

import argparse
from dataclasses import replace
import logging
import sys
from pathlib import Path
import time

from g1_bottle_reaction.audio.classifiers import YamnetClassifier
from g1_bottle_reaction.audio.debug import AudioDebugRecorder
from g1_bottle_reaction.audio.offline import classify_wav_file
from g1_bottle_reaction.audio.pipeline import AudioMonitor, AudioProcessor
from g1_bottle_reaction.audio.sources import (
    G1MicSource,
    create_windows_mic_source,
    list_input_devices,
)
from g1_bottle_reaction.adapters.g1_robot import (
    G1RobotAdapter,
    probe_g1_connection,
)
from g1_bottle_reaction.adapters.mock_robot import MockRobotAdapter
from g1_bottle_reaction.adapters.mock_navigation import MockNavigationAdapter
from g1_bottle_reaction.adapters.navigation import NavigationAdapter, NavigationTransport
from g1_bottle_reaction.adapters.mujoco_robot import (
    MujocoRobotAdapter,
    default_g1_model_path,
)
from g1_bottle_reaction.adapters.remote_navigation import RemoteNavigationAdapter
from g1_bottle_reaction.adapters.robot import RobotAdapter
from g1_bottle_reaction.adapters.speech import create_speech_backend
from g1_bottle_reaction.app import (
    BottleReactionApp,
    StealthGameApp,
    run_audio_only,
    run_audio_simulation,
    run_simulation,
    run_stealth_simulation,
    run_stealth_webcam,
    run_tracking_preview,
    run_webcam,
)
from g1_bottle_reaction.config.loader import default_config_path, load_config
from g1_bottle_reaction.custom_motion import (
    build_relative_trajectory,
    default_custom_motion_config_path,
    format_dry_run,
    load_custom_motion_config,
)
from g1_bottle_reaction.reactions.engine import ReactionEngine
from g1_bottle_reaction.reactions.models import Reaction
from g1_bottle_reaction.simulation.motion import (
    default_motion_config_path,
    load_motion_library,
)
from g1_bottle_reaction.vision.camera import (
    CameraSource,
    G1CameraSource,
    OpenCVCameraSource,
    TeleImagerCameraSource,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="G1 bottle reaction prototype")
    parser.add_argument("--simulate", action="store_true", help="use scripted input")
    parser.add_argument(
        "--simulate-stealth",
        action="store_true",
        help="run the scripted PLAYER stealth round without a camera or YOLO",
    )
    parser.add_argument(
        "--game",
        choices=("stealth-phone",),
        help="enable an explicit game mode; existing bottle mode remains the default",
    )
    parser.add_argument(
        "--simulate-audio",
        action="store_true",
        help="run deterministic music-score simulation without audio dependencies",
    )
    parser.add_argument("--robot", choices=("mock", "mujoco", "g1"), default="mock")
    parser.add_argument(
        "--navigation",
        choices=("disabled", "mock", "remote"),
        default="disabled",
        help="independent navigation capability backend",
    )
    parser.add_argument("--enable-real-navigation", action="store_true")
    parser.add_argument("--navigation-endpoint")
    parser.add_argument(
        "--navigation-test", choices=("health", "status", "pose")
    )
    parser.add_argument(
        "--start-patrol",
        nargs="?",
        const="__DEFAULT_ROUTE__",
        metavar="ROUTE_ID",
        help="start the default or specified route after application startup",
    )
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument(
        "--camera-source",
        choices=("opencv", "g1", "g1-teleimager"),
        default="opencv",
        help="opencv webcam, G1 videohub VideoClient, or legacy TeleImager",
    )
    parser.add_argument(
        "--g1-image-server-ip",
        default="192.168.123.164",
        help="deprecated: used only with --camera-source g1-teleimager",
    )
    parser.add_argument("--no-camera", action="store_true")
    parser.add_argument(
        "--speech", choices=("auto", "console", "mute", "aivis"), default="auto"
    )
    parser.add_argument("--speech-debug", action="store_true")
    parser.add_argument("--list-aivis-speakers", action="store_true")
    parser.add_argument("--check-aivis", action="store_true")
    parser.add_argument("--precache-speech", action="store_true")
    parser.add_argument("--preview-speech", metavar="TEXT")
    parser.add_argument("--voice-profile", default="neutral", metavar="NAME")
    parser.add_argument("--preview-voice-profiles", metavar="TEXT")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--network-interface")
    parser.add_argument(
        "--network-address",
        help="Windows IPv4 address for G1 DDS; bypasses interface alias resolution",
    )
    parser.add_argument("--enable-real-robot", action="store_true")
    parser.add_argument(
        "--g1-motion", choices=("disabled", "safe-actions"), default="disabled"
    )
    parser.add_argument("--g1-custom-motion", action="store_true")
    parser.add_argument(
        "--custom-motion-amplitude",
        choices=("small", "medium", "demo"),
        default="small",
    )
    parser.add_argument(
        "--custom-motion-config",
        type=Path,
        default=default_custom_motion_config_path(),
    )
    parser.add_argument("--custom-motion-dry-run", action="store_true")
    parser.add_argument("--custom-motion-with-speech", action="store_true")
    parser.add_argument("--enable-custom-notice-reaction", action="store_true")
    parser.add_argument("--reaction-timing-debug", action="store_true")
    parser.add_argument("--notice-speech-delay", type=float)
    parser.add_argument(
        "--g1-test",
        choices=("connection", "camera", "speaker", "wave", "custom-notice"),
    )
    parser.add_argument("--wav", type=Path, metavar="PATH")
    parser.add_argument(
        "--audio-output", choices=("windows", "g1"), default="windows"
    )
    parser.add_argument("--mujoco-model", type=Path, default=default_g1_model_path())
    parser.add_argument(
        "--motion-config", type=Path, default=default_motion_config_path()
    )
    parser.add_argument("--preview-motion", metavar="NAME")
    parser.add_argument(
        "--preview-tracking",
        action="store_true",
        help="preview continuous left/center/right tracking without a camera",
    )
    parser.add_argument(
        "--tracking-debug",
        action="store_true",
        help="show detailed desired/actual tracking values and frame guides",
    )
    parser.add_argument("--list-motions", action="store_true")
    parser.add_argument("--mujoco-headless", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--audio-source", choices=("none", "windows", "g1"), default="none"
    )
    parser.add_argument("--audio-classifier", choices=("yamnet",), default="yamnet")
    parser.add_argument(
        "--audio-mode", choices=("normal", "raw", "auto")
    )
    parser.add_argument("--audio-device", type=_audio_device_value)
    parser.add_argument("--list-audio-devices", action="store_true")
    parser.add_argument("--audio-debug", action="store_true")
    parser.add_argument(
        "--record-audio-debug",
        nargs="?",
        const=Path("__MODE_DEFAULT__"),
        type=Path,
        metavar="PATH",
        help="record about 10 seconds of normalized YAMNet input PCM",
    )
    parser.add_argument(
        "--audio-file",
        type=Path,
        metavar="PATH",
        help="classify a WAV file instead of opening a microphone",
    )
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    return parser


def _create_robot(args: argparse.Namespace, config) -> RobotAdapter:
    if args.robot == "mock":
        return MockRobotAdapter()
    if args.robot == "mujoco":
        adapter = MujocoRobotAdapter(
            model_path=args.mujoco_model,
            motion_config_path=args.motion_config,
            launch_viewer=not args.mujoco_headless,
        )
        adapter.initialize()
        return adapter
    adapter = G1RobotAdapter(
        args.network_interface or "",
        network_address=args.network_address,
        enabled=args.enable_real_robot,
        motion_mode=args.g1_motion,
        timeout_seconds=config.g1.client_timeout_seconds,
        release_delay_seconds=config.g1.arm_release_delay_seconds,
        custom_motion_enabled=args.g1_custom_motion,
        custom_motion_amplitude=args.custom_motion_amplitude,
        custom_motion_config=(
            load_custom_motion_config(args.custom_motion_config)
            if args.g1_custom_motion
            else None
        ),
    )
    adapter.initialize()
    return adapter


def _create_navigation(
    args: argparse.Namespace,
    config,
    *,
    transport: NavigationTransport | None = None,
    start_heartbeat: bool = True,
) -> NavigationAdapter | None:
    if args.navigation == "disabled":
        if args.enable_real_navigation or args.navigation_endpoint:
            raise ValueError(
                "Navigation flags require --navigation mock or --navigation remote"
            )
        return None
    if args.navigation == "mock":
        return MockNavigationAdapter()
    if not args.navigation_endpoint:
        raise ValueError("--navigation remote requires --navigation-endpoint")
    if transport is None:
        raise RuntimeError(
            "No Ubuntu bridge transport is implemented yet; inject a verified "
            "NavigationTransport after the bridge protocol is selected"
        )
    adapter = RemoteNavigationAdapter(
        args.navigation_endpoint,
        transport,
        real_navigation_enabled=args.enable_real_navigation,
        command_timeout_s=config.navigation.command_timeout_s,
        heartbeat_interval_s=config.navigation.heartbeat_interval_s,
        heartbeat_timeout_s=config.navigation.heartbeat_timeout_s,
    )
    if start_heartbeat:
        adapter.start_heartbeat()
    return adapter


def _create_camera_source(args: argparse.Namespace, config) -> CameraSource:
    if args.camera_source == "opencv":
        return OpenCVCameraSource(args.camera)
    if args.camera_source == "g1":
        return G1CameraSource(
            args.network_interface,
            network_address=args.network_address,
            timeout_seconds=config.g1.camera_frame_timeout_seconds,
            read_attempts=config.g1.camera_reconnect_attempts,
            retry_delay_seconds=config.g1.camera_reconnect_delay_seconds,
        )
    logging.warning(
        "--camera-source g1-teleimager is legacy; standard G1 camera uses VideoClient"
    )
    return TeleImagerCameraSource(
        args.g1_image_server_ip,
        frame_timeout_seconds=config.g1.camera_frame_timeout_seconds,
        reconnect_attempts=config.g1.camera_reconnect_attempts,
        reconnect_delay_seconds=config.g1.camera_reconnect_delay_seconds,
    )


def _create_g1_audio_output(args: argparse.Namespace, config):
    from g1_bottle_reaction.adapters.g1_audio import G1AudioOutput

    return G1AudioOutput(
        args.network_interface or "",
        network_address=args.network_address,
        volume=config.g1.speaker_volume,
        timeout_seconds=config.g1.client_timeout_seconds,
        chunk_bytes=config.g1.audio_chunk_bytes,
        chunk_delay_seconds=config.g1.audio_chunk_delay_seconds,
    )


def _audio_device_value(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


def _create_audio_monitor(
    args: argparse.Namespace,
    config,
    app: BottleReactionApp,
) -> AudioMonitor | None:
    if args.audio_source == "none":
        return None
    if args.audio_source == "windows":
        source = create_windows_mic_source(
            args.audio_mode,
            device=args.audio_device,
            target_sample_rate=config.audio.target_sample_rate,
        )
    else:
        source = G1MicSource()
    classifier = _create_classifier(config)
    recorder = (
        AudioDebugRecorder(
            _debug_record_path(args),
            sample_rate=config.audio.target_sample_rate,
            duration_seconds=config.audio.debug_record_seconds,
        )
        if args.record_audio_debug is not None
        else None
    )
    processor = AudioProcessor(config.audio, classifier, debug_recorder=recorder)
    return AudioMonitor(
        source,
        processor,
        lambda update, now: app.process_audio(update, now=now),
        queue_max_chunks=config.audio.queue_max_chunks,
    )


def _debug_record_path(args: argparse.Namespace) -> Path:
    if args.record_audio_debug != Path("__MODE_DEFAULT__"):
        return args.record_audio_debug
    names = {
        "normal": "audio_normal.wav",
        "raw": "audio_raw.wav",
        "auto": "audio_auto.wav",
    }
    return Path("debug") / names[args.audio_mode]


def _create_classifier(config) -> YamnetClassifier:
    return YamnetClassifier(
        model_url=config.audio.yamnet.model_url,
        cache_dir=config.audio.yamnet.cache_dir,
        music_labels=config.audio.yamnet.music_labels,
        top_n=max(10, config.audio.yamnet.top_n),
    )


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_console()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.list_motions:
            for name in load_motion_library(args.motion_config).names:
                print(name)
            return 0
        if args.list_audio_devices:
            devices = list_input_devices()
            print("Available audio input devices:")
            if devices:
                print("\n".join(devices))
            else:
                print("(no input devices found)")
            return 0
        config = load_config(args.config)
        config = _apply_custom_notice_overrides(config, args)
        if args.navigation_test is not None:
            _run_navigation_diagnostic(args, config)
            return 0
        if args.g1_test is not None:
            _run_g1_diagnostic(args, config)
            return 0
        if args.list_aivis_speakers:
            _list_aivis_speakers(config)
            return 0
        if args.check_aivis:
            _check_aivis(config)
            return 0
        if args.precache_speech:
            _precache_speech(config, debug=args.speech_debug)
            return 0
        if args.preview_speech is not None:
            _preview_speech(
                config,
                args.preview_speech,
                args.voice_profile,
                debug=args.speech_debug,
            )
            return 0
        if args.preview_voice_profiles is not None:
            _preview_voice_profiles(
                config, args.preview_voice_profiles, debug=args.speech_debug
            )
            return 0
        if args.audio_mode is None:
            args.audio_mode = config.audio.mode
        if (
            args.record_audio_debug is not None
            and args.audio_file is None
            and args.audio_source == "none"
        ):
            raise ValueError(
                "--record-audio-debug requires --audio-source windows"
            )
        robot = _create_robot(args, config)
        try:
            navigation = _create_navigation(args, config)
        except Exception:
            robot.close()
            raise
        if args.preview_motion is not None:
            if navigation is not None:
                navigation.close()
            if not isinstance(robot, MujocoRobotAdapter):
                robot.close()
                raise ValueError("--preview-motion requires --robot mujoco")
            _run_motion_preview(robot, args.preview_motion)
            return 0
        if args.preview_tracking:
            if navigation is not None:
                navigation.close()
            if not isinstance(robot, MujocoRobotAdapter):
                robot.close()
                raise ValueError("--preview-tracking requires --robot mujoco")
            run_tracking_preview(
                robot,
                config.stealth_game.tracking,
                realtime=not args.mujoco_headless,
            )
            return 0
        if (
            isinstance(robot, MujocoRobotAdapter)
            and args.no_camera
            and args.audio_source == "none"
            and args.audio_file is None
            and not args.simulate
            and not args.simulate_audio
            and not args.simulate_stealth
        ):
            if navigation is not None:
                navigation.close()
            print("MuJoCo viewer is ready; close the viewer or press Ctrl+C to stop")
            try:
                robot.wait_until_viewer_closed()
            finally:
                robot.close()
            return 0
        try:
            audio_output = (
                _create_g1_audio_output(args, config)
                if args.audio_output == "g1" and args.speech in {"auto", "aivis"}
                else None
            )
            speech = create_speech_backend(
                args.speech,
                aivis_config=config.speech.aivis,
                audio_output=audio_output,
                debug=args.speech_debug,
            )
        except Exception:
            robot.close()
            if navigation is not None:
                navigation.close()
            raise
        stealth_mode = args.game == "stealth-phone" or args.simulate_stealth
        app = (
            StealthGameApp(config, robot, speech, navigation)
            if stealth_mode
            else BottleReactionApp(config, robot, speech, navigation)
        )
        if args.start_patrol is not None:
            route_id = (
                None if args.start_patrol == "__DEFAULT_ROUTE__" else args.start_patrol
            )
            try:
                app.start_patrol(route_id)
            except Exception:
                app.close()
                raise
        if args.audio_file is not None:
            if args.record_audio_debug is not None:
                raise ValueError(
                    "--record-audio-debug records microphone input and cannot be used with --audio-file"
                )
            classifier = _create_classifier(config)
            try:
                classify_wav_file(
                    args.audio_file,
                    config=config.audio,
                    classifier=classifier,
                    app=app,
                )
            finally:
                app.close()
        elif args.simulate_stealth:
            assert isinstance(app, StealthGameApp)
            run_stealth_simulation(
                app,
                realtime_scale=0.0 if args.mujoco_headless or args.headless else 1.0,
            )
        elif args.simulate_audio:
            run_audio_simulation(app)
        elif args.simulate:
            run_simulation(
                app, realtime_scale=config.simulation_realtime_scale
            )
        else:
            audio_monitor = _create_audio_monitor(args, config, app)
            if args.no_camera:
                if audio_monitor is None:
                    raise ValueError("--no-camera requires an enabled --audio-source")
                run_audio_only(
                    app,
                    audio_monitor,
                    audio_source_label=args.audio_source,
                    audio_debug=args.audio_debug,
                )
            elif stealth_mode:
                assert isinstance(app, StealthGameApp)
                run_stealth_webcam(
                    app,
                    camera_source=_create_camera_source(args, config),
                    headless=args.headless,
                    audio_monitor=audio_monitor,
                    audio_source_label=args.audio_source,
                    audio_debug=args.audio_debug,
                    tracking_debug=args.tracking_debug,
                )
            else:
                run_webcam(
                    app,
                    camera_source=_create_camera_source(args, config),
                    headless=args.headless,
                    audio_monitor=audio_monitor,
                    audio_source_label=args.audio_source,
                    audio_debug=args.audio_debug,
                )
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _run_navigation_diagnostic(args: argparse.Namespace, config) -> None:
    navigation = _create_navigation(
        args,
        config,
        start_heartbeat=False,
    )
    if navigation is None:
        raise ValueError("--navigation-test requires an enabled navigation backend")
    try:
        if args.navigation_test == "health":
            print(f"Navigation health: {'OK' if navigation.health() else 'NOT HEALTHY'}")
        elif args.navigation_test == "status":
            print(f"Navigation status: {navigation.status()}")
        else:
            print(f"Navigation pose: {navigation.pose()}")
    finally:
        navigation.close()


def _run_g1_diagnostic(args: argparse.Namespace, config) -> None:
    if args.g1_test == "custom-notice":
        _run_custom_notice_diagnostic(args, config)
        return
    if args.g1_test == "camera":
        _run_g1_camera_diagnostic(_create_camera_source_for_diagnostic(args, config))
        return
    if not (args.network_interface or args.network_address):
        raise ValueError(
            f"--g1-test {args.g1_test} requires --network-interface or "
            "--network-address"
        )
    if args.g1_test == "connection":
        probe_g1_connection(
            args.network_interface,
            network_address=args.network_address,
            timeout_seconds=config.g1.client_timeout_seconds,
        )
        print("G1 DDS and AudioClient initialization: OK", flush=True)
        return
    if args.g1_test == "speaker":
        if args.wav is None:
            raise ValueError("--g1-test speaker requires --wav PATH")
        output = _create_g1_audio_output(args, config)
        output.play_wav(args.wav)
        print("G1 speaker test: complete", flush=True)
        return
    if args.robot != "g1" or not args.enable_real_robot:
        raise RuntimeError(
            "Arm action test requires both --robot g1 and --enable-real-robot"
        )
    if args.g1_motion != "safe-actions":
        raise RuntimeError("Arm action test requires --g1-motion safe-actions")
    robot = _create_robot(args, config)
    robot.play_motion("notice")
    print("G1 arm Action notice test: complete", flush=True)


def _run_custom_notice_diagnostic(args: argparse.Namespace, config) -> None:
    motion_config = load_custom_motion_config(args.custom_motion_config)
    if args.custom_motion_dry_run:
        base_pose = {name: 0.0 for name in motion_config.joints}
        report = build_relative_trajectory(
            motion_config,
            "custom_notice",
            args.custom_motion_amplitude,
            base_pose,
        )
        print(format_dry_run(report, motion_config), flush=True)
        return
    if args.robot != "g1" or not args.enable_real_robot:
        raise RuntimeError(
            "Custom notice requires --robot g1 and --enable-real-robot"
        )
    if args.g1_motion != "safe-actions" or not args.g1_custom_motion:
        raise RuntimeError(
            "Custom notice requires --g1-motion safe-actions and --g1-custom-motion"
        )
    if not (args.network_interface or args.network_address):
        raise ValueError(
            "Custom notice requires --network-interface or --network-address"
        )
    robot = _create_robot(args, config)
    try:
        if args.custom_motion_with_speech:
            output = (
                _create_g1_audio_output(args, config)
                if args.audio_output == "g1"
                else None
            )
            speech = create_speech_backend(
                args.speech,
                aivis_config=config.speech.aivis,
                audio_output=output,
                debug=args.speech_debug,
            )
            reaction = Reaction(
                name="CUSTOM_NOTICE_DIAGNOSTIC",
                motion="custom_notice",
                speech="ん？",
                speech_delay_seconds=(
                    args.notice_speech_delay
                    if args.notice_speech_delay is not None
                    else motion_config.speech_delay_seconds
                ),
                voice_profile="curious",
            )
            engine = ReactionEngine(
                replace(
                    config.reaction,
                    items={"CUSTOM_NOTICE_DIAGNOSTIC": reaction},
                    timeline_debug=(
                        args.reaction_timing_debug
                        or config.reaction.timeline_debug
                    ),
                ),
                robot,
                speech,
                start_worker=False,
            )
            engine.execute(reaction)
        else:
            started = robot.play_motion_timed(
                "custom_notice",
                timeline_start=time.monotonic(),
                timing_debug=args.reaction_timing_debug,
            )
            if not started:
                raise RuntimeError("custom_notice was rejected because arm control is busy")
        if not robot.wait_for_custom_motion(5.0):
            raise RuntimeError("custom_notice did not finish within 5 seconds")
        print("G1 custom_notice test: complete", flush=True)
    finally:
        robot.close()


def _apply_custom_notice_overrides(config, args: argparse.Namespace):
    if args.notice_speech_delay is not None and args.notice_speech_delay < 0:
        raise ValueError("--notice-speech-delay cannot be negative")
    if (
        args.enable_custom_notice_reaction
        and args.robot == "g1"
        and not args.g1_custom_motion
    ):
        raise ValueError(
            "Real-G1 custom notice integration requires --g1-custom-motion"
        )
    timeline_debug = config.reaction.timeline_debug or args.reaction_timing_debug
    if not args.enable_custom_notice_reaction:
        if timeline_debug == config.reaction.timeline_debug:
            return config
        return replace(
            config,
            reaction=replace(config.reaction, timeline_debug=timeline_debug),
        )
    custom_config = load_custom_motion_config(args.custom_motion_config)
    items = dict(config.reaction.items)
    current = items["SUSPICION_STARTED"]
    items["SUSPICION_STARTED"] = replace(
        current,
        motion="custom_notice",
        speech="ん？",
        speech_delay_seconds=(
            args.notice_speech_delay
            if args.notice_speech_delay is not None
            else custom_config.speech_delay_seconds
        ),
    )
    return replace(
        config,
        reaction=replace(
            config.reaction,
            items=items,
            timeline_debug=timeline_debug,
        ),
    )


def _create_camera_source_for_diagnostic(args: argparse.Namespace, config) -> CameraSource:
    if args.camera_source == "g1-teleimager":
        logging.warning(
            "TeleImager camera diagnostic is legacy and may conflict with videohub_pc4"
        )
        return TeleImagerCameraSource(
            args.g1_image_server_ip,
            frame_timeout_seconds=config.g1.camera_frame_timeout_seconds,
            reconnect_attempts=config.g1.camera_reconnect_attempts,
            reconnect_delay_seconds=config.g1.camera_reconnect_delay_seconds,
        )
    if not (args.network_interface or args.network_address):
        raise ValueError(
            "--g1-test camera requires --network-interface or --network-address"
        )
    return G1CameraSource(
        args.network_interface,
        network_address=args.network_address,
        timeout_seconds=config.g1.camera_frame_timeout_seconds,
        read_attempts=config.g1.camera_reconnect_attempts,
        retry_delay_seconds=config.g1.camera_reconnect_delay_seconds,
    )


def _run_g1_camera_diagnostic(source: CameraSource) -> None:
    import cv2

    source.open()
    previous = time.monotonic()
    fps = 0.0
    try:
        while True:
            frame = source.read()
            now = time.monotonic()
            elapsed = now - previous
            previous = now
            if elapsed > 0:
                current = 1.0 / elapsed
                fps = current if fps == 0 else 0.9 * fps + 0.1 * current
            cv2.putText(
                frame,
                f"G1 CAMERA  FPS: {fps:.1f}",
                (12, 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (0, 255, 0),
                2,
            )
            cv2.imshow("G1 Camera Diagnostic", frame)
            if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                break
    finally:
        source.close()
        cv2.destroyAllWindows()


def _run_motion_preview(adapter: MujocoRobotAdapter, motion: str) -> None:
    try:
        animation = adapter.library.animation(motion)
        print(f"Animation: {motion}", flush=True)
        adapter.play_motion(motion)
        if not adapter.wait_for_idle(animation.duration_seconds + 5.0):
            raise RuntimeError(f"Motion preview timed out: {motion}")
        # Leave the returned stand pose visible briefly before closing.
        time.sleep(1.0)
    finally:
        adapter.close()


def _aivis_backend(config, *, debug: bool = False):
    from g1_bottle_reaction.adapters.aivis_speech import AivisSpeechBackend

    return AivisSpeechBackend(config.speech.aivis, debug=debug)


def _list_aivis_speakers(config) -> None:
    from g1_bottle_reaction.adapters.aivis_speech import format_speakers

    backend = _aivis_backend(config)
    print("AivisSpeech Engine: connected")
    print(format_speakers(backend.speakers))


def _check_aivis(config) -> None:
    backend = _aivis_backend(config)
    style_count = sum(len(speaker.styles) for speaker in backend.speakers)
    print("AivisSpeech Engine")
    print(f"URL: {config.speech.aivis.base_url}")
    print("Status: OK")
    print(f"Speakers: {len(backend.speakers)}")
    print(f"Styles: {style_count}")


def _precache_speech(config, *, debug: bool) -> None:
    backend = _aivis_backend(config, debug=debug)
    requests: list[tuple[str, str]] = []
    for reaction in config.reaction.items.values():
        requests.append((reaction.speech, reaction.voice_profile))
        requests.extend(
            (variant.speech, reaction.voice_profile)
            for variant in reaction.encounter_variants
        )
    seen: set[tuple[str, str]] = set()
    for text, profile in requests:
        if not text:
            continue
        if (text, profile) in seen:
            continue
        seen.add((text, profile))
        path, hit = backend.precache(text, voice_profile=profile)
        print(
            f"[SPEECH CACHE] {'hit' if hit else 'generated'} "
            f"profile={profile} text={text} path={path}"
        )


def _preview_speech(config, text: str, profile: str, *, debug: bool) -> None:
    backend = _aivis_backend(config, debug=debug)
    backend.speak(text, voice_profile=profile)


def _preview_voice_profiles(config, text: str, *, debug: bool) -> None:
    backend = _aivis_backend(config, debug=debug)
    for profile in config.speech.aivis.voice_profiles:
        print(f"[VOICE PROFILE] {profile}")
        backend.speak(text, voice_profile=profile)


def _configure_utf8_console() -> None:
    """Keep Japanese reaction text readable in redirected Windows terminals."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
