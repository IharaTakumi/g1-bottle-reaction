from __future__ import annotations

from pathlib import Path
import subprocess
import threading
import time

import pytest

from g1_bottle_reaction.adapters.robot import RobotAdapter
from g1_bottle_reaction.adapters.motiondecode_reaction import MotionDecodeSafeReturnMiss
from g1_bottle_reaction.game_vision.found_audio import (
    FoundReactionController,
    FoundSettings,
)
from g1_bottle_reaction.game_vision.wander_interlock import RemoteWanderController
from g1_bottle_reaction.reactions.models import Reaction


class FakeWander:
    def __init__(self, events):
        self.events = events
        self.running = False

    def start(self):
        self.running = True
        self.events.append("WANDER START")

    def stop_and_wait(self):
        assert self.running
        self.running = False
        self.events.append("WANDER STOP")

    def close(self):
        if self.running:
            self.running = False
            self.events.append("WANDER STOP")


class RecordingOutput:
    def __init__(self):
        self.played = []

    def play_wav(self, _path):
        self.played.append(_path)

    def close(self):
        pass


class OrderedRobot(RobotAdapter):
    def __init__(self, events, *, fail=False):
        self.events = events
        self.fail = fail

    def play_motion(self, _motion):
        self.events.append("REACTION START")
        if self.fail:
            self.events.append("REACTION FAIL")
            raise RuntimeError("reaction failed")
        self.events.append("REACTION COMPLETE")


class BlockingRobot(RobotAdapter):
    def __init__(self, events):
        self.events = events
        self.release = threading.Event()

    def play_motion(self, _motion):
        self.events.append("REACTION START")
        assert self.release.wait(timeout=2)
        self.events.append("REACTION COMPLETE")


class PreflightRobot(OrderedRobot):
    def __init__(self, events, *, safe):
        super().__init__(events)
        self.safe = safe
        self.last_preflight_error = "robot not stable" if not safe else ""

    def preflight_motion(self):
        self.events.append("REACTION PREFLIGHT")
        return self.safe


class SafeReturnMissRobot(OrderedRobot):
    def play_motion(self, _motion):
        self.events.append("REACTION START")
        self.events.append("SAFE RETURN MISS")
        raise MotionDecodeSafeReturnMiss(
            "found",
            {"returned_to_q0": False, "controlled_q0_return_error_rad": .0328},
            {"state": "READY", "weight": 0.0, "ownership_safe": True},
        )


class SequenceRobot(RobotAdapter):
    def __init__(self, events):
        self.events = events

    def preflight_motion(self):
        self.events.append("STABLE")
        return True

    def play_motion(self, motion):
        self.events.append(motion.removeprefix("motiondecode:").upper())
        self.events.append("REACTION_DONE")


def make_controller(events, *, fail=False, timeout=1.0):
    configured = FoundSettings(
        confidence=.25,
        duration=.3,
        grace=.15,
        cooldown=2,
        sounds=(Path("detected.wav"),),
        output="mock",
        rearm_absence=1,
    )
    wander = FakeWander(events)
    controller = FoundReactionController(
        {"person": configured},
        RecordingOutput(),
        OrderedRobot(events, fail=fail),
        base_reaction=Reaction("FOUND", "notice", "unused", 0),
        cooldown_seconds=0,
        wander=wander,
        reaction_completion_timeout=timeout,
    )
    return controller, wander


def wait_not_busy(controller):
    deadline = time.monotonic() + 2
    while controller.busy and time.monotonic() < deadline:
        time.sleep(.005)
    assert not controller.busy


def test_wander_stops_before_reaction_and_resumes_only_after_completion():
    events = []
    controller, wander = make_controller(events)
    wander.start()
    events.append("PERSON FOUND")
    try:
        assert controller.trigger("person", time.monotonic())
        wait_not_busy(controller)
        assert events == [
            "WANDER START",
            "PERSON FOUND",
            "WANDER STOP",
            "REACTION START",
            "REACTION COMPLETE",
            "WANDER START",
        ]
        assert wander.running
    finally:
        controller.close()


def test_settle_and_preflight_happen_after_stop_before_reaction():
    events = []
    configured = FoundSettings(.25, .3, .15, 2, (Path("detected.wav"),), "mock", 1)
    wander = FakeWander(events)
    robot = PreflightRobot(events, safe=True)
    controller = FoundReactionController(
        {"person": configured}, RecordingOutput(), robot,
        base_reaction=Reaction("FOUND", "notice", "unused", 0),
        cooldown_seconds=0, wander=wander, reaction_settle_seconds=.75,
        sleeper=lambda seconds: events.append(("SETTLE", seconds)),
    )
    wander.start()
    try:
        assert controller.trigger("person", 1)
        wait_not_busy(controller)
        assert events[:5] == [
            "WANDER START", "WANDER STOP", ("SETTLE", .75),
            "REACTION PREFLIGHT", "REACTION START",
        ]
        assert events[-1] == "WANDER START"
    finally:
        controller.close()


