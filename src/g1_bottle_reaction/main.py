from __future__ import annotations

import argparse
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
from g1_bottle_reaction.adapters.g1_robot import G1RobotAdapter
from g1_bottle_reaction.adapters.mock_robot import MockRobotAdapter
from g1_bottle_reaction.adapters.mujoco_robot import (
    MujocoRobotAdapter,
    default_g1_model_path,
)
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
from g1_bottle_reaction.simulation.motion import (
    default_motion_config_path,
    load_motion_library,
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
    parser.add_argument("--camera", type=int, default=0)
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
    parser.add_argument("--enable-real-robot", action="store_true")
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


def _create_robot(args: argparse.Namespace) -> RobotAdapter:
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
        args.network_interface or "", enabled=args.enable_real_robot
    )
    adapter.initialize()
    return adapter


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
        robot = _create_robot(args)
        if args.preview_motion is not None:
            if not isinstance(robot, MujocoRobotAdapter):
                robot.close()
                raise ValueError("--preview-motion requires --robot mujoco")
            _run_motion_preview(robot, args.preview_motion)
            return 0
        if args.preview_tracking:
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
            print("MuJoCo viewer is ready; close the viewer or press Ctrl+C to stop")
            try:
                robot.wait_until_viewer_closed()
            finally:
                robot.close()
            return 0
        try:
            speech = create_speech_backend(
                args.speech,
                aivis_config=config.speech.aivis,
                debug=args.speech_debug,
            )
        except Exception:
            robot.close()
            raise
        stealth_mode = args.game == "stealth-phone" or args.simulate_stealth
        app = (
            StealthGameApp(config, robot, speech)
            if stealth_mode
            else BottleReactionApp(config, robot, speech)
        )
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
                    camera=args.camera,
                    headless=args.headless,
                    audio_monitor=audio_monitor,
                    audio_source_label=args.audio_source,
                    audio_debug=args.audio_debug,
                    tracking_debug=args.tracking_debug,
                )
            else:
                run_webcam(
                    app,
                    camera=args.camera,
                    headless=args.headless,
                    audio_monitor=audio_monitor,
                    audio_source_label=args.audio_source,
                    audio_debug=args.audio_debug,
                )
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


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
