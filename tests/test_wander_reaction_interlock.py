from __future__ import annotations

from pathlib import Path
import subprocess
import threading
import time

from g1_bottle_reaction.adapters.robot import RobotAdapter
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
    def play_wav(self, _path):
        pass

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
