from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Iterable

import numpy as np

from g1_bottle_reaction.audio.classifiers import FakeAudioClassifier
from g1_bottle_reaction.audio.debug import format_audio_debug
from g1_bottle_reaction.audio.models import Prediction
from g1_bottle_reaction.audio.music_tracker import AudioTrackingUpdate, MusicStateTracker
from g1_bottle_reaction.audio.pipeline import AudioMonitor
from g1_bottle_reaction.adapters.robot import RobotAdapter, TrackingCommand
from g1_bottle_reaction.adapters.speech import SpeechBackend
from g1_bottle_reaction.config.loader import AppConfig, StealthTrackingConfig
from g1_bottle_reaction.event_log import JsonlEventLogger
from g1_bottle_reaction.reactions.engine import ReactionEngine
from g1_bottle_reaction.state.bottle_tracker import BottleTracker, TrackingUpdate
from g1_bottle_reaction.state.events import ReactionEvent
from g1_bottle_reaction.stealth.engine import StealthGameEngine
from g1_bottle_reaction.stealth.models import (
    GameEvent,
    GameState,
    GameUpdate,
    TargetObservation,
    TargetRole,
)
from g1_bottle_reaction.stealth.target_perception import (
    TargetPerception,
    YoloTargetDetector,
)
from g1_bottle_reaction.stealth.tracking import TargetTrackingController


@dataclass(frozen=True, slots=True)
class SimulationSample:
    now: float
    detected: bool
    confidence: float = 0.0
    proximity_ratio: float = 0.0


DEFAULT_SIMULATION: tuple[SimulationSample, ...] = (
    SimulationSample(0.0, False),
    SimulationSample(0.1, True, 0.88, 0.02),
    SimulationSample(0.5, True, 0.88, 0.02),
    SimulationSample(2.6, True, 0.90, 0.06),
    SimulationSample(4.8, True, 0.92, 0.16),
    SimulationSample(7.0, True, 0.91, 0.10),
    SimulationSample(9.2, False),
    SimulationSample(10.3, False),
    SimulationSample(12.5, True, 0.87, 0.02),
    SimulationSample(12.9, True, 0.87, 0.02),
)


class BottleReactionApp:
    def __init__(
        self,
        config: AppConfig,
        robot: RobotAdapter,
        speech: SpeechBackend,
    ) -> None:
        self.config = config
        self.robot = robot
        self.tracker = BottleTracker(config.tracking)
        self.engine = ReactionEngine(config.reaction, robot, speech)
        self.event_logger = JsonlEventLogger(config.event_log)
        self.last_update: TrackingUpdate | None = None
        self.last_event = "-"
        self.last_reaction = "-"
        self.last_speech = "-"
        self.last_audio_update: AudioTrackingUpdate | None = None
        self.last_audio_event = "-"

    def process(
        self,
        *,
        detected: bool,
        confidence: float,
        proximity_ratio: float,
        now: float,
    ) -> TrackingUpdate:
        update = self.tracker.update(
            detected=detected,
            confidence=confidence,
            proximity_ratio=proximity_ratio,
            now=now,
        )
        self.last_update = update
        if update.event is not None:
            decision = self._dispatch_reaction(
                update.event, encounter_count=update.encounter_count, now=now
            )
            self.last_event = update.event.value
            reaction = decision.reaction
            self.event_logger.write(update, reaction if decision.accepted else None)
        return update

    def process_audio(self, update: AudioTrackingUpdate, *, now: float) -> None:
        self.last_audio_update = update
        if update.event is None:
            return
        self.last_audio_event = update.event.value
        decision = None
        if update.event is ReactionEvent.MUSIC_STARTED and self._allow_audio_reaction():
            decision = self._dispatch_reaction(update.event, encounter_count=1, now=now)
            print(
                f"[AUDIO EVENT] {update.event.value} "
                f"music_score={update.music_score:.3f}",
                flush=True,
            )
        reaction = decision.reaction if decision is not None and decision.accepted else None
        self.event_logger.write_audio(
            event=update.event,
            music_score=update.music_score,
            music_state=update.state.value,
            top_labels=[
                {"label": item.label, "score": item.score}
                for item in update.top_predictions
            ],
            best_music_label=update.best_music_label,
            rms=update.rms,
            peak_amplitude=update.peak_amplitude,
            sample_rate=update.sample_rate,
            buffer_duration_seconds=update.buffer_duration_seconds,
            reaction=reaction,
        )

    def _allow_audio_reaction(self) -> bool:
        return True

    def _dispatch_reaction(
        self, event: ReactionEvent, *, encounter_count: int, now: float
    ):
        decision = self.engine.handle(
            event, encounter_count=encounter_count, now=now
        )
        reaction = decision.reaction
        if reaction is not None:
            self.last_reaction = (
                reaction.motion if decision.accepted else f"{reaction.motion} (cooldown)"
            )
            self.last_speech = reaction.speech
        return decision

    def close(self) -> None:
        self.engine.close(wait=True)


