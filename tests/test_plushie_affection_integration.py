from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
import time
import wave

import numpy as np
import pytest

from g1_bottle_reaction.adapters.robot import RobotAdapter
from g1_bottle_reaction.game_vision.app import build_parser
from g1_bottle_reaction.game_vision.dual import validate_args
from g1_bottle_reaction.game_vision.found_audio import (
    AttenuatedWavOutput,
    FoundGate,
    FoundReactionController,
    FoundSettings,
    load_plushie_settings,
    load_quiet_gain_db,
    select_audio_trigger,
    select_plushie_only_trigger,
)
from g1_bottle_reaction.game_vision.person_yolo import (
    Banana,
    Detection,
    Person,
    Plushie,
)
from g1_bottle_reaction.reactions.models import Reaction
from g1_bottle_reaction.state.events import ReactionEvent


def plushie_detection(stamp: float, visible: bool = True) -> Detection:
    plushies = (Plushie((1, 2, 20, 30), 0.9),) if visible else ()
    return Detection(plushies=plushies, stamp=stamp, status="RUNNING")


class RecordingOutput:
    def __init__(self) -> None:
        self.played: list[Path] = []
        self.started = threading.Event()

    def play_wav(self, path: Path) -> None:
        self.played.append(Path(path))
        self.started.set()

    def close(self) -> None:
        pass


class BlockingRobot(RobotAdapter):
    def __init__(self) -> None:
        self.motions: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def play_motion(self, motion: str) -> None:
        self.motions.append(motion)
        self.started.set()
        assert self.release.wait(timeout=2)


class RecordingRobot(RobotAdapter):
    def __init__(self) -> None:
        self.motions: list[str] = []

    def play_motion(self, motion: str) -> None:
        self.motions.append(motion)


def settings() -> FoundSettings:
    return FoundSettings(
        confidence=0.25,
        duration=0.3,
        grace=0.3,
        cooldown=2.0,
        sounds=(Path("plushie_affectionate.wav"),),
        output="mock",
        rearm_absence=1.0,
    )


def controller(robot: RobotAdapter, output: RecordingOutput) -> FoundReactionController:
    configured = settings()
    return FoundReactionController(
        {
            "person": replace(configured, sounds=(Path("person.wav"),)),
            "banana": replace(configured, sounds=(Path("banana.wav"),)),
            "plushie": configured,
        },
        output,
        robot,
        base_reaction=Reaction("FOUND", "notice", "unused", 0.3),
        cooldown_seconds=0.0,
        motion_overrides={
            "person": "motiondecode:found",
            "banana": "motiondecode:surprise",
            "plushie": "motiondecode:joy",
        },
        speech_delay_overrides={"person": 0.0, "banana": 0.0, "plushie": 0.0},
    )


def wait_idle(subject: FoundReactionController) -> None:
    deadline = time.monotonic() + 2
    while subject.busy and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not subject.busy


def test_plushie_confirmation_hold_rearm_and_second_confirmation() -> None:
    gate = FoundGate(
        duration=0.3,
        grace=0.3,
        cooldown=2.0,
        confidence=0.25,
        rearm_absence=1.0,
        object_attribute="plushies",
    )

    # One frame is a false positive, not a confirmed event.
    assert not gate.update(plushie_detection(1.0), 1.0)
    assert not gate.update(plushie_detection(1.4, False), 1.4)

    # Stable presence confirms exactly once.
    triggers = 0
    for stamp in (2.0, 2.1, 2.2, 2.3):
        triggers += int(gate.update(plushie_detection(stamp), stamp))
    assert triggers == 1
    for stamp in np.arange(2.4, 5.0, 0.07):
        assert not gate.update(plushie_detection(float(stamp)), float(stamp))

    # A full second of fresh negative frames rearms, then 300 ms reconfirms.
    for stamp in np.arange(5.0, 6.2, 0.07):
        assert not gate.update(plushie_detection(float(stamp), False), float(stamp))
    assert not gate.waiting_clear
    second = 0
    for stamp in (6.3, 6.4, 6.5, 6.6):
        second += int(gate.update(plushie_detection(stamp), stamp))
    assert second == 1


