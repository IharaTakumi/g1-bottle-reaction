from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
import time
import wave
import os

import numpy as np
import pytest
import yaml

from g1_bottle_reaction.game_vision.app import build_parser, main
from g1_bottle_reaction.game_vision.dual import FrameState, compose
from g1_bottle_reaction.game_vision.found_audio import (
    FoundGate, FoundReactionController, SingleAudioWorker, load_banana_settings,
    load_plushie_settings, load_settings, select_audio_trigger, validate_sound,
)
from g1_bottle_reaction.adapters.mock_robot import MockRobotAdapter
from g1_bottle_reaction.adapters.g1_robot import (
    ARM_ACTION_RPC_TIMEOUT_CODE,
    G1RobotAdapter,
)
from g1_bottle_reaction.adapters.robot import RobotAdapter
from g1_bottle_reaction.adapters.speech import MuteSpeechBackend
from g1_bottle_reaction.config.loader import ReactionConfig
from g1_bottle_reaction.reactions.engine import ReactionEngine
from g1_bottle_reaction.reactions.models import Reaction
from g1_bottle_reaction.game_vision.person_yolo import Banana, Detection, Person, Plushie
from g1_bottle_reaction.state.events import ReactionEvent


def detection(stamp, positive=True, confidence=.8):
    people = (Person((1, 2, 10, 20), confidence),) if positive else ()
    return Detection(people=people, stamp=stamp, status="RUNNING")


def test_single_result_or_single_frame_never_triggers():
    gate = FoundGate()
    result = detection(1)
    assert not gate.update(result, 1)
    for now in (1.05, 1.1, 1.15, 1.2, 1.4, 2):
        assert not gate.update(result, now)
    assert gate.state == "SEARCHING"


def test_banana_never_triggers_person_audio():
    gate = FoundGate()
    for t in (1, 1.1, 1.2, 1.3, 1.4):
        result = Detection(stamp=t, status='RUNNING',
                           bananas=(Banana((1, 2, 10, 20), .9),))
        assert not gate.update(result, t)
    assert gate.state == 'SEARCHING'


def object_detection(stamp, *, person=False, banana=False, plushie=False):
    return Detection(
        people=(Person((1, 2, 10, 20), .9),) if person else (),
        bananas=(Banana((2, 3, 12, 22), .9),) if banana else (),
        plushies=(Plushie((3, 4, 13, 23), .9),) if plushie else (),
        stamp=stamp, status='RUNNING')


def test_person_has_priority_when_person_and_banana_are_both_visible():
    person_gate = FoundGate()
    banana_gate = FoundGate(object_attribute='bananas')
    selected = []
    for t in (1, 1.1, 1.2, 1.3):
        trigger = select_audio_trigger(object_detection(t, person=True, banana=True),
                                       t, person_gate, banana_gate)
        if trigger:
            selected.append(trigger)
    assert selected == ['person']
    assert banana_gate.state == 'DETECTING'


def test_banana_triggers_once_when_no_person_then_waits_until_clear():
    person_gate = FoundGate()
    banana_gate = FoundGate(object_attribute='bananas')
    triggers = []
    for t in (1, 1.1, 1.2, 1.3, 3.4, 3.5, 5.0):
        trigger = select_audio_trigger(object_detection(t, banana=True),
                                       t, person_gate, banana_gate)
        if trigger:
            triggers.append(trigger)
    assert triggers == ['banana']
    assert banana_gate.waiting_clear


def test_plushie_plays_its_audio_once_using_teddy_bear_detection():
    person_gate = FoundGate()
    banana_gate = FoundGate(object_attribute='bananas')
    plushie_gate = FoundGate(object_attribute='plushies')
    triggers = []
    for t in (1, 1.1, 1.2, 1.3, 3.4, 3.5, 5.0):
        trigger = select_audio_trigger(object_detection(t, plushie=True),
                                       t, person_gate, banana_gate, plushie_gate)
        if trigger:
            triggers.append(trigger)
    assert triggers == ['plushie']
    assert plushie_gate.waiting_clear


