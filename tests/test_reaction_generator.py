from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import numpy as np
import pytest

from g1_bottle_reaction.motion import load_motion_asset
from g1_bottle_reaction.motion.g1_joints import G1_29DOF_JOINT_NAMES
from tools.reaction_generator.clean_motion import load_gmr_intermediate
from tools.reaction_generator.extract_human_motion import build_gvhmr_command
from tools.reaction_generator.environment import PythonRuntime, resolve_environment
from tools.reaction_generator.generate import (
    generate_reaction_motion,
    validate_motion_name,
)
from tools.reaction_generator.preview_motion import (
    inspect_with_mujoco,
    summarize_asset,
)
from tools.reaction_generator.retarget_g1 import (
    _actuated_joint_names,
    build_gmr_command,
)
from tools.reaction_generator.runner import ExternalStageError, run_external_stage


def _write_intermediate(path: Path, *, robot: str = "unitree_g1") -> None:
    positions = np.zeros((4, len(G1_29DOF_JOINT_NAMES)), dtype=np.float32)
    positions[1:3, 15] = 0.25
    np.savez_compressed(
        path,
        fps=np.asarray(30.0),
        joint_names=np.asarray(G1_29DOF_JOINT_NAMES),
        dof_positions=positions,
        robot=np.asarray(robot),
    )


def test_external_commands_use_official_entrypoints(tmp_path: Path) -> None:
    video = tmp_path / "source clip.mp4"
    gvhmr_root = tmp_path / "GVHMR"
    output_root = tmp_path / "out"
    command = build_gvhmr_command(
        video,
        gvhmr_root=gvhmr_root,
        python_executable="gvhmr-python",
        output_root=output_root,
        static_camera=True,
    )

    assert command[0] == "gvhmr-python"
    assert Path(command[1]) == gvhmr_root / "tools" / "demo" / "demo.py"
    assert "--video" in command
    assert "--output_root" in command
    assert command[-1] == "-s"

    gmr_command = build_gmr_command(
        tmp_path / "hmr4d_results.pt",
        gmr_root=tmp_path / "GMR",
        python_executable="gmr-python",
        output=tmp_path / "g1.npz",
    )
    assert gmr_command[0] == "gmr-python"
    assert "--worker" in gmr_command
    assert "--gmr-root" in gmr_command


def test_wsl_runtime_translates_windows_paths() -> None:
    runtime = PythonRuntime.parse(
        "wsl:Ubuntu:/home/user/.local/share/g1-reaction-generator/envs/gvhmr/bin/python"
    )
    command = runtime.command(
        Path(r"C:\dev\GVHMR\tools\demo\demo.py"),
        ["--video", runtime.path(Path(r"C:\dev\clip.mp4"))],
        cwd=Path(r"C:\dev\GVHMR"),
    )

    assert command[:3] == ["wsl.exe", "-d", "Ubuntu"]
    assert "/mnt/c/dev/GVHMR" in command
    assert "/mnt/c/dev/GVHMR/tools/demo/demo.py" in command
    assert "/mnt/c/dev/clip.mp4" in command