class StealthGameApp(BottleReactionApp):
    """Stealth orchestration using the existing shared Reaction Engine."""

    def __init__(
        self,
        config: AppConfig,
        robot: RobotAdapter,
        speech: SpeechBackend,
    ) -> None:
        super().__init__(config, robot, speech)
        self.game = StealthGameEngine(config.stealth_game)
        self.target_tracking = TargetTrackingController(
            config.stealth_game.tracking, robot
        )
        self.last_game_update: GameUpdate | None = None
        self.last_game_event = "-"
        self.tracking_yaw_radians = 0.0
        self.tracking_command: TrackingCommand = self.target_tracking.last_command

    def process_target(
        self, observation: TargetObservation, *, now: float
    ) -> GameUpdate:
        update = self.game.update(observation, now=now)
        self.last_game_update = update
        self.tracking_command = self.target_tracking.update(
            observation,
            state=update.state,
            now=now,
        )
        self.tracking_yaw_radians = self.tracking_command.actual_yaw_rad
        for event in update.events:
            self.last_game_event = event.value
            decision = None
            if event is not GameEvent.GAME_OVER:
                decision = self._dispatch_reaction(
                    ReactionEvent(event.value), encounter_count=1, now=now
                )
            reaction = (
                decision.reaction
                if decision is not None and decision.accepted
                else None
            )
            self.event_logger.write_game(
                update=update,
                event=event,
                tracking_yaw_radians=self.tracking_yaw_radians,
                reaction=reaction,
            )
        return update

    def reset_game(self, *, now: float | None = None) -> None:
        self.game.reset(now=now)
        self.tracking_command = self.target_tracking.reset(now=now)
        self.last_game_update = None
        self.last_game_event = "-"
        self.tracking_yaw_radians = self.tracking_command.actual_yaw_rad
        print("[STEALTH] round reset", flush=True)

    def _allow_audio_reaction(self) -> bool:
        return self.game.state is GameState.UNAWARE


def run_simulation(
    app: BottleReactionApp,
    samples: Iterable[SimulationSample] = DEFAULT_SIMULATION,
    *,
    realtime_scale: float = 0.0,
) -> list[TrackingUpdate]:
    print("[SIMULATION] start", flush=True)
    outputs: list[TrackingUpdate] = []
    previous_time: float | None = None
    for sample in samples:
        if realtime_scale > 0 and previous_time is not None:
            time.sleep(max(0.0, sample.now - previous_time) * realtime_scale)
        update = app.process(
            detected=sample.detected,
            confidence=sample.confidence,
            proximity_ratio=sample.proximity_ratio,
            now=sample.now,
        )
        outputs.append(update)
        if update.event is not None:
            print(
                f"[EVENT] {update.event.value} state={update.state.value} "
                f"proximity_ratio={update.proximity_ratio:.3f} "
                f"encounter_count={update.encounter_count}",
                flush=True,
            )
        previous_time = sample.now
    app.close()
    print("[SIMULATION] complete", flush=True)
    return outputs


@dataclass(frozen=True, slots=True)
class AudioScoreSample:
    now: float
    music_score: float


DEFAULT_AUDIO_SIMULATION: tuple[AudioScoreSample, ...] = (
    AudioScoreSample(0.0, 0.05),
    AudioScoreSample(0.5, 0.10),
    AudioScoreSample(1.0, 0.65),
    AudioScoreSample(1.5, 0.72),
    AudioScoreSample(2.0, 0.80),
    AudioScoreSample(2.5, 0.75),
    AudioScoreSample(3.0, 0.20),
    AudioScoreSample(5.1, 0.10),
    AudioScoreSample(13.2, 0.70),
    AudioScoreSample(14.3, 0.75),
)


