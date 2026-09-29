from __future__ import annotations

from pathlib import Path
import threading
import time

import pytest

from g1_bottle_reaction.adapters.motiondecode_reaction import (
    MotionDecodeRecoverableAbort,
    MotionDecodeSafeReturnMiss,
)
from g1_bottle_reaction.adapters.robot import RobotAdapter
from g1_bottle_reaction.game_vision.found_audio import FoundReactionController, FoundSettings
from g1_bottle_reaction.reactions.models import Reaction


class FakePatrol:
    def __init__(self, events):
        self.events = events
        self.running = True
        self.aborted = False
        self.last_stop_sent_monotonic = None
        self.phase = "FORWARD_OUT"
        self.forward_leg_id = 1
        self.successful = set()

    def reaction_context(self, target, force=False):
        del force
        return {
            "eligible": self.phase.startswith("FORWARD_")
            and (self.forward_leg_id, target) not in self.successful,
            "phase": self.phase, "paused": False,
            "forward_leg_id": self.forward_leg_id,
        }

    def mark_reaction_success(self, target, leg_id):
        self.successful.add((leg_id, target))

    def stop_and_wait(self):
        self.running = False
        self.last_stop_sent_monotonic = time.monotonic()
        self.events.append("PATROL STOP")

    def wait_reaction_ready(self):
        self.events.append("TELEMETRY READY")

    def start(self):
        self.running = True
        self.events.append("PATROL RESUME")

    def abort(self, reason):
        self.aborted = True
        self.running = False
        self.events.append(("FINAL STOP", reason))

    def close(self):
        if not self.running:
            self.abort("closed")


class BlockingPatrol(FakePatrol):
    def __init__(self, events):
        super().__init__(events)
        self.stop_entered = threading.Event()
        self.release_stop = threading.Event()

    def stop_and_wait(self):
        self.events.append("PATROL STOP REQUEST")
        self.stop_entered.set()
        assert self.release_stop.wait(timeout=2)
        super().stop_and_wait()


class Output:
    def __init__(self, events):
        self.events = events

    def play_wav(self, _path):
        self.events.append("AUDIO")

    def close(self):
        pass


class Robot(RobotAdapter):
    def __init__(self, events, *, ready=True, safe_return=True, postflight=None,
                 recoverable_abort=False):
        self.events = events
        self.ready = ready
        self.safe_return = safe_return
        self.postflight = postflight or {
            "accepted": True,
            "state": "READY",
            "lowstate_age_s": .01,
            "ownership_safe": True,
            "external_writers": 0,
            "weight": 0.0,
            "fault": None,
        }
        self.recoverable_abort = recoverable_abort
        self.last_preflight_error = "Arms are not stationary" if not ready else ""

    def preflight_motion(self):
        self.events.append("PREFLIGHT")
        return self.ready

    def play_motion(self, motion):
        self.events.append(motion)
        if self.recoverable_abort:
            raise MotionDecodeRecoverableAbort(
                "found", {
                    "classification": "RECOVERABLE_ABORT",
                    "recoverable_reason": "tilt/angular exceed",
                }, self.postflight,
            )
        if not self.safe_return:
            raise MotionDecodeSafeReturnMiss(
                "found", {"returned_to_q0": False},
                self.postflight,
            )
        self.events.append("Q0 WEIGHT0")


def build(events, *, ready=True, safe_return=True, postflight=None,
          allow_safe_return_miss_resume=False, recoverable_abort=False,
          hackathon_runtime=False):
    settings = FoundSettings(.25, .3, .15, 2, (Path("reaction.wav"),), "mock", 1)
    patrol = FakePatrol(events)
    controller = FoundReactionController(
        {"person": settings}, Output(events),
        Robot(events, ready=ready, safe_return=safe_return, postflight=postflight,
              recoverable_abort=recoverable_abort),
        base_reaction=Reaction("FOUND", "notice", "unused", 0),
        cooldown_seconds=0,
        motion_overrides={"person": "motiondecode:found"},
        speech_delay_overrides={"person": 0},
        patrol=patrol,
        reaction_completion_timeout=1,
        allow_safe_return_miss_resume=allow_safe_return_miss_resume,
        hackathon_runtime=hackathon_runtime,
    )
    return controller, patrol


def test_hackathon_recoverable_reaction_abort_resumes_without_retry():
    events = []
    controller, patrol = build(
        events, recoverable_abort=True, hackathon_runtime=True
    )
    try:
        assert controller.trigger("person", 1)
        wait_idle(controller)
        assert patrol.running
        assert not patrol.aborted
        assert events.count("motiondecode:found") == 1
        assert "PATROL RESUME" in events
    finally:
        controller.close()


def test_audio_starts_while_patrol_stop_confirmation_is_pending():
    events = []
    settings = FoundSettings(.25, .3, .15, 2, (Path("reaction.wav"),), "mock", 1)
    patrol = BlockingPatrol(events)
    controller = FoundReactionController(
        {"person": settings}, Output(events), Robot(events),
        base_reaction=Reaction("FOUND", "notice", "unused", 0),
        cooldown_seconds=0,
        motion_overrides={"person": "motiondecode:found"},
        speech_delay_overrides={"person": 0}, patrol=patrol,
        reaction_completion_timeout=1,
    )
    try:
        assert controller.trigger("person", time.monotonic())
        assert patrol.stop_entered.wait(timeout=1)
        deadline = time.monotonic() + 1
        while "AUDIO" not in events and time.monotonic() < deadline:
            time.sleep(.005)
        assert "AUDIO" in events
        assert "motiondecode:found" not in events
        patrol.release_stop.set()
        wait_idle(controller)
        assert events.index("AUDIO") < events.index("motiondecode:found")
    finally:
        patrol.release_stop.set()
        controller.close()


