"""YOLO confirmation gates and adapters for the shared Reaction Engine."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
import threading
import wave

import yaml

from g1_bottle_reaction.adapters.robot import RobotAdapter
from g1_bottle_reaction.adapters.speech import SpeechBackend
from g1_bottle_reaction.config.loader import ReactionConfig
from g1_bottle_reaction.reactions.engine import ReactionEngine, ReactionJob
from g1_bottle_reaction.reactions.models import Reaction
from g1_bottle_reaction.state.events import ReactionEvent


@dataclass(frozen=True)
class FoundSettings:
    confidence: float
    duration: float
    grace: float
    cooldown: float
    sounds: tuple[Path, ...]
    output: str
    rearm_absence: float


FOUND_REACTION_EVENTS = {
    "person": ReactionEvent.YOLO_PERSON_FOUND,
    "banana": ReactionEvent.YOLO_BANANA_FOUND,
    "plushie": ReactionEvent.YOLO_PLUSHIE_FOUND,
}


class ConsoleWavOutput:
    """Hardware-free audio output for the dual-camera mock mode."""

    def play_wav(self, path: Path) -> None:
        print(f"[MOCK AUDIO] {path}", flush=True)

    def close(self) -> None:
        pass


class FoundWavSpeechBackend(SpeechBackend):
    """Adapt existing reaction WAV files to ReactionEngine's speech boundary."""

    def __init__(self, output) -> None:
        self.output = output
        self.error = ""

    def speak(self, text: str, *, voice_profile: str = "neutral") -> None:
        del voice_profile
        path = Path(text)
        print(f"Audio triggered: {path}", flush=True)
        try:
            self.output.play_wav(path)
        except Exception as exc:
            self.error = str(exc)
            print(
                f"FOUND AUDIO ERROR: {exc}; camera/YOLO continue (audio disabled)",
                flush=True,
            )

    def close(self) -> None:
        close = getattr(self.output, "close", None)
        if close:
            close()


class FailSafeReactionRobotAdapter(RobotAdapter):
    """Log one named reaction and permanently inhibit motion after an exception."""

    def __init__(self, delegate: RobotAdapter) -> None:
        self.delegate = delegate
        self.error = ""

    def play_motion(self, motion: str) -> None:
        if self.error:
            print(
                f"Motion reaction skipped: {motion} (disabled after previous error)",
                flush=True,
            )
            return
        print(f"Motion reaction triggered: {motion}", flush=True)
        try:
            self.delegate.play_motion(motion)
            if getattr(self.delegate, "last_motion_timed_out", False):
                raise RuntimeError(
                    "Unitree safe Action returned RPC timeout 3104"
                )
        except Exception as exc:
            self.error = str(exc)
            print(
                f"MOTION REACTION ERROR: {exc}; camera/YOLO continue "
                "(motion disabled, no automatic retry)",
                flush=True,
            )

    def request_shutdown(self) -> None:
        self.delegate.request_shutdown()

    def close(self) -> None:
        self.delegate.close()


class FoundReactionController:
    """Submit confirmed YOLO events to the repository's shared ReactionEngine."""

    def __init__(
        self,
        settings: dict[str, FoundSettings],
        output,
        robot: RobotAdapter,
        *,
        base_reaction: Reaction,
        cooldown_seconds: float,
    ) -> None:
        if base_reaction.motion != "notice":
            raise ValueError("Dual-camera G1 reaction is restricted to existing 'notice'")
        items = {
            FOUND_REACTION_EVENTS[name].value: replace(
                base_reaction,
                name=FOUND_REACTION_EVENTS[name].value,
                speech=str(value.sounds[0]),
                encounter_variants=(),
                bypass_cooldown=False,
            )
            for name, value in settings.items()
        }
        self.speech = FoundWavSpeechBackend(output)
        self.robot = FailSafeReactionRobotAdapter(robot)
        self.engine = ReactionEngine(
            ReactionConfig(cooldown_seconds=cooldown_seconds, items=items),
            self.robot,
            self.speech,
        )
        self._jobs: list[ReactionJob] = []

    @property
    def busy(self) -> bool:
        self._jobs = [job for job in self._jobs if not job.wait(0)]
        return bool(self._jobs)

    @property
    def audio_error(self) -> str:
        return self.speech.error

    @property
    def motion_error(self) -> str:
        return self.robot.error

    def trigger(self, name: str, now: float) -> bool:
        if name not in FOUND_REACTION_EVENTS:
            raise ValueError(f"Unknown found reaction target: {name}")
        decision = self.engine.handle(
            FOUND_REACTION_EVENTS[name], encounter_count=1, now=now
        )
        if decision.accepted and decision.job is not None:
            self._jobs.append(decision.job)
            print(
                f"{name.upper()} REACTION EVENT: audio + "
                f"motion={decision.reaction.motion}",
                flush=True,
            )
        return decision.accepted

    def close(self) -> None:
        try:
            self.engine.close(wait=True, cancel_pending=True)
        finally:
            self.speech.close()