def run_audio_simulation(
    app: BottleReactionApp,
    samples: Iterable[AudioScoreSample] = DEFAULT_AUDIO_SIMULATION,
) -> list[AudioTrackingUpdate]:
    print("[AUDIO SIMULATION] start", flush=True)
    sample_list = tuple(samples)
    classifier = FakeAudioClassifier(item.music_score for item in sample_list)
    tracker = MusicStateTracker(app.config.audio.music_tracking)
    outputs: list[AudioTrackingUpdate] = []
    for sample in sample_list:
        result = classifier.classify(
            # Fake classifier intentionally ignores waveform contents.
            np.zeros(1, dtype=np.float32),
            sample_rate=app.config.audio.target_sample_rate,
        )
        update = tracker.update(
            result.music_score,
            now=sample.now,
            top_predictions=result.top_predictions,
            best_music_label=result.best_music_label,
        )
        outputs.append(update)
        app.process_audio(update, now=sample.now)
        print(
            f"[AUDIO] score={update.music_score:.2f} state={update.state.value} "
            f"event={update.event.value if update.event else '-'}",
            flush=True,
        )
    app.close()
    print("[AUDIO SIMULATION] complete", flush=True)
    return outputs


@dataclass(frozen=True, slots=True)
class StealthSimulationSample:
    now: float
    visible: bool
    confidence: float = 0.0
    center_x: float = 0.0
    area_ratio: float = 0.0


DEFAULT_STEALTH_SIMULATION: tuple[StealthSimulationSample, ...] = (
    StealthSimulationSample(0.0, False),
    StealthSimulationSample(0.1, True, 0.85, -0.8, 0.08),
    StealthSimulationSample(0.3, True, 0.85, -0.8, 0.08),
    StealthSimulationSample(0.5, True, 0.85, -0.8, 0.08),
    StealthSimulationSample(0.6, False),
    StealthSimulationSample(1.0, False),
    StealthSimulationSample(1.4, False),
    StealthSimulationSample(1.5, True, 0.95, -0.8, 0.12),
    StealthSimulationSample(1.7, True, 0.95, -0.8, 0.12),
    StealthSimulationSample(1.9, True, 0.95, -0.4, 0.12),
    StealthSimulationSample(2.1, True, 0.95, 0.0, 0.12),
    StealthSimulationSample(2.3, True, 0.95, 0.4, 0.12),
    StealthSimulationSample(2.5, True, 0.95, 0.8, 0.12),
    StealthSimulationSample(2.7, True, 0.95, 0.4, 0.12),
    StealthSimulationSample(2.9, True, 0.95, 0.0, 0.12),
    StealthSimulationSample(3.1, True, 0.95, -0.4, 0.12),
    StealthSimulationSample(3.3, True, 0.95, -0.8, 0.12),
    StealthSimulationSample(3.5, True, 0.95, -0.4, 0.12),
    StealthSimulationSample(3.7, True, 0.95, 0.0, 0.12),
    StealthSimulationSample(3.9, True, 0.95, 0.4, 0.12),
    StealthSimulationSample(4.1, True, 0.95, 0.8, 0.12),
    StealthSimulationSample(4.9, True, 0.95, 0.8, 0.12),
    StealthSimulationSample(5.7, True, 0.95, 0.8, 0.12),
)


def run_stealth_simulation(
    app: StealthGameApp,
    samples: Iterable[StealthSimulationSample] = DEFAULT_STEALTH_SIMULATION,
    *,
    realtime_scale: float = 0.0,
) -> list[GameUpdate]:
    print("[STEALTH SIMULATION] start", flush=True)
    outputs: list[GameUpdate] = []
    previous_time: float | None = None
    target_config = app.config.stealth_game.target
    for sample in samples:
        if realtime_scale > 0 and previous_time is not None:
            time.sleep(max(0.0, sample.now - previous_time) * realtime_scale)
        observation = _simulation_observation(sample, target_config.detector_label)
        update = app.process_target(observation, now=sample.now)
        outputs.append(update)
        print(
            f"[STEALTH] t={sample.now:.1f} state={update.state.value} "
            f"suspicion={update.suspicion:.1f} x={sample.center_x:+.1f} "
            f"desired={app.tracking_command.desired_yaw_rad:+.3f} "
            f"actual={app.tracking_command.actual_yaw_rad:+.3f} "
            f"tracking={app.tracking_command.status} "
            f"events={','.join(event.value for event in update.events) or '-'}",
            flush=True,
        )
        previous_time = sample.now
    app.close()
    print("[STEALTH SIMULATION] complete", flush=True)
    return outputs


