from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import queue
import threading
import time
from typing import Callable, Protocol
import uuid

from g1_bottle_reaction.adapters.robot import RobotAdapter
from g1_bottle_reaction.adapters.speech import SpeechBackend
from g1_bottle_reaction.config.loader import ReactionConfig
from g1_bottle_reaction.reactions.models import Reaction
from g1_bottle_reaction.state.events import ReactionEvent

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReactionDecision:
    accepted: bool
    reaction: Reaction | None
    job: ReactionJob | None = None


class ReactionLifecycleState(str, Enum):
    ACCEPTED = "ACCEPTED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReactionCompletionError(RuntimeError):
    pass


class ReactionJob:
    """Observable lifecycle for one accepted reaction."""

    def __init__(self, reaction: Reaction, *, accepted_at: float) -> None:
        self.id = str(uuid.uuid4())
        self.reaction = reaction
        self.accepted_at = accepted_at
        self.state = ReactionLifecycleState.ACCEPTED
        self.error: Exception | None = None
        self._completed = threading.Event()
        self._lock = threading.Lock()

    @property
    def successful(self) -> bool:
        with self._lock:
            return self.state is ReactionLifecycleState.COMPLETED

    def wait(self, timeout: float | None = None) -> bool:
        return self._completed.wait(timeout)

    def _mark_started(self) -> None:
        with self._lock:
            self.state = ReactionLifecycleState.STARTED

    def _mark_completed(self) -> None:
        with self._lock:
            self.state = ReactionLifecycleState.COMPLETED
            self._completed.set()

    def _mark_failed(self, error: Exception) -> None:
        with self._lock:
            self.error = error
            self.state = ReactionLifecycleState.FAILED
            self._completed.set()


class ReactionLifecycleObserver(Protocol):
    """Navigation-neutral observer for accepted reaction jobs."""

    def on_accepted(self, job: ReactionJob) -> None: ...

    def before_start(self, job: ReactionJob) -> None: ...

    def on_started(self, job: ReactionJob) -> None: ...

    def on_completed(self, job: ReactionJob) -> None: ...

    def on_failed(self, job: ReactionJob, error: Exception) -> None: ...