def load_settings(args, root):
    with (root / "config/person_found_audio.yaml").open(encoding="utf-8") as stream:
        defaults = yaml.safe_load(stream)
    def pick(name, key):
        value = getattr(args, name, None)
        return defaults[key] if value is None else value
    confidence = args.yolo_confidence
    duration = float(pick("found_duration", "found_duration"))
    grace = float(pick("detection_grace", "dropout_grace"))
    cooldown = float(pick("audio_cooldown", "audio_cooldown"))
    absence = float(pick("rearm_absence", "rearm_absence"))
    if not all(math.isfinite(v) and v > 0 for v in (duration, grace, cooldown, absence)):
        raise ValueError("found duration, dropout grace and audio cooldown must be finite and positive")
    path = pick("found_sound", "sound")
    if not path:
        raise ValueError("No default reaction sound selected; use --found-sound /absolute/path.wav")
    path = Path(path).expanduser()
    paths = ((path if path.is_absolute() else root / path).resolve(),)
    for path in paths:
        validate_sound(path)
    return FoundSettings(confidence, duration, grace, cooldown, paths, pick("found_output", "output"), absence)


def load_banana_settings(root, confidence, output):
    with (root / "config/yolo_objects.yaml").open(encoding="utf-8") as stream:
        values = yaml.safe_load(stream)
    duration = float(values["banana_found_duration"])
    grace = float(values["banana_dropout_grace"])
    cooldown = float(values["banana_audio_cooldown"])
    absence = float(values["banana_rearm_absence"])
    if not all(math.isfinite(v) and v > 0 for v in (duration, grace, cooldown, absence)):
        raise ValueError("banana audio timing values must be finite and positive")
    path = Path(values["banana_sound"])
    path = (path if path.is_absolute() else root / path).resolve()
    validate_sound(path)
    return FoundSettings(confidence, duration, grace, cooldown, (path,), output, absence)


def load_plushie_settings(root, confidence, output):
    with (root / "config/yolo_objects.yaml").open(encoding="utf-8") as stream:
        values = yaml.safe_load(stream)
    duration = float(values["plushie_found_duration"])
    grace = float(values["plushie_dropout_grace"])
    cooldown = float(values["plushie_audio_cooldown"])
    absence = float(values["plushie_rearm_absence"])
    if not all(math.isfinite(v) and v > 0 for v in (duration, grace, cooldown, absence)):
        raise ValueError("plushie audio timing values must be finite and positive")
    path = Path(values["plushie_sound"])
    path = (path if path.is_absolute() else root / path).resolve()
    validate_sound(path)
    return FoundSettings(confidence, duration, grace, cooldown, (path,), output, absence)


def validate_sound(path):
    try:
        with wave.open(str(path), "rb") as stream:
            if stream.getcomptype() != "NONE" or stream.getnframes() <= 0:
                raise ValueError("Expected a nonempty PCM WAV")
            return (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()*8,
                    stream.getnframes()/stream.getframerate())
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError(f"Cannot play existing WAV {path}: {exc}") from exc


def select_audio_trigger(result, now, person_gate, banana_gate, plushie_gate=None, *, audio_busy=False):
    """Choose at most one reaction in person, banana, plushie priority order."""
    if person_gate.update(result, now, audio_busy=audio_busy):
        banana_gate.update(result, now, audio_busy=True, inhibit=True)
        if plushie_gate is not None:
            plushie_gate.update(result, now, audio_busy=True, inhibit=True)
        return "person"
    if banana_gate.update(result, now, audio_busy=audio_busy,
                          inhibit=bool(result.people)):
        if plushie_gate is not None:
            plushie_gate.update(result, now, audio_busy=True, inhibit=True)
        return "banana"
    if plushie_gate is not None and plushie_gate.update(
            result, now, audio_busy=audio_busy, inhibit=bool(result.people or result.bananas)):
        return "plushie"
    return None