def test_existing_audio_priority_extends_to_plushie_without_overlap():
    person_gate = FoundGate()
    banana_gate = FoundGate(object_attribute='bananas')
    plushie_gate = FoundGate(object_attribute='plushies')
    selected = []
    for t in (1, 1.1, 1.2, 1.3):
        trigger = select_audio_trigger(
            object_detection(t, person=True, banana=True, plushie=True),
            t, person_gate, banana_gate, plushie_gate)
        if trigger:
            selected.append(trigger)
    assert selected == ['person']


def test_banana_suppresses_plushie_when_both_stay_visible():
    person_gate = FoundGate()
    banana_gate = FoundGate(object_attribute='bananas')
    plushie_gate = FoundGate(object_attribute='plushies')
    selected = []
    for t in (1, 1.1, 1.2, 1.3, 3.4, 3.5, 4.0):
        trigger = select_audio_trigger(
            object_detection(t, banana=True, plushie=True),
            t, person_gate, banana_gate, plushie_gate)
        if trigger:
            selected.append(trigger)
    assert selected == ['banana']


def test_confirmation_cooldown_and_fresh_reconfirmation():
    gate = FoundGate()
    for t in (1, 1.1, 1.2):
        assert not gate.update(detection(t), t)
    assert gate.label(1.2).startswith("DETECTING 0.20 / 0.30")
    assert gate.update(detection(1.3), 1.3)
    assert gate.label(1.3) == "COOLDOWN 2.0 sec"
    for t in (1.4, 2, 3.29):
        assert not gate.update(detection(t), t)
    # A result captured inside cooldown cannot count after the cooldown.
    assert not gate.update(detection(3.29), 3.31)
    # Still visible: no further speech, even long after cooldown.
    for t in np.arange(3.4, 10, .07):
        assert not gate.update(detection(t), t)
    assert gate.label(10) == 'FOUND - WAITING FOR CLEAR'
    for t in np.arange(10, 11.2, .07):
        assert not gate.update(detection(t, False), t)
    for t in (11.3, 11.4, 11.5):
        assert not gate.update(detection(t), t)
    assert gate.update(detection(11.6), 11.6)


def test_cooldown_expiry_does_not_trigger_without_person():
    gate = FoundGate()
    for t in (1, 1.1, 1.2, 1.3):
        gate.update(detection(t), t)
    for t in np.arange(3.4, 6, .07):
        assert not gate.update(detection(t, False), t)
    assert gate.state == "SEARCHING"


def test_short_absence_and_camera_loss_do_not_rearm():
    gate = FoundGate()
    for t in (1, 1.1, 1.2, 1.3):
        gate.update(detection(t), t)
    for t in np.arange(3.4, 3.9, .07):
        assert not gate.update(detection(t, False), t)
    assert not gate.update(detection(4), 4)
    for t in np.arange(4.1, 4.8, .07):
        assert not gate.update(detection(t, False), t)
    for t in (5, 6, 7):
        assert not gate.update(replace(detection(t, False), status='STALE'), t)
    for t in np.arange(7.1, 9, .07):
        assert not gate.update(detection(t), t)
    assert gate.waiting_clear


def test_repeated_missing_person_result_cannot_rearm():
    gate = FoundGate()
    for t in (1, 1.1, 1.2, 1.3):
        gate.update(detection(t), t)
    result = detection(3.4, False)
    for t in np.arange(3.4, 5, .07):
        assert not gate.update(result, t)
    assert gate.waiting_clear


def test_one_missing_yolo_result_at_15fps_tolerated():
    gate = FoundGate()
    for t in (1., 1.07, 1.14):
        assert not gate.update(detection(t), t)
    assert not gate.update(detection(1.21, False), 1.21)
    assert not gate.update(detection(1.28), 1.28)
    assert gate.update(detection(1.35), 1.35)


def test_long_dropout_resets_and_low_confidence_is_not_positive():
    gate = FoundGate()
    assert not gate.update(detection(1), 1)
    assert not gate.update(detection(1.1), 1.1)
    assert not gate.update(detection(1.2, confidence=.2), 1.2)
    assert not gate.update(detection(1.3), 1.3)
    assert gate.start == 1.3