def test_failed_preflight_plays_audio_only_and_resumes_without_retry():
    events = []
    configured = FoundSettings(.25, .3, .15, 2, (Path("detected.wav"),), "mock", 1)
    wander = FakeWander(events)
    robot = PreflightRobot(events, safe=False)
    output = RecordingOutput()
    controller = FoundReactionController(
        {"person": configured}, output, robot,
        base_reaction=Reaction("FOUND", "notice", "unused", 0),
        cooldown_seconds=0, wander=wander,
    )
    wander.start()
    try:
        assert controller.trigger("person", 1)
        assert events == [
            "WANDER START", "WANDER STOP", "REACTION PREFLIGHT", "WANDER START",
        ]
        assert output.played == [Path("detected.wav")]
        assert "REACTION START" not in events
        assert wander.running
    finally:
        controller.close()


def test_safe_return_miss_with_safe_postflight_resumes_wander():
    events = []
    configured = FoundSettings(.25, .3, .15, 2, (Path("detected.wav"),), "mock", 1)
    wander = FakeWander(events)
    controller = FoundReactionController(
        {"person": configured}, RecordingOutput(), SafeReturnMissRobot(events),
        base_reaction=Reaction("FOUND", "notice", "unused", 0),
        cooldown_seconds=0, wander=wander,
    )
    wander.start()
    try:
        assert controller.trigger("person", 1)
        wait_not_busy(controller)
        assert events == [
            "WANDER START", "WANDER STOP", "REACTION START",
            "SAFE RETURN MISS", "WANDER START",
        ]
        assert controller.motion_error == ""
        assert wander.running
    finally:
        controller.close()


@pytest.mark.parametrize(
    ("target", "motion"),
    (("person", "FOUND"), ("banana", "SURPRISE"), ("plushie", "SURPRISE")),
)
def test_silent_mock_full_cycle_for_every_detection_target(target, motion):
    events = []
    configured = FoundSettings(.25, .3, .15, 2, (Path("suppressed.wav"),), "mock", 1)
    wander = FakeWander(events)
    silent_output = RecordingOutput()
    controller = FoundReactionController(
        {name: configured for name in ("person", "banana", "plushie")},
        silent_output,
        SequenceRobot(events),
        base_reaction=Reaction("FOUND", "notice", "unused", 0),
        cooldown_seconds=0,
        motion_overrides={
            "person": "motiondecode:found",
            "banana": "motiondecode:surprise",
            "plushie": "motiondecode:surprise",
        },
        speech_delay_overrides={"person": 0, "banana": 0, "plushie": 0},
        wander=wander,
    )
    wander.start()
    events.append("DETECT")
    try:
        assert controller.trigger(target, 1)
        wait_not_busy(controller)
        assert events == [
            "WANDER START", "DETECT", "WANDER STOP", "STABLE",
            motion, "REACTION_DONE", "WANDER START",
        ]
        # The mock consumes the speech request without opening an audio device.
        assert silent_output.played == [Path("suppressed.wav")]
        print(f"{target}: " + " -> ".join(events))
    finally:
        controller.close()


def test_reaction_failure_leaves_wander_stopped():
    events = []
    controller, wander = make_controller(events, fail=True)
    wander.start()
    try:
        assert controller.trigger("person", time.monotonic())
        wait_not_busy(controller)
        assert events == [
            "WANDER START", "WANDER STOP", "REACTION START", "REACTION FAIL"
        ]
        assert not wander.running
    finally:
        controller.close()


def test_reaction_completion_timeout_never_resumes_later():
    events = []
    configured = FoundSettings(
        .25, .3, .15, 2, (Path("detected.wav"),), "mock", 1
    )
    wander = FakeWander(events)
    robot = BlockingRobot(events)
    controller = FoundReactionController(
        {"person": configured}, RecordingOutput(), robot,
        base_reaction=Reaction("FOUND", "notice", "unused", 0),
        cooldown_seconds=0, wander=wander, reaction_completion_timeout=.02,
    )
    wander.start()
    try:
        assert controller.trigger("person", time.monotonic())
        time.sleep(.05)
        assert not wander.running
        robot.release.set()
        wait_not_busy(controller)
        assert not wander.running
        assert events == [
            "WANDER START", "WANDER STOP", "REACTION START", "REACTION COMPLETE"
        ]
    finally:
        robot.release.set()
        controller.close()


def test_remote_controller_uses_verified_pid_and_never_sigkills():
    commands = []
    responses = iter(("STARTED\n", "STOPPED\n"))

    def runner(command, **kwargs):
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, next(responses), "")

    controller = RemoteWanderController(
        "unitree@pc2", ssh_control="/tmp/control", runner=runner
    )
    controller.start()
    controller.stop_and_wait()

    start_command = commands[0][0][-1]
    stop_command = commands[1][0][-1]
    assert "g1-wander-reactive-mvp.py" in start_command
    assert "--duration 3600 --max-pulses 10000" in start_command
    assert "/tmp/g1-mapless-wander.pid" in start_command
    assert "kill -TERM" in stop_command
    assert "/proc/$1/cmdline" in stop_command
    assert "SIGKILL" not in stop_command
    assert "pkill" not in stop_command