def test_sparse_wireless_frames_still_require_two_fresh_positive_samples() -> None:
    gate = FoundGate(
        duration=0.3,
        grace=1.0,
        cooldown=2.0,
        confidence=0.25,
        rearm_absence=1.0,
        object_attribute="plushies",
    )
    first = plushie_detection(1.0)
    assert not gate.update(first, 1.0)
    # Replaying the same result for longer than confirmation never fires.
    assert not gate.update(first, 1.4)
    assert not gate.update(first, 1.55)
    # A distinct positive source frame proves persistence and can confirm.
    assert gate.update(plushie_detection(1.6), 1.6)


def test_hold_for_twenty_seconds_does_not_rearm_on_overlapping_misclassification() -> None:
    gate = FoundGate(
        duration=0.3,
        grace=1.0,
        cooldown=2.0,
        confidence=0.25,
        rearm_absence=3.0,
        object_attribute="plushies",
        absence_blocking_attributes=("people",),
        absence_overlap=0.1,
    )
    for stamp in (1.0, 1.2, 1.4):
        gate.update(plushie_detection(stamp), stamp)
    assert gate.waiting_clear
    for stamp in np.arange(3.5, 23.6, 0.25):
        result = Detection(
            people=(Person((0, 0, 25, 35), 0.9),),
            stamp=float(stamp),
            status="RUNNING",
        )
        assert not gate.update(result, float(stamp))
    assert gate.waiting_clear

    # Fresh, explicit empty-scene observations rearm within a few seconds.
    for stamp in np.arange(24.0, 27.25, 0.25):
        assert not gate.update(plushie_detection(float(stamp), False), float(stamp))
    assert not gate.waiting_clear

    second = 0
    for stamp in (27.5, 27.7, 27.9):
        second += int(gate.update(plushie_detection(stamp), stamp))
    assert second == 1


def test_rearm_does_not_advance_without_fresh_running_yolo_observations() -> None:
    gate = FoundGate(
        duration=0.3,
        grace=1.0,
        cooldown=2.0,
        confidence=0.25,
        rearm_absence=3.0,
        object_attribute="plushies",
    )
    for stamp in (1.0, 1.2, 1.4):
        gate.update(plushie_detection(stamp), stamp)
    assert gate.waiting_clear

    first_absent = plushie_detection(3.5, False)
    assert not gate.update(first_absent, 3.5)
    assert not gate.update(first_absent, 10.0)  # replayed source observation
    stale = Detection(stamp=10.1, status="STALE")
    assert not gate.update(stale, 10.1)
    assert gate.waiting_clear

    for stamp in np.arange(11.0, 14.25, 0.25):
        assert not gate.update(plushie_detection(float(stamp), False), float(stamp))
    assert not gate.waiting_clear


def test_small_unrelated_person_does_not_prevent_explicit_absence_rearm() -> None:
    gate = FoundGate(
        duration=0.3,
        grace=1.0,
        cooldown=2.0,
        confidence=0.25,
        rearm_absence=3.0,
        object_attribute="plushies",
        absence_blocking_attributes=("people",),
        absence_overlap=0.1,
    )
    for stamp in (1.0, 1.2, 1.4):
        gate.update(plushie_detection(stamp), stamp)
    for stamp in np.arange(3.5, 6.75, 0.25):
        result = Detection(
            people=(Person((100, 100, 110, 110), 0.9),),
            stamp=float(stamp),
            status="RUNNING",
        )
        assert not gate.update(result, float(stamp))
    assert not gate.waiting_clear