@pytest.mark.parametrize("status", ["STALE", "OFF", "ERROR", "WAITING CAMERA"])
def test_yolo_disabled_or_camera_lost_resets_detection(status):
    gate = FoundGate()
    gate.update(detection(1), 1)
    assert not gate.update(replace(detection(1.1), status=status), 1.1)
    assert gate.state == "SEARCHING"


def test_slow_audio_never_overlaps_and_does_not_block_submit():
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Output:
        def play_wav(self, path):
            calls.append(path)
            entered.set()
            release.wait(timeout=3)
        def close(self):
            release.set()
    worker = SingleAudioWorker(Output(), [Path('a.wav'), Path('b.wav')])
    try:
        assert worker.submit()
        assert entered.wait(timeout=2)
        start = time.monotonic()
        assert all(not worker.submit() for _ in range(100))
        assert time.monotonic()-start < .1
        assert len(calls) == 1 and calls[0] in worker.sounds
        gate = FoundGate()
        for i in range(10):
            t = i*.07
            assert not gate.update(detection(t), t, audio_busy=True)
    finally:
        worker.close()
    assert not worker.thread.is_alive()


def test_worker_can_play_one_object_specific_sound():
    played, done = [], threading.Event()
    class Output:
        def play_wav(self, path):
            played.append(path)
            done.set()
    worker = SingleAudioWorker(Output(), [Path('person.wav')])
    try:
        assert worker.submit([Path('banana.wav')])
        assert done.wait(timeout=2)
    finally:
        worker.close()
    assert played == [Path('banana.wav')]


def test_playback_failure_disables_audio_without_exception_to_gui():
    class Output:
        def play_wav(self, path):
            raise RuntimeError('fake device unavailable')
    worker = SingleAudioWorker(Output(), [Path('fake.wav')])
    try:
        assert worker.submit()
        deadline = time.monotonic()+2
        while not worker.error and time.monotonic()<deadline:
            time.sleep(.01)
        assert 'unavailable' in worker.error
        assert not worker.submit()
    finally:
        worker.close()


class RecordingWavOutput:
    def __init__(self):
        self.played = []

    def play_wav(self, path):
        self.played.append(path)

    def close(self):
        pass


def make_reaction_controller(robot=None):
    settings = found_settings_for_test()
    output = RecordingWavOutput()
    selected_robot = robot or MockRobotAdapter()
    controller = FoundReactionController(
        {"person": settings, "banana": settings, "plushie": settings},
        output,
        selected_robot,
        base_reaction=Reaction(
            name="FOUND",
            motion="notice",
            speech="unused",
            speech_delay_seconds=0,
        ),
        cooldown_seconds=2,
    )
    return controller, selected_robot, output


def found_settings_for_test():
    from g1_bottle_reaction.game_vision.found_audio import FoundSettings

    return FoundSettings(.25, .3, .15, 2, (Path("detected.wav"),), "mock", 1)


def wait_reaction(controller):
    deadline = time.monotonic() + 2
    while controller.busy and time.monotonic() < deadline:
        time.sleep(.005)
    assert not controller.busy


def test_confirmed_detection_uses_shared_reaction_engine_once_and_rearms():
    controller, robot, output = make_reaction_controller()
    gate = FoundGate()
    try:
        # Test A: no target never reaches either adapter.
        for t in (0, .1, .2, .3):
            assert not gate.update(detection(t, False), t)
        assert robot.motions == [] and output.played == []

        # Test B/C: confirmation fires one job; continuous frames cannot spam it.
        for t in (1, 1.1, 1.2, 1.3):
            if gate.update(detection(t), t, audio_busy=controller.busy):
                assert controller.trigger("person", t)
        for t in np.arange(1.4, 4.5, .07):
            assert not gate.update(detection(t), t, audio_busy=controller.busy)
        wait_reaction(controller)
        assert isinstance(controller.engine, ReactionEngine)
        assert robot.motions == ["notice"]
        assert output.played == [Path("detected.wav")]

        # Test D: cooldown plus a real absence rearms exactly one later reaction.
        for t in np.arange(4.5, 5.7, .07):
            assert not gate.update(detection(t, False), t)
        for t in (5.8, 5.9, 6.0, 6.1):
            if gate.update(detection(t), t, audio_busy=controller.busy):
                assert controller.trigger("person", t)
        wait_reaction(controller)
        assert robot.motions == ["notice", "notice"]
        assert output.played == [Path("detected.wav"), Path("detected.wav")]
    finally:
        controller.close()


