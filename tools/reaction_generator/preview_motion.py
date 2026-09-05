from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys
import time
from typing import Any

import numpy as np

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPOSITORY_ROOT / "src"))

from g1_bottle_reaction.motion import load_motion_asset
from g1_bottle_reaction.motion.g1_joints import G1_ARM_JOINT_NAMES

DEFAULT_MODEL = (
    _REPOSITORY_ROOT
    / "models"
    / "unitree_mujoco"
    / "unitree_robots"
    / "g1"
    / "scene_29dof.xml"
)


def summarize_asset(path: str | Path) -> dict[str, Any]:
    asset = load_motion_asset(path)
    if asset.joint_names != G1_ARM_JOINT_NAMES:
        raise ValueError("Motion asset does not use the canonical G1 14-DoF arm joints")
    velocities = np.diff(asset.positions, axis=0) * asset.fps
    return {
        "name": asset.name,
        "frames": int(asset.positions.shape[0]),
        "fps": float(asset.fps),
        "duration_seconds": float(asset.duration),
        "max_abs_offset_rad": float(np.max(np.abs(asset.positions))),
        "max_abs_velocity_rad_s": float(np.max(np.abs(velocities))),
    }


def inspect_with_mujoco(
    asset_path: str | Path, model_path: str | Path
) -> dict[str, Any]:
    mujoco = _import_mujoco()
    asset = load_motion_asset(asset_path)
    model = mujoco.MjModel.from_xml_path(str(Path(model_path).resolve()))
    data = mujoco.MjData(model)
    joints = _resolve_joints(mujoco, model, asset.joint_names)
    range_violations = 0
    collision_frames = 0
    collision_pairs: Counter[tuple[str, str]] = Counter()
    for frame in asset.positions:
        data.qpos[:] = model.qpos0
        for offset, (joint_id, qpos_address) in zip(frame, joints):
            data.qpos[qpos_address] += float(offset)
            low, high = model.jnt_range[joint_id]
            if data.qpos[qpos_address] < low or data.qpos[qpos_address] > high:
                range_violations += 1
        mujoco.mj_forward(model, data)
        frame_has_collision = False
        for contact_index in range(data.ncon):
            contact = data.contact[contact_index]
            body1 = int(model.geom_bodyid[contact.geom1])
            body2 = int(model.geom_bodyid[contact.geom2])
            if body1 == 0 or body2 == 0 or body1 == body2:
                continue
            frame_has_collision = True
            body1_name = (
                mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, body1)
                or f"body#{body1}"
            )
            body2_name = (
                mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, body2)
                or f"body#{body2}"
            )
            names = tuple(sorted((body1_name, body2_name)))
            collision_pairs[names] += 1
        collision_frames += int(frame_has_collision)
    return {
        "range_violations": range_violations,
        "collision_frames": collision_frames,
        "collision_pairs": dict(collision_pairs),
    }


def play_with_mujoco(
    asset_path: str | Path,
    model_path: str | Path,
    *,
    speed: float,
    loop: bool,
) -> None:
    mujoco = _import_mujoco()
    try:
        import mujoco.viewer
    except ImportError as exc:
        raise RuntimeError("MuJoCo viewer is unavailable") from exc
    asset = load_motion_asset(asset_path)
    model = mujoco.MjModel.from_xml_path(str(Path(model_path).resolve()))
    data = mujoco.MjData(model)
    joints = _resolve_joints(mujoco, model, asset.joint_names)
    frame_period = 1.0 / (asset.fps * speed)
    with mujoco.viewer.launch_passive(model, data) as viewer:
        while viewer.is_running():
            for frame in asset.positions:
                if not viewer.is_running():
                    return
                started = time.monotonic()
                data.qpos[:] = model.qpos0
                for offset, (_, qpos_address) in zip(frame, joints):
                    data.qpos[qpos_address] += float(offset)
                mujoco.mj_forward(model, data)
                viewer.sync()
                time.sleep(max(0.0, frame_period - (time.monotonic() - started)))
            if not loop:
                return


def _resolve_joints(mujoco: Any, model: Any, names: tuple[str, ...]):
    resolved: list[tuple[int, int]] = []
    for name in names:
        joint_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        if joint_id < 0:
            raise ValueError(f"MuJoCo model is missing joint: {name}")
        if model.jnt_type[joint_id] != mujoco.mjtJoint.mjJNT_HINGE:
            raise ValueError(f"Expected a hinge joint in MuJoCo model: {name}")
        resolved.append((joint_id, int(model.jnt_qposadr[joint_id])))
    return resolved


def _import_mujoco():
    try:
        import mujoco
    except ImportError as exc:
        raise RuntimeError(
            "MuJoCo is optional; install the project with the 'sim' extra"
        ) from exc
    return mujoco


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or preview a generated upper-body motion asset"
    )
    parser.add_argument("asset", type=Path)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="Validate and summarize the asset without importing MuJoCo",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run MuJoCo range/contact checks without opening a viewer",
    )
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--speed", type=float, default=1.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not np.isfinite(args.speed) or args.speed <= 0:
            raise ValueError("Preview speed must be positive and finite")
        summary = summarize_asset(args.asset)
        print(
            f"{summary['name']}: {summary['frames']} frames at "
            f"{summary['fps']:g} Hz ({summary['duration_seconds']:.3f} s)"
        )
        print(
            f"max offset={summary['max_abs_offset_rad']:.3f} rad, "
            f"max velocity={summary['max_abs_velocity_rad_s']:.3f} rad/s"
        )
        if args.inspect_only:
            return 0
        if not args.model.is_file():
            raise FileNotFoundError(f"MuJoCo model not found: {args.model}")
        if args.headless:
            report = inspect_with_mujoco(args.asset, args.model)
            print(
                f"MuJoCo: range violations={report['range_violations']}, "
                f"collision frames={report['collision_frames']}"
            )
            if report["collision_pairs"]:
                print(f"Contact pairs: {report['collision_pairs']}")
            print("This preview does not prove dynamic or real-robot safety.")
            return int(report["range_violations"] > 0)
        play_with_mujoco(
            args.asset,
            args.model,
            speed=args.speed,
            loop=args.loop,
        )
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
