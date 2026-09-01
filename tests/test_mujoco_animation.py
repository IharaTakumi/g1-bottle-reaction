from __future__ import annotations

import importlib

import pytest

from g1_bottle_reaction.adapters import mujoco_robot
from g1_bottle_reaction.adapters.mujoco_robot import compose_tracking_yaw
from g1_bottle_reaction.simulation.motion import (
    AnimationPlayer,
    AnimationState,
    MotionAnimation,
    MotionKeyframe,
    default_motion_config_path,
    load_motion_library,
    sanitize_pose,
)


EXPECTED_MOTIONS = (
    "stand",
    "notice",
    "wave_hand",
    "reach_forward",
    "guard",
    "look_around",
    "surprise",
    "little_dance",
    "spot_target",
)


def test_mujoco_adapter_import_does_not_import_mujoco() -> None:
    module = importlib.import_module("g1_bottle_reaction.adapters.mujoco_robot")
    assert module.MujocoRobotAdapter is not None


def test_motion_config_load_and_name_mapping() -> None:
    library = load_motion_library(default_motion_config_path())
    assert library.names == EXPECTED_MOTIONS
    assert library.animation("little_dance").duration_seconds == pytest.approx(3.4)
    with pytest.raises(KeyError, match="Unknown motion"):
        library.animation("moonwalk")


def test_smoothstep_interpolation() -> None:
    animation = MotionAnimation(
        "test",
        (
            MotionKeyframe(0.0, {"joint": 0.0}),
            MotionKeyframe(2.0, {"joint": 2.0}),
        ),
    )
    assert animation.sample(0.5, baseline={})["joint"] == pytest.approx(0.3125)
    assert animation.sample(1.0, baseline={})["joint"] == pytest.approx(1.0)
    assert animation.sample(1.5, baseline={})["joint"] == pytest.approx(1.6875)


def test_sparse_keyframe_interpolates_missing_joint_to_stand() -> None:
    animation = MotionAnimation(
        "sparse",
        (
            MotionKeyframe(0.0, {}),
            MotionKeyframe(1.0, {"joint": 1.0}),
            MotionKeyframe(2.0, {}),
        ),
    )
    assert animation.sample(0.5, baseline={})["joint"] == pytest.approx(0.5)
    assert animation.sample(1.5, baseline={})["joint"] == pytest.approx(0.5)


def test_unknown_joint_is_ignored_and_range_is_clamped() -> None:
    warnings: list[str] = []
    pose = sanitize_pose(
        {"known": 2.0, "free": -3.0, "missing": 0.5},
        {"known": (-1.0, 1.0), "free": None},
        warn=warnings.append,
    )
    assert pose == {"known": 1.0, "free": -3.0}
    assert any("missing" in message and "ignored" in message for message in warnings)
    assert any("known" in message and "clamped" in message for message in warnings)


def test_animation_player_returns_to_ready_state() -> None:
    library = load_motion_library(default_motion_config_path())
    player = AnimationPlayer(library)
    player.start("notice", now=10.0)
    assert player.state is AnimationState.PLAYING
    moving_pose, completed = player.update(now=10.3)
    assert moving_pose
    assert completed is None
    stand_pose, completed = player.update(now=11.1)
    assert stand_pose == library.stand_pose
    assert completed == "notice"
    assert player.state is AnimationState.READY
    assert player.current_motion is None


def test_optional_dependency_missing_message(monkeypatch) -> None:
    real_import = mujoco_robot.importlib.import_module

    def missing(name: str):
        if name == "mujoco":
            raise ImportError("not installed")
        return real_import(name)

    monkeypatch.setattr(mujoco_robot.importlib, "import_module", missing)
    with pytest.raises(RuntimeError, match=r"\[sim\]"):
        mujoco_robot._import_mujoco()


def test_motion_and_tracking_yaw_are_composed_and_clamped() -> None:
    limits = {"waist_yaw_joint": (-0.5, 0.5)}
    assert compose_tracking_yaw(
        {"waist_yaw_joint": 0.2}, attention_yaw=0.1, joint_limits=limits
    ) == pytest.approx(0.3)
    assert compose_tracking_yaw(
        {"waist_yaw_joint": 0.4}, attention_yaw=0.3, joint_limits=limits
    ) == pytest.approx(0.5)


def test_missing_tracking_joint_is_safe_no_op() -> None:
    assert compose_tracking_yaw({}, attention_yaw=0.2, joint_limits={}) is None