def test_motion_failure_does_not_stop_reaction_worker_and_is_not_retried():
    class FailingRobot(RobotAdapter):
        def __init__(self):
            self.calls = 0

        def play_motion(self, motion):
            self.calls += 1
            raise RuntimeError("simulated G1 action failure")

    failing = FailingRobot()
    controller, _, output = make_reaction_controller(failing)
    try:
        assert controller.trigger("person", 1)
        wait_reaction(controller)
        assert "simulated G1 action failure" in controller.motion_error
        assert output.played == [Path("detected.wav")]
        # A later, distinct event may still play audio, but never retries motion.
        assert controller.trigger("person", 4)
        wait_reaction(controller)
        assert failing.calls == 1
        assert output.played == [Path("detected.wav"), Path("detected.wav")]
        assert controller.engine.last_error is None
    finally:
        controller.close()


class FakeSafeActionClient:
    def __init__(self, results=None):
        self.results = results or {}
        self.execute_calls = []

    def GetActionList(self):
        return 0, {"actions": [{"id": item} for item in (23, 26, 99)]}

    def ExecuteAction(self, action_id):
        self.execute_calls.append(action_id)
        values = self.results.get(action_id)
        return values.pop(0) if values else 0


class FakeSafeActionRuntime:
    def __init__(self, client):
        self.client = client

    def create_arm_action_client(self, *args):
        return self.client


def make_g1_reaction_controller(client, *, sleeper=lambda seconds: None):
    robot = G1RobotAdapter(
        "test-interface",
        enabled=True,
        motion_mode="safe-actions",
        runtime=FakeSafeActionRuntime(client),
        sleeper=sleeper,
    )
    robot.initialize()
    return make_reaction_controller(robot)


@pytest.mark.parametrize("timeout_action", (23, 99))
def test_f_3104_latches_safe_action_motion_for_remainder_of_process(
    timeout_action,
):
    client = FakeSafeActionClient({timeout_action: [ARM_ACTION_RPC_TIMEOUT_CODE]})
    controller, _, output = make_g1_reaction_controller(client)
    try:
        assert controller.trigger("person", 1)
        wait_reaction(controller)
        assert client.execute_calls == [23, 99]
        assert "3104" in controller.motion_error

        assert controller.trigger("person", 4)
        wait_reaction(controller)
        assert client.execute_calls == [23, 99]
        assert output.played == [Path("detected.wav"), Path("detected.wav")]
    finally:
        controller.close()


def test_g_shutdown_cancels_queued_job_without_starting_its_motion():
    entered = threading.Event()
    release = threading.Event()

    class BlockingRobot(RobotAdapter):
        def __init__(self):
            self.notice_calls = 0

        def play_motion(self, motion):
            if motion == "block":
                entered.set()
                release.wait(timeout=2)
            elif motion == "notice":
                self.notice_calls += 1

    robot = BlockingRobot()
    engine = ReactionEngine(
        ReactionConfig(
            cooldown_seconds=0,
            items={
                ReactionEvent.FOUND.value: Reaction("first", "block", "", 0),
                ReactionEvent.NEAR.value: Reaction("queued", "notice", "", 0),
            },
        ),
        robot,
        MuteSpeechBackend(),
    )
    engine.handle(ReactionEvent.FOUND, encounter_count=1, now=1)
    assert entered.wait(timeout=1)
    queued = engine.handle(ReactionEvent.NEAR, encounter_count=1, now=2)
    closer = threading.Thread(
        target=lambda: engine.close(wait=True, cancel_pending=True)
    )
    closer.start()
    assert queued.job.wait(timeout=1)
    release.set()
    closer.join(timeout=2)
    assert not closer.is_alive()
    assert robot.notice_calls == 0


