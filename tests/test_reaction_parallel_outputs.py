from __future__ import annotations

import threading

import pytest

from g1_bottle_reaction.adapters.robot import RobotAdapter
from g1_bottle_reaction.adapters.speech import SpeechBackend
from g1_bottle_reaction.config.loader import ReactionConfig
from g1_bottle_reaction.game_vision.found_audio import FailSafeReactionRobotAdapter
from g1_bottle_reaction.reactions.engine import (
    ReactionEngine,
    ReactionLifecycleState,
)
from g1_bottle_reaction.reactions.models import Reaction
from g1_bottle_reaction.state.events import ReactionEvent


def config() -> ReactionConfig:
    return ReactionConfig(
        cooldown_seconds=0,
        items={
            ReactionEvent.FOUND.value: Reaction(
                "found", "notice", "detected", speech_delay_seconds=0
            )
        },
    )


class RecordingSpeech(SpeechBackend):
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.failure = failure

    def speak(self, text: str, *, voice_profile: str = "neutral") -> None:
        del text, voice_profile
        self.calls += 1
        self.started.set()
        if self.failure is not None:
            raise self.failure


def test_z1_audio_starts_before_motion_finishes() -> None:
    order = []
    release_motion = threading.Event()

    class Robot(RobotAdapter):
        def __init__(self) -> None:
            self.calls = 0

        def play_motion(self, motion: str) -> None:
            del motion
            self.calls += 1
            order.append("motion_start")
            assert release_motion.wait(timeout=2)
            order.append("motion_end")

    class Speech(RecordingSpeech):
        def speak(self, text: str, *, voice_profile: str = "neutral") -> None:
            super().speak(text, voice_profile=voice_profile)
            order.append("audio")

    robot = Robot()
    speech = Speech()
    engine = ReactionEngine(config(), robot, speech)
    try:
        decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=1)
        assert speech.started.wait(timeout=1)
        assert "motion_end" not in order
        release_motion.set()
        assert decision.job is not None and decision.job.wait(timeout=1)
        assert robot.calls == 1
        assert speech.calls == 1
        assert order.index("audio") < order.index("motion_end")
    finally:
        release_motion.set()
        engine.close(wait=True, cancel_pending=True)


def test_z2_slow_motion_does_not_delay_audio_start() -> None:
    motion_started = threading.Event()
    release_motion = threading.Event()

    class SlowRobot(RobotAdapter):
        def play_motion(self, motion: str) -> None:
            del motion
            motion_started.set()
            assert release_motion.wait(timeout=2)

    speech = RecordingSpeech()
    engine = ReactionEngine(config(), SlowRobot(), speech)
    try:
        decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=1)
        assert motion_started.wait(timeout=1)
        assert speech.started.wait(timeout=0.2)
        assert decision.job is not None and not decision.job.wait(0)
        release_motion.set()
        assert decision.job.wait(timeout=1)
    finally:
        release_motion.set()
        engine.close(wait=True, cancel_pending=True)


def test_z3_motion_failure_does_not_cancel_audio_and_latches_motion() -> None:
    class FailingRobot(RobotAdapter):
        def __init__(self) -> None:
            self.calls = 0

        def play_motion(self, motion: str) -> None:
            del motion
            self.calls += 1
            raise RuntimeError("simulated SSH motion failure")

    delegate = FailingRobot()
    robot = FailSafeReactionRobotAdapter(delegate)
    speech = RecordingSpeech()
    engine = ReactionEngine(config(), robot, speech, start_worker=False)
    try:
        engine.handle(ReactionEvent.FOUND, encounter_count=1, now=1)
        engine.handle(ReactionEvent.FOUND, encounter_count=1, now=2)
        assert speech.calls == 2
        assert delegate.calls == 1
        assert "simulated SSH motion failure" in robot.error
    finally:
        engine.close()


def test_z4_audio_failure_does_not_interrupt_motion_cleanup() -> None:
    release_motion = threading.Event()
    cleanup_done = threading.Event()

    class CleanupRobot(RobotAdapter):
        def __init__(self) -> None:
            self.actions = []

        def play_motion(self, motion: str) -> None:
            del motion
            self.actions.append(23)
            assert release_motion.wait(timeout=2)
            self.actions.append(99)
            cleanup_done.set()

    robot = CleanupRobot()
    speech = RecordingSpeech(failure=RuntimeError("simulated audio failure"))
    engine = ReactionEngine(config(), robot, speech)
    try:
        decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=1)
        assert speech.started.wait(timeout=1)
        assert not cleanup_done.is_set()
        release_motion.set()
        assert decision.job is not None and decision.job.wait(timeout=1)
        assert cleanup_done.is_set()
        assert robot.actions == [23, 99]
        assert decision.job.state is ReactionLifecycleState.FAILED
        assert "simulated audio failure" in str(decision.job.error)
    finally:
        release_motion.set()
        engine.close(wait=True, cancel_pending=True)


def test_z5_one_job_submits_each_output_exactly_once() -> None:
    class Robot(RobotAdapter):
        def __init__(self) -> None:
            self.calls = 0

        def play_motion(self, motion: str) -> None:
            del motion
            self.calls += 1

    robot = Robot()
    speech = RecordingSpeech()
    engine = ReactionEngine(config(), robot, speech, start_worker=False)
    try:
        engine.handle(ReactionEvent.FOUND, encounter_count=1, now=1)
        assert robot.calls == 1
        assert speech.calls == 1
    finally:
        engine.close()
