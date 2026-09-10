"""Small person debounce/cooldown and a single non-queuing audio worker."""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import random
import threading
import wave

import yaml


@dataclass(frozen=True)
class FoundSettings:
    confidence: float
    duration: float
    grace: float
    cooldown: float
    sounds: tuple[Path, ...]
    output: str
    rearm_absence: float


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
        raise ValueError("No default cache sound selected; use --found-sound /absolute/path.wav")
    if str(path) == "random":
        paths = tuple(sorted((root / ".cache/tts").glob("*.wav")))
        if not paths:
            raise ValueError("No existing .cache/tts/*.wav files")
    else:
        path = Path(path).expanduser()
        paths = ((path if path.is_absolute() else root / path).resolve(),)
    for path in paths:
        validate_sound(path)
    return FoundSettings(confidence, duration, grace, cooldown, paths, pick("found_output", "output"), absence)


def validate_sound(path):
    try:
        with wave.open(str(path), "rb") as stream:
            if stream.getcomptype() != "NONE" or stream.getnframes() <= 0:
                raise ValueError("Expected a nonempty PCM WAV")
            return (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()*8,
                    stream.getnframes()/stream.getframerate())
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError(f"Cannot play existing WAV {path}: {exc}") from exc


class FoundGate:
    """Fresh source timestamps only: replaying one YOLO result cannot trigger."""
    def __init__(self, duration=.3, grace=.15, cooldown=2., confidence=.25, rearm_absence=1.):
        self.duration, self.grace, self.cooldown = duration, grace, cooldown
        self.confidence = confidence
        self.rearm_absence = rearm_absence
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

    def update(self, result, now, *, audio_busy=False):
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
        positive = any(p.confidence >= self.confidence for p in result.people)
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
        if stamp - self.start + 1e-9 < self.duration or audio_busy:
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


class SingleAudioWorker:
    """One persistent thread, no playback backlog, never overlapping sounds."""
    def __init__(self, output, sounds):
        self.output, self.sounds = output, tuple(sounds)
        if not self.sounds:
            raise ValueError("At least one existing sound is required")
        self.condition = threading.Condition()
        self.busy = False
        self.pending = False
        self.stopping = False
        self.error = ""
        self.thread = threading.Thread(target=self._run, name="found-audio", daemon=True)
        self.thread.start()

    def submit(self):
        with self.condition:
            if self.busy or self.stopping or self.error:
                return False
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
            try:
                sound = random.choice(self.sounds)
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
