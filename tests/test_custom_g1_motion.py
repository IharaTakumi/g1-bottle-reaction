from __future__ import annotations

from dataclasses import replace
import threading

import pytest

from g1_bottle_reaction.adapters.g1_robot import G1RobotAdapter
from g1_bottle_reaction.adapters.g1_robot import UnitreeArmSdkTransport
from g1_bottle_reaction.adapters.mock_robot import MockRobotAdapter
from g1_bottle_reaction.adapters.speech import ConsoleSpeechBackend
from g1_bottle_reaction.custom_motion import (
    CustomArmMotionController,
    MotionOwnership,
    build_relative_trajectory,
    format_dry_run,
    load_custom_motion_config,
)
from g1_bottle_reaction.reactions.engine import ReactionEngine


class FakeTransport:
    def __init__(self, pose=None, *, fail_at: int | None = None) -> None:
        self.pose = pose
        self.fail_at = fail_at
        self.writes: list[tuple[dict[str, float], float]] = []
        self.entered = threading.Event()
        self.release_wait = threading.Event()

    def wait_for_pose(self, joint_indices, timeout_seconds):
        del joint_indices, timeout_seconds
        self.entered.set()
        if self.release_wait.is_set():
            self.release_wait.wait(1)
        return self.pose

    def write(self, positions, joint_indices, *, weight, kp, kd):
        del joint_indices, kp, kd
        self.writes.append((dict(positions), weight))
        if self.fail_at is not None and len(self.writes) == self.fail_at:
            self.fail_at = None
            raise OSError("DDS write failed")


def _base(config, value=0.0):
    return {name: value for name in config.joints}


def test_current_pose_relative_offsets_and_small_scaling() -> None:
    config = load_custom_motion_config()
    base = {
        "right_shoulder_pitch": 0.4,
        "right_shoulder_roll": -0.2,
        "right_elbow": 0.7,
    }
    report = build_relative_trajectory(config, "custom_notice", "small", base)
    peak = min(report.samples, key=lambda item: abs(item.time_seconds - 0.20))
    assert peak.positions["right_shoulder_pitch"] == pytest.approx(0.36)
    assert peak.positions["right_shoulder_roll"] == pytest.approx(-0.23)
    assert peak.positions["right_elbow"] == pytest.approx(0.75)
    assert report.samples[0].positions == base
    assert report.samples[-1].positions == base


def test_interpolation_dt_and_max_step_delta() -> None:
    config = load_custom_motion_config()
    report = build_relative_trajectory(config, "custom_notice", "demo", _base(config))
    assert report.samples[1].time_seconds == pytest.approx(0.02)
    assert report.duration_seconds == pytest.approx(1.15)
    assert report.max_delta_per_step <= config.max_delta_per_step_radians + 1e-12
    assert report.samples[5].positions["right_elbow"] < report.samples[10].positions["right_elbow"]


def test_joint_clamp_keeps_software_margin() -> None:
    config = load_custom_motion_config()
    base = _base(config)
    pitch = config.joints["right_shoulder_pitch"]
    base["right_shoulder_pitch"] = pitch.minimum + config.safety_margin_radians + 0.01
    report = build_relative_trajectory(config, "custom_notice", "demo", base)
    assert report.target_minimums["right_shoulder_pitch"] >= (
        pitch.minimum + config.safety_margin_radians - 1e-12
    )


def test_only_verified_right_arm_joints_are_configured() -> None:
    config = load_custom_motion_config()
    assert set(config.joints) == {
        "right_shoulder_pitch",
        "right_shoulder_roll",
        "right_elbow",
    }
    assert all("waist" not in name and "hip" not in name for name in config.joints)


def test_missing_current_joint_is_rejected() -> None:
    config = load_custom_motion_config()
    with pytest.raises(RuntimeError, match="missing joints"):
        build_relative_trajectory(
            config, "custom_notice", "small", {"right_elbow": 0.0}
        )


def test_unsafe_or_nonfinite_base_pose_is_rejected() -> None:
    config = load_custom_motion_config()
    unsafe = _base(config)
    unsafe["right_elbow"] = config.joints["right_elbow"].maximum
    with pytest.raises(RuntimeError, match="safety envelope"):
        build_relative_trajectory(config, "custom_notice", "small", unsafe)
    nonfinite = _base(config)
    nonfinite["right_elbow"] = float("nan")
    with pytest.raises(RuntimeError, match="not finite"):
        build_relative_trajectory(config, "custom_notice", "small", nonfinite)


def test_missing_low_state_fails_without_command() -> None:
    config = load_custom_motion_config()
    transport = FakeTransport(None)
    controller = CustomArmMotionController(
        config, transport, MotionOwnership(), sleep=lambda _: None
    )
    assert controller.start("custom_notice", "small")
    with pytest.raises(RuntimeError, match="LowState"):
        controller.wait(1)
    assert transport.writes == []


def test_controller_releases_control_after_motion() -> None:
    config = load_custom_motion_config()
    transport = FakeTransport(_base(config))
    controller = CustomArmMotionController(
        config, transport, MotionOwnership(), sleep=lambda _: None
    )
    assert controller.start("custom_notice", "small")
    assert controller.wait(1)
    assert transport.writes[-1][1] == pytest.approx(0.0)
    assert transport.writes[-1][0] == _base(config)


