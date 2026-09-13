from __future__ import annotations

import json
from pathlib import Path
import time

import numpy as np

from g1_bottle_reaction.adapters.g1_ssh_safe_action import (
    RESULT_PREFIX,
    SshG1SafeActionAdapter,
)
from g1_bottle_reaction.game_vision.found_audio import (
    FoundGate,
    FoundReactionController,
    FoundSettings,
)
from g1_bottle_reaction.game_vision.person_yolo import Detection, Person
from g1_bottle_reaction.reactions.engine import ReactionEngine
from g1_bottle_reaction.reactions.models import Reaction


class RecordingWavOutput:
    def __init__(self) -> None:
        self.played: list[Path] = []

    def play_wav(self, path: Path) -> None:
        self.played.append(path)

    def close(self) -> None:
        pass


class FakeSshProcess:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode

    def communicate(self, timeout=None):
        del timeout
        return self.stdout, ""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        return self.returncode


def helper_result(*, ok=True, error="") -> str:
    result = {
        "ok": ok,
        "operation": "notice",
        "dds_initialized": True,
        "action_list_ok": True,
        "action23_available": True,
        "action99_available": True,
        "action23_invoked": ok,
        "action23_code": 0 if ok else None,
        "action99_invoked": ok,
        "action99_code": 0 if ok else None,
        "motion_uncertain": False,
        "error": error,
    }
    return RESULT_PREFIX + json.dumps(result) + "\n"


def detection(stamp: float, positive: bool = True) -> Detection:
    people = (Person((1, 2, 10, 20), 0.9),) if positive else ()
    return Detection(people=people, stamp=stamp, status="RUNNING")


def make_pipeline(popen_factory):
    robot = SshG1SafeActionAdapter(
        "unitree@10.42.0.76",
        "/tmp/fake-control",
        enabled=True,
        motion_mode="safe-actions",
        execute_real_action=True,
        environ={"G1_ALLOW_REAL_ACTION": "1"},
        popen_factory=popen_factory,
    )
    robot.initialize()
    output = RecordingWavOutput()
    settings = FoundSettings(
        confidence=0.25,
        duration=0.3,
        grace=0.15,
        cooldown=2.0,
        sounds=(Path("detected.wav"),),
        output="mock",
        rearm_absence=1.0,
    )
    controller = FoundReactionController(
        {"person": settings, "banana": settings, "plushie": settings},
        output,
        robot,
        base_reaction=Reaction(
            name="FOUND",
            motion="notice",
            speech="unused",
            speech_delay_seconds=0,
        ),
        cooldown_seconds=2.0,
    )
    return controller, robot, output


def wait_reaction(controller: FoundReactionController) -> None:
    deadline = time.monotonic() + 2
    while controller.busy and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not controller.busy


def test_u_v_w_confirmed_detection_continuous_hold_and_rearm() -> None:
    ssh_commands = []

    def popen(command, **kwargs):
        del kwargs
        ssh_commands.append(command)
        return FakeSshProcess(helper_result())

    controller, _, output = make_pipeline(popen)
    gate = FoundGate()
    try:
        # U: one confirmed YOLO event reaches one audio and one SSH notice.
        for stamp in (1.0, 1.1, 1.2, 1.3):
            if gate.update(detection(stamp), stamp, audio_busy=controller.busy):
                assert controller.trigger("person", stamp)
        wait_reaction(controller)
        assert isinstance(controller.engine, ReactionEngine)
        assert len(ssh_commands) == 1
        assert ssh_commands[0][0] == "ssh"
        assert output.played == [Path("detected.wav")]

        # V: a continuously visible target cannot enqueue another reaction.
        for stamp in np.arange(1.4, 4.5, 0.07):
            assert not gate.update(
                detection(float(stamp)), float(stamp), audio_busy=controller.busy
            )
        assert len(ssh_commands) == 1
        assert output.played == [Path("detected.wav")]

        # W: real absence plus cooldown/rearm permits exactly one later event.
        for stamp in np.arange(4.5, 5.7, 0.07):
            assert not gate.update(detection(float(stamp), False), float(stamp))
        for stamp in (5.8, 5.9, 6.0, 6.1):
            if gate.update(detection(stamp), stamp, audio_busy=controller.busy):
                assert controller.trigger("person", stamp)
        wait_reaction(controller)
        assert len(ssh_commands) == 2
        assert output.played == [Path("detected.wav"), Path("detected.wav")]
    finally:
        controller.close()


def test_x_ssh_failure_keeps_audio_but_never_starts_helper_again() -> None:
    ssh_commands = []

    def popen(command, **kwargs):
        del kwargs
        ssh_commands.append(command)
        return FakeSshProcess(
            helper_result(ok=False, error="GetActionList return code 3102"),
            returncode=2,
        )

    controller, robot, output = make_pipeline(popen)
    try:
        assert controller.trigger("person", 1.0)
        wait_reaction(controller)
        assert len(ssh_commands) == 1
        assert output.played == [Path("detected.wav")]
        assert "3102" in robot.motion_disabled_reason

        assert controller.trigger("person", 4.0)
        wait_reaction(controller)
        assert len(ssh_commands) == 1
        assert output.played == [Path("detected.wav"), Path("detected.wav")]
    finally:
        controller.close()


def test_y_shutdown_rejects_reaction_without_starting_ssh_helper() -> None:
    ssh_commands = []

    def popen(command, **kwargs):
        del kwargs
        ssh_commands.append(command)
        return FakeSshProcess(helper_result())

    controller, _, output = make_pipeline(popen)
    controller.close()
    assert not controller.trigger("person", 1.0)
    assert ssh_commands == []
    assert output.played == []