@pytest.mark.parametrize(
    ("target", "motion"),
    [("banana", "motiondecode:surprise"), ("plushie", "motiondecode:surprise")],
)
def test_object_audio_waits_for_confirmed_patrol_stop_and_starts_with_motion(
    target, motion
):
    events = []
    settings = FoundSettings(.25, .15, 1, 2, (Path("reaction.wav"),), "mock", 1)
    patrol = BlockingPatrol(events)
    controller = FoundReactionController(
        {target: settings}, Output(events), Robot(events),
        base_reaction=Reaction("SURPRISE", "notice", "unused", 0),
        cooldown_seconds=0,
        motion_overrides={target: motion},
        speech_delay_overrides={target: 0}, patrol=patrol,
        reaction_completion_timeout=1,
    )
    try:
        assert controller.trigger(target, time.monotonic())
        assert patrol.stop_entered.wait(timeout=1)
        time.sleep(.03)
        assert "AUDIO" not in events
        assert motion not in events
        patrol.release_stop.set()
        wait_idle(controller)
        assert events.index("PATROL STOP") < events.index("TELEMETRY READY")
        assert events.index("TELEMETRY READY") < events.index("AUDIO")
        assert events.index("TELEMETRY READY") < events.index(motion)
    finally:
        patrol.release_stop.set()
        controller.close()


def test_object_audio_gate_cancels_without_hanging_when_preflight_fails():
    events = []
    settings = FoundSettings(.25, .15, 1, 2, (Path("reaction.wav"),), "mock", 1)
    patrol = FakePatrol(events)
    controller = FoundReactionController(
        {"banana": settings}, Output(events), Robot(events, ready=False),
        base_reaction=Reaction("SURPRISE", "notice", "unused", 0),
        cooldown_seconds=0,
        motion_overrides={"banana": "motiondecode:surprise"},
        speech_delay_overrides={"banana": 0}, patrol=patrol,
        reaction_completion_timeout=1,
    )
    try:
        assert controller.trigger("banana", time.monotonic())
        wait_idle(controller)
        assert patrol.aborted
        assert "AUDIO" not in events
        assert "motiondecode:surprise" not in events
    finally:
        controller.close()


def wait_idle(controller):
    deadline = time.monotonic() + 2
    while controller.busy and time.monotonic() < deadline:
        time.sleep(.005)
    assert not controller.busy


def test_patrol_resumes_only_after_motion_audio_and_q0_weight_zero():
    events = []
    controller, patrol = build(events)
    try:
        assert controller.trigger("person", 1)
        wait_idle(controller)
        assert events.index("PATROL STOP") < events.index("PREFLIGHT")
        assert events.index("TELEMETRY READY") < events.index("PREFLIGHT")
        assert events.index("AUDIO") < events.index("PATROL RESUME")
        assert events[-1] == "PATROL RESUME"
        assert events.index("Q0 WEIGHT0") < events.index("PATROL RESUME")
        assert events.index("AUDIO") < events.index("PATROL RESUME")
        assert patrol.running
    finally:
        controller.close()


def test_stationary_preflight_failure_final_stops_without_reaction_or_audio():
    events = []
    controller, patrol = build(events, ready=False)
    try:
        assert controller.trigger("person", 1)
        wait_idle(controller)
        assert "motiondecode:found" not in events
        assert "AUDIO" in events
        assert patrol.aborted
        assert not patrol.running
    finally:
        controller.close()


def test_unconfirmed_q0_return_final_stops_and_never_resumes():
    events = []
    controller, patrol = build(events, safe_return=False)
    try:
        assert controller.trigger("person", 1)
        wait_idle(controller)
        assert patrol.aborted
        assert "PATROL RESUME" not in events
        assert not patrol.running
    finally:
        controller.close()


def test_safe_return_miss_with_safe_postflight_and_opt_in_resumes_patrol():
    events = []
    controller, patrol = build(
        events, safe_return=False, allow_safe_return_miss_resume=True
    )
    try:
        assert controller.trigger("person", 1)
        wait_idle(controller)
        assert not patrol.aborted
        assert patrol.running
        assert events[-1] == "PATROL RESUME"
    finally:
        controller.close()


@pytest.mark.parametrize(
    "unsafe_field",
    [
        {"lowstate_age_s": .2},
        {"fault": "hard fault"},
        {"ownership_safe": False},
    ],
)
def test_safe_return_miss_opt_in_still_aborts_on_unsafe_postflight(unsafe_field):
    events = []
    postflight = {
        "accepted": True,
        "state": "READY",
        "lowstate_age_s": .01,
        "ownership_safe": True,
        "external_writers": 0,
        "weight": 0.0,
        "fault": None,
        **unsafe_field,
    }
    controller, patrol = build(
        events,
        safe_return=False,
        postflight=postflight,
        allow_safe_return_miss_resume=True,
    )
    try:
        assert controller.trigger("person", 1)
        wait_idle(controller)
        assert patrol.aborted
        assert "PATROL RESUME" not in events
    finally:
        controller.close()