def test_h_shutdown_after_dequeue_cancels_before_motion_boundary():
    before_motion = threading.Event()
    release = threading.Event()

    class Observer:
        def on_accepted(self, job):
            pass

        def before_start(self, job):
            before_motion.set()
            release.wait(timeout=2)

        def on_started(self, job):
            pass

        def on_completed(self, job):
            pass

        def on_failed(self, job, error):
            pass

    client = FakeSafeActionClient()
    robot = G1RobotAdapter(
        "test-interface",
        enabled=True,
        motion_mode="safe-actions",
        runtime=FakeSafeActionRuntime(client),
        sleeper=lambda seconds: None,
    )
    robot.initialize()
    engine = ReactionEngine(
        ReactionConfig(
            cooldown_seconds=0,
            items={ReactionEvent.FOUND.value: Reaction("found", "notice", "", 0)},
        ),
        robot,
        MuteSpeechBackend(),
        lifecycle_observer=Observer(),
    )
    decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=1)
    assert before_motion.wait(timeout=1)
    closer = threading.Thread(
        target=lambda: engine.close(wait=True, cancel_pending=True)
    )
    closer.start()
    deadline = time.monotonic() + 1
    while not engine._shutdown_requested.is_set() and time.monotonic() < deadline:
        time.sleep(.005)
    release.set()
    closer.join(timeout=2)
    assert not closer.is_alive()
    assert decision.job.wait(timeout=1)
    assert client.execute_calls == []


def test_i_shutdown_after_action_23_allows_only_one_release_99():
    action_started = threading.Event()
    allow_result = threading.Event()

    class InFlightActionClient(FakeSafeActionClient):
        def ExecuteAction(self, action_id):
            self.execute_calls.append(action_id)
            if action_id == 23:
                action_started.set()
                allow_result.wait(timeout=2)
                raise RuntimeError("simulated in-flight Action 23 failure")
            return 0

    client = InFlightActionClient()
    controller, _, output = make_g1_reaction_controller(client)
    assert controller.trigger("person", 1)
    assert action_started.wait(timeout=1)
    closer = threading.Thread(target=controller.close)
    closer.start()
    deadline = time.monotonic() + 1
    while not controller.engine._shutdown_requested.is_set() and time.monotonic() < deadline:
        time.sleep(.005)
    allow_result.set()
    closer.join(timeout=2)
    assert not closer.is_alive()
    assert client.execute_calls == [23, 99]
    # Audio is an independent branch and may already have started before the
    # in-flight Action 23 reaches its shutdown cleanup boundary.
    assert output.played == [Path("detected.wav")]


def test_j_normal_safe_action_and_audio_survive_cooldown_rearm():
    client = FakeSafeActionClient()
    controller, _, output = make_g1_reaction_controller(client)
    gate = FoundGate()
    try:
        for t in (1, 1.1, 1.2, 1.3):
            if gate.update(detection(t), t, audio_busy=controller.busy):
                assert controller.trigger("person", t)
        wait_reaction(controller)
        assert client.execute_calls == [23, 99]
        assert output.played == [Path("detected.wav")]

        for t in np.arange(3.5, 4.7, .07):
            assert not gate.update(detection(t, False), t)
        for t in (4.8, 4.9, 5.0, 5.1):
            if gate.update(detection(t), t, audio_busy=controller.busy):
                assert controller.trigger("person", t)
        wait_reaction(controller)
        assert client.execute_calls == [23, 99, 23, 99]
        assert output.played == [Path("detected.wav"), Path("detected.wav")]
    finally:
        controller.close()


def test_status_overlay_only_affects_g1():
    frame = np.full((540,960,3),80,np.uint8)
    states = {k: FrameState(frame=frame, stamp=1, error='') for k in ('g1','usb')}
    before = compose(states, 'dual', 1.1)
    after = compose(states, 'dual', 1.1, found_label='COOLDOWN 1.4 sec')
    assert np.array_equal(before[:,640:],after[:,640:])
    assert not np.array_equal(before[:,:640],after[:,:640])
    assert np.all(frame == 80)


