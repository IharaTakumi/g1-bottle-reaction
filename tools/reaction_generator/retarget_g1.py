from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPOSITORY_ROOT))

from tools.reaction_generator.environment import PythonRuntime
from tools.reaction_generator.runner import run_external_stage


def build_gmr_command(
    gvhmr_result: Path,
    *,
    gmr_root: Path,
    python_executable: str,
    output: Path,
) -> list[str]:
    runtime = PythonRuntime.parse(python_executable)
    return runtime.command(
        Path(__file__).resolve(),
        [
        "--worker",
        "--gvhmr-result",
        runtime.path(gvhmr_result.resolve()),
        "--gmr-root",
        runtime.path(gmr_root.resolve()),
        "--output",
        runtime.path(output.resolve()),
        ],
        cwd=gmr_root,
    )


def retarget_g1(
    gvhmr_result: str | Path,
    *,
    gmr_root: str | Path,
    python_executable: str = sys.executable,
    output: str | Path,
    dry_run: bool = False,
) -> Path:
    result_path = Path(gvhmr_result)
    root = Path(gmr_root)
    output_path = Path(output)
    if not dry_run and not result_path.is_file():
        raise FileNotFoundError(f"GVHMR result not found: {result_path}")
    if not dry_run and not (root / "general_motion_retargeting").is_dir():
        raise FileNotFoundError(f"GMR package not found below: {root}")
    command = build_gmr_command(
        result_path,
        gmr_root=root,
        python_executable=python_executable,
        output=output_path,
    )
    if dry_run:
        print(_format_command(command))
        return output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_external_stage(command, cwd=root, stage="GMR")
    if not output_path.is_file():
        raise RuntimeError(f"GMR worker did not produce: {output_path}")
    return output_path


def _run_worker(gvhmr_result: Path, gmr_root: Path, output: Path) -> None:
    # Imported only inside the separately configured GMR Python environment.
    sys.path.insert(0, str(gmr_root.resolve()))
    import numpy as np

    from general_motion_retargeting import GeneralMotionRetargeting
    from general_motion_retargeting.utils.smpl import (
        get_gvhmr_data_offline_fast,
        load_gvhmr_pred_file,
    )

    body_models = gmr_root / "assets" / "body_models"
    smplx_data, body_model, smplx_output, actual_human_height = load_gvhmr_pred_file(
        str(gvhmr_result), body_models
    )
    frames, aligned_fps = get_gvhmr_data_offline_fast(
        smplx_data,
        body_model,
        smplx_output,
        tgt_fps=30,
    )
    retargeter = GeneralMotionRetargeting(
        actual_human_height=actual_human_height,
        src_human="smplx",
        tgt_robot="unitree_g1",
    )
    joint_names = _actuated_joint_names(
        retargeter.robot_dof_names,
        model_nv=int(retargeter.model.nv),
        qpos_width=int(retargeter.model.nq) - 7,
    )
    dof_positions = np.asarray(
        [retargeter.retarget(frame)[7:] for frame in frames], dtype=np.float64
    )
    if dof_positions.ndim != 2 or dof_positions.shape[1] != len(joint_names):
        raise RuntimeError(
            "GMR joint-name count does not match retargeted qpos width: "
            f"{len(joint_names)} names, {dof_positions.shape} positions"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        output,
        fps=np.asarray(float(aligned_fps), dtype=np.float64),
        joint_names=np.asarray(joint_names),
        dof_positions=dof_positions.astype(np.float32),
        robot=np.asarray("unitree_g1"),
        source=np.asarray(str(gvhmr_result)),
    )


def _actuated_joint_names(
    raw_names: Any,
    *,
    model_nv: int,
    qpos_width: int,
) -> tuple[str, ...]:
    """Match GMR's floating-base DoF map to the actuated qpos[7:] slice."""

    floating_base_dofs = model_nv - qpos_width
    if floating_base_dofs < 0:
        raise RuntimeError("GMR model dimensions are inconsistent")
    if isinstance(raw_names, Mapping):
        ordered = sorted(
            (
                (int(dof_index), name)
                for name, dof_index in raw_names.items()
                if name is not None and int(dof_index) >= floating_base_dofs
            ),
            key=lambda item: item[0],
        )
        names = tuple(str(name) for _, name in ordered)
    else:
        values = tuple(raw_names)
        names = tuple(str(name) for name in values[-qpos_width:] if name is not None)
    if len(names) != qpos_width or len(set(names)) != len(names):
        raise RuntimeError(
            "Could not map GMR DoF names to qpos[7:]: "
            f"expected {qpos_width}, got {len(names)}"
        )
    return names


def _format_command(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(command))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Retarget a GVHMR result with the official GMR Python API"
    )
    parser.add_argument("--gvhmr-result", type=Path, required=True)
    parser.add_argument("--gmr-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable, dest="python_executable")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.worker:
            _run_worker(args.gvhmr_result, args.gmr_root, args.output)
            return 0
        result = retarget_g1(
            args.gvhmr_result,
            gmr_root=args.gmr_root,
            python_executable=args.python_executable,
            output=args.output,
            dry_run=args.dry_run,
        )
        print(f"GMR output: {result}")
        return 0
    except (ImportError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
