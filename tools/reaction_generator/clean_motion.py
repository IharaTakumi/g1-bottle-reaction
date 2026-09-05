from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Mapping

import numpy as np

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPOSITORY_ROOT / "src"))

from g1_bottle_reaction.motion import (
    MotionAsset,
    clean_upper_body_motion,
    load_motion_cleaner_config,
    save_motion_asset,
)
from g1_bottle_reaction.motion.g1_joints import G1_ARM_JOINT_NAMES

GENERATOR_VERSION = "2"


def load_gmr_intermediate(path: str | Path) -> tuple[np.ndarray, tuple[str, ...], float]:
    source = Path(path)
    try:
        with np.load(source, allow_pickle=False) as archive:
            required = {"fps", "joint_names", "dof_positions", "robot"}
            missing = required - set(archive.files)
            if missing:
                raise ValueError(
                    "GMR intermediate is missing fields: " + ", ".join(sorted(missing))
                )
            raw_positions = np.asarray(archive["dof_positions"])
            if not np.issubdtype(raw_positions.dtype, np.number) or not np.isrealobj(
                raw_positions
            ):
                raise ValueError("GMR positions must be real numeric values")
            positions = raw_positions.astype(np.float64, copy=False)
            joint_names = tuple(str(item) for item in archive["joint_names"].tolist())
            fps = float(archive["fps"].item())
            robot = str(archive["robot"].item())
    except OSError as exc:
        raise ValueError(f"Could not load GMR intermediate {source}: {exc}") from exc
    if robot != "unitree_g1":
        raise ValueError(f"Expected unitree_g1 GMR output, got: {robot}")
    return positions, joint_names, fps


def clean_gmr_file(
    source: str | Path,
    output: str | Path,
    *,
    name: str,
    config_path: str | Path | None = None,
    metadata: Mapping[str, Any] | None = None,
    overwrite: bool = False,
):
    output_path = Path(output)
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Motion asset already exists: {output_path}")
    positions, joint_names, fps = load_gmr_intermediate(source)
    upper_body_asset, report = clean_upper_body_motion(
        positions,
        joint_names,
        source_fps=fps,
        name=name,
        config=load_motion_cleaner_config(config_path),
        metadata={"generator_version": GENERATOR_VERSION, **dict(metadata or {})},
    )
    by_name = {
        joint_name: index
        for index, joint_name in enumerate(upper_body_asset.joint_names)
    }
    asset = MotionAsset(
        name=upper_body_asset.name,
        fps=upper_body_asset.fps,
        joint_names=G1_ARM_JOINT_NAMES,
        positions=upper_body_asset.positions[
            :, [by_name[name] for name in G1_ARM_JOINT_NAMES]
        ],
        metadata={
            **upper_body_asset.metadata,
            "reaction_joint_set": "unitree_g1_arms_14dof",
        },
    )
    save_motion_asset(asset, output_path)
    return asset, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clean a GMR intermediate into a safe upper-body motion asset"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--source-video")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asset, report = clean_gmr_file(
            args.source,
            args.output,
            name=args.name,
            config_path=args.config,
            metadata={"source_video": args.source_video} if args.source_video else None,
            overwrite=args.overwrite,
        )
        print(
            f"Saved {args.output}: {asset.positions.shape[0]} frames, "
            f"{asset.fps:g} Hz, {asset.duration:.3f} s"
        )
        print(
            f"Clipped samples: source={report.source_limit_clips}, "
            f"offset={report.offset_limit_clips}"
        )
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