class FoundGate:
    """Fresh source timestamps only: replaying one YOLO result cannot trigger."""
    def __init__(self, duration=.3, grace=.15, cooldown=2., confidence=.25,
                 rearm_absence=1., object_attribute="people"):
        self.duration, self.grace, self.cooldown = duration, grace, cooldown
        self.confidence = confidence
        self.rearm_absence = rearm_absence
        self.object_attribute = object_attribute
        self.waiting_clear = False
        self.clear_start = self.clear_last = None
        self.start = self.last = None
        self.last_sample = -math.inf
        self.eligible_after = -math.inf
        self.until = -math.inf
        self.state = "SEARCHING"

    def clear_detection(self):
        self.start = self.last = None
        self.state = "SEARCHING"

    def update(self, result, now, *, audio_busy=False, inhibit=False):
        if now < self.until:
            self.state = "COOLDOWN"
            return False
        if self.state == "COOLDOWN":
            self.clear_detection()
        if result.status != "RUNNING":
            self.clear_detection()
            self.clear_start = self.clear_last = None
            if self.waiting_clear:
                self.state = "FOUND"
            return False
        if self.waiting_clear:
            self.state = "FOUND"
            if self.clear_last is not None and now - self.clear_last > self.grace:
                self.clear_start = self.clear_last = None
        if self.last is not None and now - self.last > self.grace:
            self.clear_detection()
        stamp = result.stamp
        if not math.isfinite(stamp) or stamp > now or stamp <= self.last_sample:
            return False
        self.last_sample = stamp
        if stamp <= self.eligible_after or now - stamp > self.grace:
            return False
        positive = any(item.confidence >= self.confidence
                       for item in getattr(result, self.object_attribute))
        if self.waiting_clear:
            if positive:
                self.clear_start = self.clear_last = None
            else:
                if self.clear_start is None:
                    self.clear_start = stamp
                self.clear_last = stamp
                if stamp - self.clear_start + 1e-9 >= self.rearm_absence:
                    self.waiting_clear = False
                    self.clear_start = self.clear_last = None
                    self.eligible_after = stamp
                    self.clear_detection()
            return False
        if not positive:
            return False  # Brief negative results do not reset the positive interval.
        if self.start is None:
            self.start = stamp
        self.last = stamp
        self.state = "DETECTING"
        if stamp - self.start + 1e-9 < self.duration or audio_busy or inhibit:
            return False
        self.until = now + self.cooldown
        self.eligible_after = self.until
        self.start = self.last = None
        self.state = "COOLDOWN"
        self.waiting_clear = True
        return True

    def label(self, now):
        if self.state == "COOLDOWN":
            return f"COOLDOWN {max(0, self.until-now):.1f} sec"
        if self.state == "DETECTING":
            elapsed = min(self.duration, max(0, self.last-self.start))
            return f"DETECTING {elapsed:.2f} / {self.duration:.2f} sec"
        if self.waiting_clear:
            if self.clear_start is not None:
                return f"REARMING {self.clear_last-self.clear_start:.1f} / {self.rearm_absence:.1f} sec"
            return "FOUND - WAITING FOR CLEAR"
        return "SEARCHING"

    def compact_label(self, now):
        if self.state == "COOLDOWN":
            return f"COOL {max(0, self.until-now):.1f}s"
        if self.state == "DETECTING":
            return f"DETECT {min(self.duration, max(0, self.last-self.start)):.1f}/{self.duration:.1f}s"
        if self.waiting_clear:
            if self.clear_start is not None:
                return f"REARM {self.clear_last-self.clear_start:.1f}/{self.rearm_absence:.1f}s"
            return "WAIT CLEAR"
        return "SEARCH"


class SingleAudioWorker:
    """One persistent thread, no playback backlog, never overlapping sounds."""
    def __init__(self, output, sounds):
        self.output, self.sounds = output, tuple(sounds)
        if not self.sounds:
            raise ValueError("At least one existing sound is required")
        self.condition = threading.Condition()
        self.busy = False
        self.pending = False
        self.pending_sounds = ()
        self.stopping = False
        self.error = ""
        self.thread = threading.Thread(target=self._run, name="found-audio", daemon=True)
        self.thread.start()

    def submit(self, sounds=None):
        with self.condition:
            if self.busy or self.stopping or self.error:
                return False
            selected = self.sounds if sounds is None else tuple(sounds)
            if not selected:
                return False
            self.pending_sounds = selected
            self.busy = self.pending = True
            self.condition.notify()
            return True

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending or self.stopping)
                if self.stopping:
                    return
                self.pending = False
                sounds, self.pending_sounds = self.pending_sounds, ()
            try:
                sound = sounds[0]
                print(f"FOUND AUDIO: {sound}", flush=True)
                self.output.play_wav(sound)
            except Exception as exc:
                self.error = str(exc)
                print(f"FOUND AUDIO ERROR: {exc}; camera/YOLO continue (audio disabled)", flush=True)
            finally:
                with self.condition:
                    self.busy = False

    def close(self):
        with self.condition:
            self.stopping = True
            self.pending = False
            self.condition.notify()
        close = getattr(self.output, "close", None)
        if close:
            close()
        self.thread.join(timeout=2)


def draw_found_status(panel, text):
    import cv2
    cv2.rectangle(panel, (0, 76), (panel.shape[1], 101), (20, 20, 20), -1)
    cv2.putText(panel, text, (12, 96), cv2.FONT_HERSHEY_SIMPLEX, .6, (80, 255, 180), 2)