def test_plushie_only_mode_is_not_inhibited_by_person_boxes() -> None:
    gate = FoundGate(
        duration=0.3,
        grace=1.0,
        cooldown=2.0,
        confidence=0.25,
        rearm_absence=1.0,
        object_attribute="plushies",
    )
    for stamp in (1.0, 1.2):
        result = Detection(
            people=(Person((1, 2, 20, 30), 0.9),),
            plushies=(Plushie((30, 40, 80, 100), 0.9),),
            stamp=stamp,
            status="RUNNING",
        )
        assert not select_plushie_only_trigger(result, stamp, gate)
    result = Detection(
        people=(Person((1, 2, 20, 30), 0.9),),
        plushies=(Plushie((30, 40, 80, 100), 0.9),),
        stamp=1.4,
        status="RUNNING",
    )
    assert select_plushie_only_trigger(result, 1.4, gate) == "plushie"


def test_reaction_target_defaults_to_all_and_uses_prop_first_priority() -> None:
    parser = build_parser()
    assert parser.parse_args([]).reaction_target == "all"
    person_gate = FoundGate()
    banana_gate = FoundGate(object_attribute="bananas")
    plushie_gate = FoundGate(object_attribute="plushies")
    selected = []
    for stamp in (1.0, 1.1, 1.2, 1.3):
        result = Detection(
            people=(Person((1, 2, 20, 30), 0.9),),
            bananas=(Banana((2, 3, 22, 33), 0.9),),
            plushies=(Plushie((3, 4, 23, 34), 0.9),),
            stamp=stamp,
            status="RUNNING",
        )
        trigger = select_audio_trigger(
            result, stamp, person_gate, banana_gate, plushie_gate
        )
        if trigger:
            selected.append(trigger)
    assert selected == ["plushie"]


@pytest.mark.parametrize("target", ["person", "banana", "plushie"])
def test_reaction_target_isolates_each_object(target: str) -> None:
    person_gate = FoundGate()
    banana_gate = FoundGate(object_attribute="bananas")
    plushie_gate = FoundGate(object_attribute="plushies")
    selected = []
    for stamp in (1.0, 1.1, 1.2, 1.3):
        result = Detection(
            people=(Person((1, 2, 20, 30), 0.9),),
            bananas=(Banana((2, 3, 22, 33), 0.9),),
            plushies=(Plushie((3, 4, 23, 34), 0.9),),
            stamp=stamp,
            status="RUNNING",
        )
        trigger = select_audio_trigger(
            result,
            stamp,
            person_gate,
            banana_gate,
            plushie_gate,
            reaction_target=target,
        )
        if trigger:
            selected.append(trigger)
    assert selected == [target]
    gates = {
        "person": person_gate,
        "banana": banana_gate,
        "plushie": plushie_gate,
    }
    assert all(gate.state == "SEARCHING" for name, gate in gates.items()
               if name != target)


def test_camera_lost_cannot_create_a_plushie_reaction() -> None:
    person_gate = FoundGate()
    banana_gate = FoundGate(object_attribute="bananas")
    plushie_gate = FoundGate(object_attribute="plushies")
    for stamp in (1.0, 1.2, 1.4, 1.6):
        lost = Detection(
            plushies=(Plushie((3, 4, 23, 34), 0.9),),
            stamp=stamp,
            status="WAITING CAMERA",
        )
        assert select_audio_trigger(
            lost,
            stamp,
            person_gate,
            banana_gate,
            plushie_gate,
            reaction_target="plushie",
        ) is None
    assert plushie_gate.state == "SEARCHING"


def test_three_target_motion_mapping_and_zero_speech_delay() -> None:
    robot = RecordingRobot()
    output = RecordingOutput()
    subject = controller(robot, output)
    try:
        for index, target in enumerate(("person", "banana", "plushie"), start=1):
            assert subject.trigger(target, float(index))
            wait_idle(subject)
        assert robot.motions == [
            "motiondecode:found",
            "motiondecode:surprise",
            "motiondecode:joy",
        ]
        assert output.played == [
            Path("person.wav"),
            Path("banana.wav"),
            Path("plushie_affectionate.wav"),
        ]
        for event in (
            ReactionEvent.YOLO_PERSON_FOUND,
            ReactionEvent.YOLO_BANANA_FOUND,
            ReactionEvent.YOLO_PLUSHIE_FOUND,
        ):
            assert subject.engine.config.items[event.value].speech_delay_seconds == 0.0
    finally:
        subject.close()


