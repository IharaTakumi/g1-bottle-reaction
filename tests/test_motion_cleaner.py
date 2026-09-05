from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from g1_bottle_reaction.motion import (
    UPPER_BODY_JOINT_NAMES,
    clean_upper_body_motion,
    load_motion_cleaner_config,
)
from g1_bottle_reaction.motion.g1_joints import (
    G1_29DOF_JOINT_NAMES,
    G1_UPPER_BODY_JOINT_LIMITS,
    motion_group,
)


def _synthetic_gmr_motion() -> np.ndarray:
    times = np.linspace(0.0, 1.0, 31)
    positions = np.zeros((len(times), len(G1_29DOF_JOINT_NAMES)))
    for column in range(12, positions.shape[1]):
        positions[:, column] = 0.6 * np.sin(np.pi * times)
    positions[15, G1_29DOF_JOINT_NAMES.index("waist_roll_joint")] = 10.0
    return positions


def test_cleaner_selects_names_and_enforces_upper_body_safety() -> None:
    config = load_motion_cleaner_config()
    original = _synthetic_gmr_motion()
    permutation = np.random.default_rng(7).permutation(original.shape[1])
    shuffled = original[:, permutation]
    names = tuple(G1_29DOF_JOINT_NAMES[index] for index in permutation)

    asset, report = clean_upper_body_motion(
        shuffled,
        names,
        source_fps=30.0,
        name="synthetic",
        config=config,
    )

    assert asset.joint_names == UPPER_BODY_JOINT_NAMES
    assert asset.fps == 50.0
    assert asset.positions.shape[1] == 17
    assert report.source_limit_clips > 0
    np.testing.assert_allclose(asset.positions[0], 0.0)
    np.testing.assert_allclose(asset.positions[-1], 0.0)
    assert np.isfinite(asset.positions).all()
    velocity = np.abs(np.diff(asset.positions, axis=0)) * asset.fps
    for column, joint_name in enumerate(asset.joint_names):
        group = motion_group(joint_name)
        assert velocity[:, column].max() <= config.max_velocity_rad_s[group] + 1e-5
        assert (
            np.abs(asset.positions[:, column]).max()
            <= config.max_offset_rad[group] + 1e-6
        )
        reference = asset.metadata["source_reference_positions_rad"][joint_name]
        low, high = G1_UPPER_BODY_JOINT_LIMITS[joint_name]
        absolute = reference + asset.positions[:, column]
        assert absolute.min() >= low + config.joint_margin_rad - 1e-6
        assert absolute.max() <= high - config.joint_margin_rad + 1e-6


def test_cleaner_rejects_missing_upper_joint() -> None:
    config = load_motion_cleaner_config()
    names = list(G1_29DOF_JOINT_NAMES)
    names.remove("right_wrist_yaw_joint")
    positions = np.zeros((3, len(names)))

    with pytest.raises(ValueError, match="right_wrist_yaw_joint"):
        clean_upper_body_motion(
            positions,
            names,
            source_fps=30,
            name="missing",
            config=config,
        )


def test_config_rejects_unknown_format_version(tmp_path: Path) -> None:
    config_path = tmp_path / "bad.yaml"
    config_path.write_text("format_version: 99\n", encoding="utf-8")

    with pytest.raises(ValueError, match="format_version"):
        load_motion_cleaner_config(config_path)