def _simulation_observation(
    sample: StealthSimulationSample, raw_label: str
) -> TargetObservation:
    if not sample.visible:
        return TargetObservation.missing(
            timestamp=sample.now,
            raw_detector_label=raw_label,
        )
    return TargetObservation(
        visible=True,
        confidence=sample.confidence,
        bbox=(0, 0, 1, 1),
        center_x_normalized=sample.center_x,
        center_y_normalized=0.0,
        bbox_area_ratio=sample.area_ratio,
        timestamp=sample.now,
        raw_detector_label=raw_label,
        semantic_role=TargetRole.PLAYER,
    )


def run_tracking_preview(
    robot: RobotAdapter,
    config: StealthTrackingConfig,
    *,
    realtime: bool = True,
) -> list[TrackingCommand]:
    """Camera-free left/center/right tracking preview for controller tuning."""

    print("[TRACKING PREVIEW] start", flush=True)
    controller = TargetTrackingController(config, robot)
    commands: list[TrackingCommand] = []
    now = 0.0
    interval = 1.0 / 50.0
    try:
        for center_x in (-0.8, -0.4, 0.0, 0.4, 0.8, 0.0):
            for _ in range(50):
                observation = TargetObservation(
                    visible=True,
                    confidence=0.95,
                    bbox=(0, 0, 1, 1),
                    center_x_normalized=center_x,
                    center_y_normalized=0.0,
                    bbox_area_ratio=0.1,
                    timestamp=now,
                    raw_detector_label="simulated",
                    semantic_role=TargetRole.PLAYER,
                )
                command = controller.update(
                    observation, state=GameState.ALERT, now=now
                )
                commands.append(command)
                if realtime:
                    time.sleep(interval)
                now += interval
            print(
                f"[TRACKING] x={center_x:+.1f} "
                f"desired={np.degrees(command.desired_yaw_rad):+.1f}deg "
                f"actual={np.degrees(command.actual_yaw_rad):+.1f}deg",
                flush=True,
            )
        return commands
    finally:
        controller.reset(now=now, immediate=True)
        robot.close()
        print("[TRACKING PREVIEW] complete", flush=True)