def test_exception_stops_trajectory_and_attempts_cleanup_release() -> None:
    config = load_custom_motion_config()
    transport = FakeTransport(_base(config), fail_at=2)
    controller = CustomArmMotionController(
        config, transport, MotionOwnership(), sleep=lambda _: None
    )
    assert controller.start("custom_notice", "small")
    with pytest.raises(RuntimeError, match="DDS write failed"):
        controller.wait(1)
    assert transport.writes[-1][1] == pytest.approx(0.0)


def test_busy_and_preset_ownership_reject_custom_motion() -> None:
    config = load_custom_motion_config()
    ownership = MotionOwnership()
    assert ownership.acquire("preset")
    controller = CustomArmMotionController(
        config, FakeTransport(_base(config)), ownership, sleep=lambda _: None
    )
    assert not controller.start("custom_notice", "small")
    ownership.release("preset")


def test_busy_controller_rejects_second_motion() -> None:
    config = load_custom_motion_config()
    gate = threading.Event()

    class BlockingTransport(FakeTransport):
        def wait_for_pose(self, joint_indices, timeout_seconds):
            del joint_indices, timeout_seconds
            self.entered.set()
            gate.wait(1)
            return self.pose

    transport = BlockingTransport(_base(config))
    controller = CustomArmMotionController(
        config, transport, MotionOwnership(), sleep=lambda _: None
    )
    assert controller.start("custom_notice", "small")
    assert transport.entered.wait(1)
    assert not controller.start("custom_notice", "small")
    gate.set()
    assert controller.wait(1)


def test_unitree_transport_uses_only_verified_joints_and_weight_slot() -> None:
    published = []
    subscriber_holder = {}

    class Motor:
        def __init__(self):
            self.q = 0.0
            self.dq = 0.0
            self.kp = 0.0
            self.kd = 0.0
            self.tau = 0.0

    class Command:
        def __init__(self):
            self.motor_cmd = [Motor() for _ in range(30)]
            self.crc = 0

    class Publisher:
        def __init__(self, topic, message_type):
            assert topic == "rt/arm_sdk"
            del message_type

        def Init(self):
            pass

        def Write(self, command):
            published.append(command)

    class Subscriber:
        def __init__(self, topic, message_type):
            assert topic == "rt/lowstate"
            del message_type

        def Init(self, callback, depth):
            assert depth == 10
            subscriber_holder["callback"] = callback

    class CRC:
        def Crc(self, command):
            del command
            return 123

    transport = UnitreeArmSdkTransport(
        Publisher, Subscriber, object, object, Command, CRC
    )
    state = type("State", (), {})()
    state.motor_state = [Motor() for _ in range(30)]
    state.motor_state[22].q = 0.1
    state.motor_state[23].q = 0.2
    state.motor_state[25].q = 0.3
    subscriber_holder["callback"](state)
    indices = {
        "right_shoulder_pitch": 22,
        "right_shoulder_roll": 23,
        "right_elbow": 25,
    }
    assert transport.wait_for_pose(indices, 0.1) == {
        "right_shoulder_pitch": 0.1,
        "right_shoulder_roll": 0.2,
        "right_elbow": 0.3,
    }
    transport.write(
        {name: 0.4 for name in indices}, indices, weight=1.0, kp=60.0, kd=1.5
    )
    command = published[0]
    assert command.motor_cmd[29].q == 1.0
    assert {index for index, motor in enumerate(command.motor_cmd) if motor.kp} == {
        22,
        23,
        25,
    }
    assert command.crc == 123


def test_custom_real_robot_safety_gate() -> None:
    config = load_custom_motion_config()
    with pytest.raises(RuntimeError, match="safe-actions gates"):
        G1RobotAdapter(
            "eth0",
            enabled=True,
            motion_mode="disabled",
            custom_motion_enabled=True,
            custom_motion_config=config,
        )


def test_reaction_timeline_uses_common_start_clock(app_config) -> None:
    class Clock:
        value = 10.0

        def now(self):
            return self.value

        def sleep(self, seconds):
            self.value += seconds

    class TimedRobot(MockRobotAdapter):
        starts: list[float]

        def __init__(self):
            super().__init__()
            self.starts = []

        def play_motion_timed(self, motion, *, timeline_start, timing_debug=False):
            del timing_debug
            self.starts.append(timeline_start)
            self.play_motion(motion)
            return True

    class Speech(ConsoleSpeechBackend):
        def __init__(self):
            self.called_at = None

        def speak(self, text, *, voice_profile="neutral"):
            del text, voice_profile
            self.called_at = clock.now()

    clock = Clock()
    robot = TimedRobot()
    speech = Speech()
    reaction = replace(
        app_config.reaction.items["SUSPICION_STARTED"],
        motion="custom_notice",
        speech="ん？",
        speech_delay_seconds=0.25,
    )
    engine = ReactionEngine(
        replace(
            app_config.reaction,
            items={**app_config.reaction.items, "SUSPICION_STARTED": reaction},
        ),
        robot,
        speech,
        sleep=clock.sleep,
        clock=clock.now,
        start_worker=False,
    )
    engine.execute(reaction)
    assert robot.starts == [10.0]
    assert speech.called_at == pytest.approx(10.25)


def test_dry_run_format_requires_no_transport() -> None:
    config = load_custom_motion_config()
    report = build_relative_trajectory(config, "custom_notice", "small", _base(config))
    output = format_dry_run(report, config)
    assert "NO G1 COMMANDS" in output
    assert "Joint range validation: OK" in output
    assert "Number of samples: 59" in output


def test_module_import_requires_no_unitree_sdk() -> None:
    assert load_custom_motion_config().motions["custom_notice"] is not None