@pytest.mark.parametrize('flag,value', [('--found-duration','0'), ('--found-duration','nan'),
                                      ('--audio-cooldown','-1'), ('--detection-grace','inf'),
                                      ('--rearm-absence','0'), ('--rearm-absence','nan')])
def test_invalid_found_options(flag,value):
    root = Path(__file__).resolve().parents[1]
    args = build_parser().parse_args([flag,value])
    with pytest.raises(ValueError):
        load_settings(args,root)


def test_cli_requires_yolo_and_dual():
    assert main(['--found-audio','--source','dual','--headless']) == 2
    assert main(['--found-audio','--source','usb-lan','--yolo','--headless']) == 2


def test_explicit_existing_wav_and_invalid_path(tmp_path):
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / 'test.wav'
    with wave.open(str(path),'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\0\0'*160)
    args = build_parser().parse_args(['--found-sound',str(path),'--audio-cooldown','2.5'])
    settings = load_settings(args,root)
    assert settings.sounds == (path,) and settings.cooldown == 2.5
    assert settings.duration == .3 and settings.grace == .15
    assert validate_sound(path) == (16000,1,16,.01)
    with pytest.raises(ValueError):
        validate_sound(tmp_path/'missing.wav')


def test_default_audio_paths_are_semantic_and_separate(tmp_path):
    root = Path(__file__).resolve().parents[1]
    # Reaction assets are version-controlled and separated by detected object.
    settings = yaml.safe_load((root/'config/person_found_audio.yaml').read_text())
    assert settings['sound'] == 'assets/audio/reactions/person/detected.wav'
    assert settings['rearm_absence'] == 1.0
    banana_path = tmp_path/'assets/audio/reactions/banana/detected.wav'
    banana_path.parent.mkdir(parents=True)
    with wave.open(str(banana_path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\0\0'*160)
    (tmp_path/'config').mkdir()
    (tmp_path/'config/yolo_objects.yaml').write_text(yaml.safe_dump({
        'banana_found_duration': .3, 'banana_dropout_grace': .15,
        'banana_audio_cooldown': 2., 'banana_rearm_absence': 1.,
        'banana_sound': 'assets/audio/reactions/banana/detected.wav',
        'plushie_found_duration': .3, 'plushie_dropout_grace': .15,
        'plushie_audio_cooldown': 2., 'plushie_rearm_absence': 1.,
        'plushie_sound': 'assets/audio/reactions/plushie/plushie_affectionate.wav',
        'quiet_mode_gain_db': -30.0}))
    banana = load_banana_settings(tmp_path, .25, 'g1')
    assert banana.sounds == (banana_path.resolve(),)
    assert banana.confidence == .25 and banana.output == 'g1'
    plushie_path = tmp_path/'assets/audio/reactions/plushie/plushie_affectionate.wav'
    plushie_path.parent.mkdir(parents=True)
    with wave.open(str(plushie_path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\0\0'*160)
    plushie = load_plushie_settings(tmp_path, .25, 'g1')
    assert plushie.sounds == (plushie_path.resolve(),)
    assert plushie.confidence == .25 and plushie.output == 'g1'
    assert plushie.rearm_absence == 1.0


@pytest.mark.skipif(os.name != 'posix', reason='G1 audio pipe helper is Linux-only')
def test_g1_audio_reuses_one_process_for_multiple_sounds(tmp_path):
    from g1_bottle_reaction.adapters.cached_audio import G1CachedOutput
    tools = tmp_path/'tools'
    tools.mkdir()
    (tools/'g1_cached_sound.py').write_text(
        'import sys, json\nprint(json.dumps({"status":"ready"}),flush=True)\n'
        'for line in sys.stdin:\n print(json.dumps({"status":"played"}),flush=True)\n')
    path = tmp_path/'voice.wav'
    with wave.open(str(path),'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\0\0'*160)
    output = G1CachedOutput(tmp_path)
    try:
        pid = output.process.pid
        output.play_wav(path)
        output.play_wav(path)
        assert output.ready and output.process.pid == pid
        assert output.process.poll() is None
    finally:
        output.close()
    assert output.process.poll() is not None
