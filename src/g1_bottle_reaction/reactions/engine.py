from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time
from typing import Callable

from g1_bottle_reaction.adapters.robot import RobotAdapter
from g1_bottle_reaction.adapters.speech import SpeechBackend
from g1_bottle_reaction.config.loader import ReactionConfig
from g1_bottle_reaction.reactions.models import Reaction
from g1_bottle_reaction.state.events import ReactionEvent


@dataclass(frozen=True, slots=True)
class ReactionDecision:
    accepted: bool
    reaction: Reaction | None


class ReactionEngine:
    """Maps events to reactions and executes them on a small worker queue."""

    def __init__(
        self,
        config: ReactionConfig,
        robot: RobotAdapter,
        speech: SpeechBackend,
        *,
        sleep: Callable[[float], None] = time.sleep,
        start_worker: bool = True,
    ) -> None:
        self.config = config
        self.robot = robot
        self.speech = speech
        self._sleep = sleep
        self._last_reaction_at = float("-inf")
        self._last_priority = 0
        self._queue: queue.Queue[Reaction | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._current: Reaction | None = None
        self._lock = threading.Lock()
        self._decision_lock = threading.Lock()
        if start_worker:
            self._worker = threading.Thread(
                target=self._run, name="reaction-worker", daemon=True
            )
            self._worker.start()

    @property
    def current_reaction(self) -> Reaction | None:
        with self._lock:
            return self._current

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
        if self._worker is None:
            self._execute(reaction)
        else:
            self._queue.put(reaction)
        return ReactionDecision(accepted=True, reaction=reaction)

    def _execute(self, reaction: Reaction) -> None:
        with self._lock:
            self._current = reaction
        try:
            self.robot.play_motion(reaction.motion)
            if reaction.speech:
                self._sleep(reaction.speech_delay_seconds)
                self.speech.speak(
                    reaction.speech, voice_profile=reaction.voice_profile
                )
        finally:
            with self._lock:
                self._current = None

    def _run(self) -> None:
        while True:
            reaction = self._queue.get()
            try:
                if reaction is None:
                    return
                self._execute(reaction)
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