class ReactionEngine:
    """Maps events to reactions and executes them on a small worker queue."""

    def __init__(
        self,
        config: ReactionConfig,
        robot: RobotAdapter,
        speech: SpeechBackend,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        start_worker: bool = True,
        lifecycle_observer: ReactionLifecycleObserver | None = None,
        motion_completion_timeout_s: float | None = None,
    ) -> None:
        self.config = config
        self.robot = robot
        self.speech = speech
        self._sleep = sleep
        self._clock = clock
        self._last_reaction_at = float("-inf")
        self._last_priority = 0
        if motion_completion_timeout_s is not None and motion_completion_timeout_s <= 0:
            raise ValueError("motion completion timeout must be positive")
        self.lifecycle_observer = lifecycle_observer
        self.motion_completion_timeout_s = motion_completion_timeout_s
        self._queue: queue.Queue[ReactionJob | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._current: Reaction | None = None
        self._lock = threading.Lock()
        self._decision_lock = threading.Lock()
        self._last_error: Exception | None = None
        if start_worker:
            self._worker = threading.Thread(
                target=self._run, name="reaction-worker", daemon=True
            )
            self._worker.start()

    @property
    def current_reaction(self) -> Reaction | None:
        with self._lock:
            return self._current

    @property
    def last_error(self) -> Exception | None:
        with self._lock:
            return self._last_error

    def resolve(self, event: ReactionEvent, encounter_count: int) -> Reaction:
        try:
            base = self.config.items[event.value]
        except KeyError as exc:
            raise KeyError(f"No reaction configured for {event.value}") from exc
        return base.for_encounter(encounter_count)

    def handle(
        self, event: ReactionEvent, *, encounter_count: int, now: float
    ) -> ReactionDecision:
        reaction = self.resolve(event, encounter_count)
        with self._decision_lock:
            within_cooldown = now - self._last_reaction_at < self.config.cooldown_seconds
            if (
                within_cooldown
                and not reaction.bypass_cooldown
                and reaction.priority <= self._last_priority
            ):
                return ReactionDecision(accepted=False, reaction=reaction)
            self._last_reaction_at = now
            self._last_priority = reaction.priority
        job = ReactionJob(reaction, accepted_at=now)
        if self.lifecycle_observer is not None:
            try:
                self.lifecycle_observer.on_accepted(job)
            except Exception as exc:
                job._mark_failed(exc)
                with self._lock:
                    self._last_error = exc
                LOGGER.exception("Reaction acceptance observer failed")
                return ReactionDecision(accepted=True, reaction=reaction, job=job)
        if self._worker is None:
            self._execute_job(job, raise_errors=True)
        else:
            self._queue.put(job)
        return ReactionDecision(accepted=True, reaction=reaction, job=job)

    def _execute_outputs(self, reaction: Reaction) -> None:
        with self._lock:
            self._current = reaction
        try:
            if reaction.motion == "custom_notice":
                started = self._clock()
                if self.config.timeline_debug:
                    LOGGER.info("[REACTION] custom_notice START t=0.000")
                motion_started = self.robot.play_motion_timed(
                    reaction.motion,
                    timeline_start=started,
                    timing_debug=self.config.timeline_debug,
                )
                if reaction.speech and motion_started:
                    remaining = reaction.speech_delay_seconds - (
                        self._clock() - started
                    )
                    if remaining > 0:
                        self._sleep(remaining)
                    elapsed = self._clock() - started
                    if self.config.timeline_debug:
                        LOGGER.info("[SPEECH] requested t=%.3f", elapsed)
                    self.speech.speak_timed(
                        reaction.speech,
                        voice_profile=reaction.voice_profile,
                        on_playback_start=(
                            lambda: LOGGER.info(
                                "[SPEECH] actual playback start t=%.3f",
                                self._clock() - started,
                            )
                            if self.config.timeline_debug
                            else None
                        ),
                    )
            else:
                self.robot.play_motion(reaction.motion)
                if reaction.speech:
                    self._sleep(reaction.speech_delay_seconds)
                    self.speech.speak(
                        reaction.speech, voice_profile=reaction.voice_profile
                    )
        finally:
            with self._lock:
                self._current = None

    def _execute_job(self, job: ReactionJob, *, raise_errors: bool) -> None:
        observer = self.lifecycle_observer
        try:
            if observer is not None:
                observer.before_start(job)
            job._mark_started()
            if observer is not None:
                observer.on_started(job)
            self._execute_outputs(job.reaction)
            if self.motion_completion_timeout_s is not None:
                completed = self.robot.wait_for_motion_complete(
                    job.reaction.motion,
                    timeout=self.motion_completion_timeout_s,
                )
                if not completed:
                    raise ReactionCompletionError(
                        f"Motion completion was not confirmed for {job.reaction.motion!r}"
                    )
            if observer is not None:
                observer.on_completed(job)
            # Signal waiters only after the lifecycle observer has completed its
            # safety work (for example, deciding whether patrol may resume).
            job._mark_completed()
        except Exception as exc:
            with self._lock:
                self._last_error = exc
                self._current = None
            LOGGER.exception("Reaction '%s' failed", job.reaction.name)
            if observer is not None:
                try:
                    observer.on_failed(job, exc)
                except Exception:
                    LOGGER.exception("Reaction failure observer failed")
            # Failure waiters likewise wake only after fail-safe cleanup ran.
            job._mark_failed(exc)
            if raise_errors:
                raise

    def execute(self, reaction: Reaction) -> None:
        """Execute an explicit diagnostic reaction on the shared timeline."""

        self._execute_job(
            ReactionJob(reaction, accepted_at=self._clock()),
            raise_errors=True,
        )

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                self._execute_job(job, raise_errors=False)
            finally:
                self._queue.task_done()

    def close(self, *, wait: bool = True) -> None:
        if self._worker is not None:
            if wait:
                self._queue.join()
            self._queue.put(None)
            if wait:
                self._worker.join(timeout=5)
            self._worker = None
        self.robot.close()
