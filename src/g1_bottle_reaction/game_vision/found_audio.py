"""YOLO confirmation gates and adapters for the shared Reaction Engine."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
from pathlib import Path
import tempfile
import threading
import time
import wave

import numpy as np
import yaml

from g1_bottle_reaction.adapters.robot import RobotAdapter
from g1_bottle_reaction.adapters.motiondecode_reaction import (
    MotionDecodeSafeReturnMiss,
)
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
    absence_blocking_overlap: float = 0.1


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
        self.last_attempt_successful: bool | None = None

    def speak(self, text: str, *, voice_profile: str = "neutral") -> None:
        del voice_profile
        path = Path(text)
        print(
            f"Audio triggered: {path}; monotonic={time.monotonic():.6f}",
            flush=True,
        )
        self.last_attempt_successful = False
        try:
            self.output.play_wav(path)
            self.last_attempt_successful = True
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


class AttenuatedWavOutput:
    """Apply a temporary per-playback PCM gain without touching source WAVs."""

    def __init__(self, delegate, gain_db: float) -> None:
        if not math.isfinite(gain_db) or not -60.0 <= gain_db < 0.0:
            raise ValueError("quiet audio gain must be finite and in [-60, 0) dB")
        self.delegate = delegate
        self.gain_db = float(gain_db)
        self.linear_gain = 10.0 ** (self.gain_db / 20.0)
        self._temporary = tempfile.TemporaryDirectory(prefix="g1-quiet-audio-")
        self._cache: dict[Path, Path] = {}
        self._lock = threading.Lock()

    def play_wav(self, path: Path) -> None:
        source = Path(path).resolve()
        with self._lock:
            quiet = self._cache.get(source)
            if quiet is None:
                quiet = self._render(source)
                self._cache[source] = quiet
        self.delegate.play_wav(quiet)

    def _render(self, source: Path) -> Path:
        from g1_bottle_reaction.adapters.g1_audio import wav_to_pcm16_mono_16k

        pcm = np.frombuffer(wav_to_pcm16_mono_16k(source), dtype="<i2")
        attenuated = np.rint(pcm.astype(np.float64) * self.linear_gain)
        attenuated = np.clip(attenuated, -32768, 32767).astype("<i2")
        digest = hashlib.sha256(str(source).encode()).hexdigest()[:16]
        target = Path(self._temporary.name) / f"{digest}-{source.name}"
        with wave.open(str(target), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16_000)
            stream.writeframes(attenuated.tobytes())
        return target

    def close(self) -> None:
        try:
            close = getattr(self.delegate, "close", None)
            if close:
                close()
        finally:
            self._temporary.cleanup()


class FailSafeReactionRobotAdapter(RobotAdapter):
    """Log one named reaction and permanently inhibit motion after an exception."""

    def __init__(self, delegate: RobotAdapter) -> None:
        self.delegate = delegate
        self.error = ""
        self.last_attempt_successful: bool | None = None

    def play_motion(self, motion: str) -> None:
        self.last_attempt_successful = False
        if self.error:
            print(
                f"Motion reaction skipped: {motion} (disabled after previous error)",
                flush=True,
            )
            return
        print(
            f"Motion reaction triggered: {motion}; "
            f"monotonic={time.monotonic():.6f}",
            flush=True,
        )
        try:
            self.delegate.play_motion(motion)
            if getattr(self.delegate, "last_motion_timed_out", False):
                raise RuntimeError(
                    "Unitree safe Action returned RPC timeout 3104"
                )
            self.last_attempt_successful = True
        except MotionDecodeSafeReturnMiss as exc:
            print(
                "MOTION REACTION SAFE RETURN MISS: "
                f"reaction={exc.reaction} returned_to_q0=false "
                "motion disabled for this event only; "
                "next reaction remains enabled",
                flush=True,
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
        motion_overrides: dict[str, str] | None = None,
        speech_delay_overrides: dict[str, float] | None = None,
        wander=None,
        reaction_completion_timeout: float = 430.0,
    ) -> None:
        if base_reaction.motion != "notice":
            raise ValueError("Dual-camera G1 reaction is restricted to existing 'notice'")
        motion_overrides = motion_overrides or {}
        speech_delay_overrides = speech_delay_overrides or {}
        unknown = (set(motion_overrides) | set(speech_delay_overrides)) - set(settings)
        if unknown:
            raise ValueError(f"Unknown found reaction overrides: {sorted(unknown)}")
        if reaction_completion_timeout <= 0:
            raise ValueError("reaction completion timeout must be positive")
        items = {
            FOUND_REACTION_EVENTS[name].value: replace(
                base_reaction,
                name=FOUND_REACTION_EVENTS[name].value,
                motion=motion_overrides.get(name, base_reaction.motion),
                speech=str(value.sounds[0]),
                speech_delay_seconds=speech_delay_overrides.get(
                    name, base_reaction.speech_delay_seconds
                ),
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
        self.wander = wander
        self.reaction_completion_timeout = reaction_completion_timeout
        self._closing = threading.Event()
        self._wander_lock = threading.Lock()
        self._completion_thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        self._jobs = [job for job in self._jobs if not job.wait(0)]
        return bool(self._jobs) or bool(
            self._completion_thread and self._completion_thread.is_alive()
        )

    @property
    def audio_error(self) -> str:
        return self.speech.error

    @property
    def motion_error(self) -> str:
        return self.robot.error

    def trigger(self, name: str, now: float) -> bool:
        if name not in FOUND_REACTION_EVENTS:
            raise ValueError(f"Unknown found reaction target: {name}")
        # The camera loop drops ordinary events while a reaction owns the
        # worker.  Do not let callers accumulate jobs in ReactionEngine either.
        if self.busy:
            print(f"{name.upper()} REACTION DROP: reaction already running", flush=True)
            return False
        if self.wander is not None:
            try:
                with self._wander_lock:
                    self.wander.stop_and_wait()
            except Exception as exc:
                print(
                    f"{name.upper()} REACTION INHIBITED: Wander stop not confirmed: {exc}",
                    flush=True,
                )
                return False
        decision = self.engine.handle(
            FOUND_REACTION_EVENTS[name], encounter_count=1, now=now
        )
        if decision.accepted and decision.job is not None:
            self._jobs.append(decision.job)
            print(
                f"{name.upper()} REACTION EVENT: audio + "
                f"motion={decision.reaction.motion}; confirmed_monotonic={now:.6f}",
                flush=True,
            )
            if self.wander is not None:
                self._completion_thread = threading.Thread(
                    target=self._resume_after_completion,
                    args=(name, decision.job),
                    name="wander-reaction-completion",
                    daemon=True,
                )
                self._completion_thread.start()
        elif self.wander is not None:
            try:
                with self._wander_lock:
                    self.wander.start()
            except Exception as exc:
                print(f"WANDER RESUME FAILED after rejected reaction: {exc}", flush=True)
        return decision.accepted

    def _resume_after_completion(self, name: str, job: ReactionJob) -> None:
        if not job.wait(self.reaction_completion_timeout):
            print(
                f"{name.upper()} REACTION TIMEOUT: Wander remains stopped",
                flush=True,
            )
            return
        successful = (
            job.successful
            and self.robot.last_attempt_successful is True
            and self.speech.last_attempt_successful is True
        )
        if not successful:
            print(
                f"{name.upper()} REACTION FAILED/UNCERTAIN: Wander remains stopped",
                flush=True,
            )
            return
        if self._closing.is_set():
            return
        try:
            with self._wander_lock:
                if not self._closing.is_set():
                    self.wander.start()
                    print(
                        f"{name.upper()} REACTION COMPLETE: Wander resumed",
                        flush=True,
                    )
        except Exception as exc:
            print(f"WANDER RESUME FAILED: {exc}; robot remains stopped", flush=True)

    def close(self) -> None:
        self._closing.set()
        try:
            if self.wander is not None:
                with self._wander_lock:
                    self.wander.close()
        finally:
            try:
                self.engine.close(wait=True, cancel_pending=True)
            finally:
                self.speech.close()
        if self._completion_thread is not None:
            self._completion_thread.join(timeout=2)


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
    blocking_overlap = float(values.get("plushie_absence_blocking_overlap", 0.1))
    if not all(math.isfinite(v) and v > 0 for v in (duration, grace, cooldown, absence)):
        raise ValueError("plushie audio timing values must be finite and positive")
    if not math.isfinite(blocking_overlap) or not 0 < blocking_overlap <= 1:
        raise ValueError("plushie absence blocking overlap must be in (0, 1]")
    path = Path(values["plushie_sound"])
    path = (path if path.is_absolute() else root / path).resolve()
    validate_sound(path)
    return FoundSettings(
        confidence, duration, grace, cooldown, (path,), output, absence,
        blocking_overlap,
    )


def load_quiet_gain_db(root) -> float:
    with (root / "config/yolo_objects.yaml").open(encoding="utf-8") as stream:
        values = yaml.safe_load(stream)
    gain_db = float(values["quiet_mode_gain_db"])
    if not math.isfinite(gain_db) or not -60.0 <= gain_db < 0.0:
        raise ValueError("quiet_mode_gain_db must be finite and in [-60, 0) dB")
    return gain_db


def validate_sound(path):
    try:
        with wave.open(str(path), "rb") as stream:
            if stream.getcomptype() != "NONE" or stream.getnframes() <= 0:
                raise ValueError("Expected a nonempty PCM WAV")
            return (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()*8,
                    stream.getnframes()/stream.getframerate())
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError(f"Cannot play existing WAV {path}: {exc}") from exc


def select_audio_trigger(
    result,
    now,
    person_gate,
    banana_gate,
    plushie_gate=None,
    *,
    audio_busy=False,
    plushie_result=None,
    reaction_target="all",
):
    """Choose at most one reaction in plushie, banana, person priority order."""
    plushie_result = result if plushie_result is None else plushie_result
    if reaction_target == "person":
        return "person" if person_gate.update(
            result, now, audio_busy=audio_busy
        ) else None
    if reaction_target == "banana":
        return "banana" if banana_gate.update(
            result, now, audio_busy=audio_busy
        ) else None
    if reaction_target == "plushie":
        if plushie_gate is None:
            return None
        return select_plushie_only_trigger(
            plushie_result, now, plushie_gate, audio_busy=audio_busy
        )
    if reaction_target != "all":
        raise ValueError(f"Unknown reaction target: {reaction_target}")
    if plushie_gate is not None and plushie_gate.update(
            plushie_result, now, audio_busy=audio_busy):
        banana_gate.update(result, now, audio_busy=True, inhibit=True)
        person_gate.update(result, now, audio_busy=True, inhibit=True)
        return "plushie"
    plushie_visible = bool(plushie_result.plushies)
    if banana_gate.update(
            result, now, audio_busy=audio_busy, inhibit=plushie_visible):
        person_gate.update(result, now, audio_busy=True, inhibit=True)
        return "banana"
    if person_gate.update(
            result, now, audio_busy=audio_busy,
            inhibit=bool(plushie_visible or result.bananas)):
        return "person"
    return None


def select_plushie_only_trigger(result, now, plushie_gate, *, audio_busy=False):
    """The MotionDecode integration intentionally exposes only plushie."""
    if plushie_gate.update(result, now, audio_busy=audio_busy):
        return "plushie"
    return None


class FoundGate:
    """Fresh source timestamps only: replaying one YOLO result cannot trigger."""
    def __init__(self, duration=.3, grace=.15, cooldown=2., confidence=.25,
                 rearm_absence=1., object_attribute="people",
                 absence_blocking_attributes=(), absence_overlap=0.1):
        self.duration, self.grace, self.cooldown = duration, grace, cooldown
        self.confidence = confidence
        self.rearm_absence = rearm_absence
        self.object_attribute = object_attribute
        self.absence_blocking_attributes = tuple(absence_blocking_attributes)
        self.absence_overlap = absence_overlap
        self.last_positive_boxes = ()
        self.waiting_clear = False
        self.clear_start = self.clear_last = None
        self.start = self.last = None
        self.last_sample = -math.inf
        self.eligible_after = -math.inf
        self.until = -math.inf
        self.state = "SEARCHING"
        self.confirmed_start = None
        self.confirmed_at = None

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
        positive_items = tuple(
            item for item in getattr(result, self.object_attribute)
            if item.confidence >= self.confidence
        )
        positive = bool(positive_items)
        if positive_items:
            self.last_positive_boxes = tuple(item.box for item in positive_items)
        if self.waiting_clear:
            if positive or self._absence_is_blocked(result):
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
        self.confirmed_start = self.start
        self.confirmed_at = stamp
        self.eligible_after = self.until
        self.start = self.last = None
        self.state = "COOLDOWN"
        self.waiting_clear = True
        return True

    def _absence_is_blocked(self, result):
        """Reject a negative that still contains an overlapping misclassification."""
        for attribute in self.absence_blocking_attributes:
            for item in getattr(result, attribute):
                if item.confidence < self.confidence:
                    continue
                for previous in self.last_positive_boxes:
                    if self._overlap_fraction(previous, item.box) >= self.absence_overlap:
                        return True
        return False

    @staticmethod
    def _overlap_fraction(reference, candidate):
        left = max(reference[0], candidate[0])
        top = max(reference[1], candidate[1])
        right = min(reference[2], candidate[2])
        bottom = min(reference[3], candidate[3])
        intersection = max(0.0, right - left) * max(0.0, bottom - top)
        reference_area = max(0.0, reference[2] - reference[0]) * max(
            0.0, reference[3] - reference[1]
        )
        return intersection / reference_area if reference_area else 0.0

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