def test_saved_environment_config_selects_isolated_wsl_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("GVHMR_ROOT", "GMR_ROOT", "GVHMR_PYTHON", "GMR_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "environment.local.json"
    config.write_text(
        """{
          "backend": "wsl",
          "wsl_distribution": "Ubuntu",
          "gvhmr_root": "C:\\\\dev\\\\GVHMR",
          "gmr_root": "C:\\\\dev\\\\GMR",
          "gvhmr_python": "/opt/reaction/envs/gvhmr/bin/python",
          "gmr_python": "/opt/reaction/envs/gmr/bin/python"
        }""",
        encoding="utf-8",
    )

    environment = resolve_environment(config)

    assert environment.gvhmr_root == Path(r"C:\dev\GVHMR")
    assert environment.gmr_root == Path(r"C:\dev\GMR")
    assert environment.gvhmr_runtime.is_wsl
    assert environment.gvhmr_runtime.serialize().startswith("wsl:Ubuntu:")


def test_external_stage_hides_child_traceback_and_names_missing_module(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "missing.py"
    script.write_text("import definitely_missing_reaction_module\n", encoding="utf-8")

    with pytest.raises(ExternalStageError, match="definitely_missing_reaction_module"):
        run_external_stage([sys.executable, str(script)], cwd=tmp_path, stage="GVHMR")

    assert "Traceback (most recent call last)" not in capsys.readouterr().out


def test_gmr_dof_names_exclude_floating_base_and_follow_dof_order() -> None:
    raw_names = {
        None: 5,
        "right_joint": 7,
        "left_joint": 6,
    }

    assert _actuated_joint_names(raw_names, model_nv=8, qpos_width=2) == (
        "left_joint",
        "right_joint",
    )


def test_generate_from_intermediate_is_windows_testable(tmp_path: Path) -> None:
    video = tmp_path / "surprised.mp4"
    video.write_bytes(b"synthetic video identity")
    intermediate = tmp_path / "gmr.npz"
    _write_intermediate(intermediate)

    output = generate_reaction_motion(
        video,
        name="surprised",
        output_dir=tmp_path / "motions",
        work_dir=tmp_path / "work",
        from_gmr_npz=intermediate,
    )
    asset = load_motion_asset(output)

    assert output == tmp_path / "motions" / "surprised.npz"
    assert asset.metadata["source_video_sha256"] == hashlib.sha256(
        video.read_bytes()
    ).hexdigest()
    assert asset.metadata["pipeline"].startswith("GVHMR")
    assert asset.metadata["gvhmr_revision"] is None
    assert asset.positions.shape[1] == 14
    assert all(not name.startswith("waist_") for name in asset.joint_names)
    assert asset.metadata["reaction_joint_set"] == "unitree_g1_arms_14dof"
    summary = summarize_asset(output)
    assert summary["name"] == "surprised"
    assert summary["fps"] == 50.0

    with pytest.raises(FileExistsError):
        generate_reaction_motion(
            video,
            name="surprised",
            output_dir=tmp_path / "motions",
            work_dir=tmp_path / "work",
            from_gmr_npz=intermediate,
        )


@pytest.mark.parametrize("name", ["../escape", "has space", "", "1starts_wrong"])
def test_motion_name_rejects_path_traversal_and_ambiguous_names(name: str) -> None:
    with pytest.raises(ValueError, match="Motion name"):
        validate_motion_name(name)


def test_intermediate_must_target_unitree_g1(tmp_path: Path) -> None:
    intermediate = tmp_path / "wrong_robot.npz"
    _write_intermediate(intermediate, robot="other_robot")

    with pytest.raises(ValueError, match="unitree_g1"):
        load_gmr_intermediate(intermediate)


def test_dry_run_does_not_require_models_or_video(tmp_path: Path) -> None:
    output = generate_reaction_motion(
        tmp_path / "missing.mp4",
        name="dry_run",
        output_dir=tmp_path / "motions",
        work_dir=tmp_path / "work",
        gvhmr_root=tmp_path / "GVHMR",
        gmr_root=tmp_path / "GMR",
        dry_run=True,
    )

    assert output == tmp_path / "motions" / "dry_run.npz"
    assert not output.exists()


def test_generated_asset_can_be_checked_in_optional_mujoco(tmp_path: Path) -> None:
    pytest.importorskip("mujoco")
    model = (
        Path(__file__).resolve().parents[1]
        / "models"
        / "unitree_mujoco"
        / "unitree_robots"
        / "g1"
        / "scene_29dof.xml"
    )
    if not model.is_file():
        pytest.skip("External Unitree MuJoCo checkout is not installed")
    video = tmp_path / "surprised.mov"
    video.write_bytes(b"synthetic video identity")
    intermediate = tmp_path / "gmr.npz"
    _write_intermediate(intermediate)
    asset_path = generate_reaction_motion(
        video,
        name="preview",
        output_dir=tmp_path,
        work_dir=tmp_path / "work",
        from_gmr_npz=intermediate,
    )

    report = inspect_with_mujoco(asset_path, model)

    assert report["range_violations"] == 0
    assert report["collision_frames"] >= 0