def test_motion_and_audio_start_in_parallel_and_busy_events_drop() -> None:
    robot = BlockingRobot()
    output = RecordingOutput()
    subject = controller(robot, output)
    try:
        assert subject.trigger("plushie", 1.0)
        assert robot.started.wait(timeout=1)
        assert output.started.wait(timeout=1)
        assert subject.busy
        assert not subject.trigger("plushie", 2.0)
        assert robot.motions == ["motiondecode:joy"]
        assert output.played == [Path("plushie_affectionate.wav")]
        robot.release.set()
        wait_idle(subject)
    finally:
        robot.release.set()
        subject.close()


def test_quiet_mode_attenuates_only_playback_copy_by_configured_24_db(
    tmp_path: Path,
) -> None:
    source = tmp_path / "plushie_affectionate.wav"
    samples = np.full(1600, 20_000, dtype="<i2")
    with wave.open(str(source), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(samples.tobytes())
    original = source.read_bytes()

    class CapturingOutput:
        def __init__(self) -> None:
            self.samples = None
            self.path = None

        def play_wav(self, path: Path) -> None:
            self.path = Path(path)
            with wave.open(str(path), "rb") as stream:
                self.samples = np.frombuffer(stream.readframes(stream.getnframes()), "<i2")

        def close(self) -> None:
            pass

    delegate = CapturingOutput()
    root = Path(__file__).resolve().parents[1]
    gain_db = load_quiet_gain_db(root)
    quiet = AttenuatedWavOutput(delegate, gain_db)
    try:
        quiet.play_wav(source)
        assert delegate.path != source
        assert np.max(np.abs(delegate.samples)) / 20_000 == pytest.approx(
            10 ** (gain_db / 20), abs=0.0001
        )
        assert source.read_bytes() == original
    finally:
        quiet.close()


def test_quiet_mode_off_preserves_existing_path_and_cli_default() -> None:
    output = RecordingOutput()
    source = Path("plushie_affectionate.wav")
    output.play_wav(source)
    assert output.played == [source]
    parser = build_parser()
    assert parser.parse_args([]).quiet_mode is False
    assert parser.parse_args(["--quiet-mode"]).quiet_mode is True


def test_commissioning_quiet_gain_is_minus_24_db() -> None:
    root = Path(__file__).resolve().parents[1]
    assert load_quiet_gain_db(root) == -24.0


def test_commissioning_plushie_rearm_uses_three_seconds_explicit_absence() -> None:
    root = Path(__file__).resolve().parents[1]
    configured = load_plushie_settings(root, 0.25, "mock")
    assert configured.rearm_absence == 3.0
    assert configured.absence_blocking_overlap == 0.1


def test_real_motiondecode_requires_attended_gate_but_quiet_mode_is_optional() -> None:
    parser = build_parser()
    base = [
        "--source",
        "dual",
        "--yolo",
        "--found-audio",
        "--robot",
        "motiondecode",
        "--enable-real-robot",
    ]
    with pytest.raises(ValueError, match="confirm-site-ready"):
        validate_args(parser.parse_args(base))
    validate_args(parser.parse_args(base + ["--confirm-site-ready"]))
    validate_args(parser.parse_args(base + ["--confirm-site-ready", "--quiet-mode"]))


def test_hackathon_joy_cli_gate_is_explicit_and_plushie_scoped() -> None:
    parser = build_parser()
    base = [
        "--source", "dual", "--yolo", "--found-audio",
        "--robot", "motiondecode", "--enable-real-robot",
        "--confirm-site-ready",
    ]
    assert parser.parse_args(base).allow_hackathon_joy is False
    validate_args(parser.parse_args(
        base + ["--reaction-target", "plushie", "--allow-hackathon-joy"]
    ))
    validate_args(parser.parse_args(
        base + ["--reaction-target", "all", "--allow-hackathon-joy"]
    ))
    with pytest.raises(ValueError, match="plushie-capable"):
        validate_args(parser.parse_args(
            base + ["--reaction-target", "person", "--allow-hackathon-joy"]
        ))
