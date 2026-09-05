from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from g1_bottle_reaction.motion import MotionAsset, load_motion_asset, save_motion_asset


def test_motion_asset_round_trip_uses_relative_non_pickle_format(
    tmp_path: Path,
) -> None:
    positions = np.asarray([[0.0, 0.0], [0.1, -0.2], [0.0, 0.0]])
    source = MotionAsset(
        name="surprised",
        fps=50.0,
        joint_names=("joint_a", "joint_b"),
        positions=positions,
        metadata={"source": "synthetic"},
    )

    path = save_motion_asset(source, tmp_path / "surprised.npz")
    loaded = load_motion_asset(path)

    assert loaded.name == source.name
    assert loaded.position_mode == "relative_to_runtime_start"
    assert loaded.duration == pytest.approx(0.04)
    assert loaded.joint_names == source.joint_names
    assert loaded.metadata == source.metadata
    np.testing.assert_allclose(loaded.positions, positions)
    with np.load(path, allow_pickle=False) as archive:
        assert all(archive[key].dtype.kind != "O" for key in archive.files)


def test_motion_asset_rejects_non_finite_positions() -> None:
    with pytest.raises(ValueError, match="finite"):
        MotionAsset(
            name="bad",
            fps=50,
            joint_names=("joint",),
            positions=np.asarray([[0.0], [np.nan]]),
        )


def test_loader_rejects_inconsistent_duration(tmp_path: Path) -> None:
    path = tmp_path / "bad.npz"
    np.savez_compressed(
        path,
        format_version=np.asarray(1),
        name=np.asarray("bad"),
        fps=np.asarray(50.0),
        joint_names=np.asarray(["joint"]),
        positions=np.asarray([[0.0], [0.0]]),
        duration=np.asarray(99.0),
        position_mode=np.asarray("relative_to_runtime_start"),
        metadata_json=np.asarray(json.dumps({})),
    )

    with pytest.raises(ValueError, match="duration"):
        load_motion_asset(path)