def run_stealth_webcam(
    app: StealthGameApp,
    *,
    camera: int,
    headless: bool = False,
    audio_monitor: AudioMonitor | None = None,
    audio_source_label: str = "none",
    audio_debug: bool = False,
    tracking_debug: bool = False,
) -> None:
    import cv2

    target_config = app.config.stealth_game.target
    perception = TargetPerception(
        YoloTargetDetector(
            app.config.vision.model,
            target_config.detector_label,
            target_config.confidence_threshold,
        ),
        detector_label=target_config.detector_label,
        semantic_role=target_config.semantic_role,
    )
    capture = cv2.VideoCapture(camera)
    if not capture.isOpened():
        app.close()
        raise RuntimeError(f"Could not open camera {camera}")

    previous_frame_at = time.monotonic()
    fps = 0.0
    last_printed_audio: AudioTrackingUpdate | None = None
    try:
        if audio_monitor is not None:
            audio_monitor.start()
        while True:
            if audio_monitor is not None and audio_monitor.error is not None:
                raise RuntimeError(f"Audio worker failed: {audio_monitor.error}")
            if (
                audio_debug
                and audio_monitor is not None
                and audio_monitor.last_update is not None
                and audio_monitor.last_update is not last_printed_audio
            ):
                print(format_audio_debug(audio_monitor.last_update), flush=True)
                last_printed_audio = audio_monitor.last_update
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError("Webcam frame capture failed")
            now = time.monotonic()
            elapsed = now - previous_frame_at
            if elapsed > 0:
                instantaneous_fps = 1.0 / elapsed
                fps = instantaneous_fps if fps == 0 else 0.9 * fps + 0.1 * instantaneous_fps
            previous_frame_at = now

            observation = perception.observe(frame, timestamp=now)
            update = app.process_target(observation, now=now)
            if observation.visible and observation.bbox is not None:
                x1, y1, x2, y2 = observation.bbox
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 220, 255), 2)
                cv2.putText(
                    frame,
                    f"{observation.raw_detector_label} -> PLAYER {observation.confidence:.2f}",
                    (x1, max(20, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (0, 220, 255),
                    2,
                )
            if tracking_debug:
                height, width = frame.shape[:2]
                cv2.line(
                    frame,
                    (width // 2, 0),
                    (width // 2, height),
                    (160, 160, 160),
                    1,
                )
                if observation.visible and observation.bbox is not None:
                    x1, y1, x2, y2 = observation.bbox
                    center = ((x1 + x2) // 2, (y1 + y2) // 2)
                    cv2.circle(frame, center, 6, (0, 0, 255), -1)
                    cv2.line(
                        frame,
                        (width // 2, center[1]),
                        center,
                        (0, 0, 255),
                        2,
                    )
            _draw_stealth_overlay(
                cv2, frame, app, update, fps, tracking_debug=tracking_debug
            )
            if not headless:
                cv2.imshow("G1 Smartphone Stealth Game", frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")):
                    break
                if key in (ord("r"), ord("R")):
                    app.reset_game(now=now)
    finally:
        if audio_monitor is not None:
            audio_monitor.stop()
        capture.release()
        cv2.destroyAllWindows()
        app.close()


def _draw_stealth_overlay(
    cv2: object,
    frame: object,
    app: StealthGameApp,
    update: GameUpdate,
    fps: float,
    *,
    tracking_debug: bool,
) -> None:
    observation = update.observation
    raw_label = observation.raw_detector_label if observation is not None else "-"
    confidence = observation.confidence if observation is not None else 0.0
    center_x = observation.center_x_normalized if observation is not None else 0.0
    area_ratio = observation.bbox_area_ratio if observation is not None else 0.0
    gauge_filled = round(update.suspicion / 5.0)
    gauge = "[" + "#" * gauge_filled + "-" * (20 - gauge_filled) + "]"
    lines = [
        "TARGET: PLAYER",
        f"RAW DETECTION: {raw_label}",
        f"CONFIDENCE: {confidence:.2f}",
        f"PLAYER X: {center_x:+.2f}",
        f"BBOX AREA: {area_ratio:.3f}",
        f"VISIBILITY SCORE: {update.visibility_score:.2f}",
        f"SUSPICION: {update.suspicion:.1f} / 100",
        gauge,
        f"GAME STATE: {update.state.value}",
        f"LAST GAME EVENT: {app.last_game_event}",
        f"TRACKING: {app.tracking_command.status}",
        f"FPS: {fps:.1f}",
        "R: RESET   Q/ESC: QUIT",
    ]
    if tracking_debug:
        command = app.tracking_command
        last_seen = (
            "-"
            if command.last_seen_seconds is None
            else f"{command.last_seen_seconds:.2f} sec ago"
        )
        lines[10:10] = [
            f"DESIRED YAW: {np.degrees(command.desired_yaw_rad):+.1f} deg",
            f"ACTUAL YAW: {np.degrees(command.actual_yaw_rad):+.1f} deg",
            f"TRACK ERROR: {np.degrees(command.error_yaw_rad):+.1f} deg",
            f"LAST SEEN: {last_seen}",
        ]
    for index, line in enumerate(lines):
        y = 28 + index * 24
        cv2.putText(
            frame, line, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2
        )
        cv2.putText(
            frame, line, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 1
        )
    if update.state is GameState.GAME_OVER:
        height, width = frame.shape[:2]
        text = "GAME OVER"
        size = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, 1.8, 4)[0]
        origin = ((width - size[0]) // 2, (height + size[1]) // 2)
        cv2.putText(
            frame, text, origin, cv2.FONT_HERSHEY_DUPLEX, 1.8, (0, 0, 0), 8
        )
        cv2.putText(
            frame, text, origin, cv2.FONT_HERSHEY_DUPLEX, 1.8, (0, 0, 255), 4
        )


def run_webcam(
    app: BottleReactionApp,
    *,
    camera: int,
    headless: bool = False,
    audio_monitor: AudioMonitor | None = None,
    audio_source_label: str = "none",
    audio_debug: bool = False,
) -> None:
    import cv2

    from g1_bottle_reaction.vision.detector import YoloBottleDetector

    detector = YoloBottleDetector(
        app.config.vision.model, app.config.vision.confidence_threshold
    )
    capture = cv2.VideoCapture(camera)
    if not capture.isOpened():
        app.close()
        raise RuntimeError(f"Could not open camera {camera}")

    previous_frame_at = time.monotonic()
    fps = 0.0
    last_printed_audio: AudioTrackingUpdate | None = None
    try:
        if audio_monitor is not None:
            audio_monitor.start()
        while True:
            if audio_monitor is not None and audio_monitor.error is not None:
                raise RuntimeError(f"Audio worker failed: {audio_monitor.error}")
            if (
                audio_debug
                and audio_monitor is not None
                and audio_monitor.last_update is not None
                and audio_monitor.last_update is not last_printed_audio
            ):
                print(format_audio_debug(audio_monitor.last_update), flush=True)
                last_printed_audio = audio_monitor.last_update
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError("Webcam frame capture failed")
            now = time.monotonic()
            elapsed = now - previous_frame_at
            if elapsed > 0:
                instantaneous_fps = 1.0 / elapsed
                fps = instantaneous_fps if fps == 0 else 0.9 * fps + 0.1 * instantaneous_fps
            previous_frame_at = now

            detection = detector.detect(frame)
            update = app.process(
                detected=detection is not None,
                confidence=detection.confidence if detection else 0.0,
                proximity_ratio=detection.proximity_ratio if detection else 0.0,
                now=now,
            )

            if detection is not None:
                x1, y1, x2, y2 = detection.bounding_box
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 220, 0), 2)
                cv2.putText(
                    frame,
                    f"bottle {detection.confidence:.2f}",
                    (x1, max(20, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (0, 220, 0),
                    2,
                )
            _draw_debug_overlay(
                cv2, frame, app, update, fps, audio_source_label=audio_source_label
            )
            if not headless:
                cv2.imshow("G1 Bottle Reaction", frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")):
                    break
    finally:
        if audio_monitor is not None:
            audio_monitor.stop()
        capture.release()
        cv2.destroyAllWindows()
        app.close()


def run_audio_only(
    app: BottleReactionApp,
    audio_monitor: AudioMonitor,
    *,
    audio_source_label: str,
    audio_debug: bool,
) -> None:
    print(
        f"[AUDIO] listening source={audio_source_label}; press Ctrl+C to stop",
        flush=True,
    )
    try:
        audio_monitor.start()
    except Exception:
        app.close()
        raise
    last_printed_update: AudioTrackingUpdate | None = None
    try:
        while True:
            time.sleep(0.2)
            if audio_monitor.error is not None:
                raise RuntimeError(f"Audio worker failed: {audio_monitor.error}")
            update = audio_monitor.last_update
            if audio_debug and update is not None and update is not last_printed_update:
                print(format_audio_debug(update), flush=True)
                last_printed_update = update
    except KeyboardInterrupt:
        pass
    finally:
        audio_monitor.stop()
        app.close()


def _draw_debug_overlay(
    cv2: object,
    frame: object,
    app: BottleReactionApp,
    update: TrackingUpdate,
    fps: float,
    *,
    audio_source_label: str,
) -> None:
    lines = [
        f"proximity_ratio: {update.proximity_ratio:.4f}",
        f"state: {update.state.value}",
        f"last event: {app.last_event}",
        f"reaction: {app.last_reaction}",
        f"speech: {app.last_speech}",
        f"encounter count: {update.encounter_count}",
        f"FPS: {fps:.1f}",
    ]
    audio = app.last_audio_update
    if audio_source_label != "none":
        lines.extend(
            [
                f"AUDIO SOURCE: {audio_source_label}",
                f"AUDIO MODE: {audio.audio_mode}" if audio else "AUDIO MODE: -",
                f"DEVICE: {audio.device}" if audio else "DEVICE: -",
                f"MUSIC SCORE: {audio.music_score:.3f}" if audio else "MUSIC SCORE: -",
                f"BEST MUSIC: {audio.best_music_label}" if audio else "BEST MUSIC: -",
                f"MUSIC STATE: {audio.state.value}" if audio else "MUSIC STATE: -",
                f"AUDIO EVENT: {app.last_audio_event}",
                f"RMS / PEAK: {audio.rms:.4f} / {audio.peak_amplitude:.4f}"
                if audio else "RMS / PEAK: -",
                f"AUDIO: {audio.sample_rate} Hz / {audio.buffer_duration_seconds:.2f} s"
                if audio else "AUDIO: -",
            ]
        )
        if audio is not None:
            lines.extend(
                f"  {item.label}: {item.score:.2f}"
                for item in audio.top_predictions[:3]
            )
    for index, line in enumerate(lines):
        cv2.putText(
            frame,
            line,
            (12, 28 + index * 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            2,
        )
        cv2.putText(
            frame,
            line,
            (12, 28 + index * 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (20, 20, 20),
            1,
        )
