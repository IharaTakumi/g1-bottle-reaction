from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPOSITORY_ROOT))
sys.path.insert(0, str(_REPOSITORY_ROOT / "src"))

from tools.reaction_generator.clean_motion import clean_gmr_file
from tools.reaction_generator.extract_human_motion import (
    _validate_video,
    extract_human_motion,
)
from tools.reaction_generator.environment import resolve_environment
from tools.reaction_generator.retarget_g1 import retarget_g1

_MOTION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def validate_motion_name(name: str) -> str:
    if not _MOTION_NAME.fullmatch(name):
        raise ValueError(
            "Motion name must start with a letter and contain only letters, "
            "digits, '_' or '-' (maximum 64 characters)"
        )
    return name


def generate_reaction_motion(
    video: str | Path,
    *,
    name: str,
    output_dir: str | Path,
    work_dir: str | Path,
    gvhmr_root: str | Path | None = None,
    gmr_root: str | Path | None = None,
    gvhmr_python: str = sys.executable,
    gmr_python: str = sys.executable,
    config_path: str | Path | None = None,
    static_camera: bool = False,
    from_gmr_npz: str | Path | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> Path:
    validate_motion_name(name)
    video_path = Path(video)
    _validate_video(video_path, require_file=not dry_run)
    target = Path(output_dir) / f"{name}.npz"
    if target.exists() and not overwrite:
        raise FileExistsError(f"Motion asset already exists: {target}")

    if from_gmr_npz is not None:
        intermediate = Path(from_gmr_npz)
        if not dry_run and not intermediate.is_file():
            raise FileNotFoundError(f"GMR intermediate not found: {intermediate}")
        gvhmr_revision = None
        gmr_revision = None
    else:
        if gvhmr_root is None or gmr_root is None:
            raise ValueError(
                "GVHMR and GMR roots are required unless --from-gmr-npz is used"
            )
        stage_root = Path(work_dir) / name
        gvhmr_output_root = stage_root / "gvhmr"
        gvhmr_result = extract_human_motion(
            video_path,
            gvhmr_root=gvhmr_root,
            python_executable=gvhmr_python,
            output_root=gvhmr_output_root,
            static_camera=static_camera,
            dry_run=dry_run,
        )
        intermediate = stage_root / "gmr_unitree_g1.npz"
        retarget_g1(
            gvhmr_result,
            gmr_root=gmr_root,
            python_executable=gmr_python,
            output=intermediate,
            dry_run=dry_run,
        )
        gvhmr_revision = _git_revision(Path(gvhmr_root)) if not dry_run else None
        gmr_revision = _git_revision(Path(gmr_root)) if not dry_run else None

    if dry_run:
        print(f"Final motion asset: {target}")
        return target

    metadata: dict[str, Any] = {
        "source_video": str(video_path.resolve()),
        "source_video_sha256": _sha256(video_path),
        "static_camera_assumption": bool(static_camera),
        "gvhmr_revision": gvhmr_revision,
        "gmr_revision": gmr_revision,
        "pipeline": "GVHMR -> GMR unitree_g1 -> 14-DoF arm safety cleaner",
    }
    clean_gmr_file(
        intermediate,
        target,
        name=name,
        config_path=config_path,
        metadata=metadata,
        overwrite=overwrite,
    )
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def build_parser() -> argparse.ArgumentParser:
    environment = resolve_environment()
    parser = argparse.ArgumentParser(
        description="Generate a safe G1 upper-body reaction motion from a video"
    )
    parser.add_argument("video", type=Path, nargs="?")
    parser.add_argument("--name")
    parser.add_argument("--output-dir", type=Path, default=Path("motions"))
    parser.add_argument(
        "--work-dir", type=Path, default=Path(".cache/reaction_generator")
    )
    parser.add_argument(
        "--gvhmr-root",
        type=Path,
        default=environment.gvhmr_root,
    )
    parser.add_argument(
        "--gmr-root",
        type=Path,
        default=environment.gmr_root,
    )
    parser.add_argument(
        "--gvhmr-python",
        default=environment.gvhmr_runtime.serialize(),
    )
    parser.add_argument(
        "--gmr-python",
        default=environment.gmr_runtime.serialize(),
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--static-camera",
        action="store_true",
        help="Use GVHMR -s only for footage captured by a physically static camera",
    )
    parser.add_argument(
        "--from-gmr-npz",
        type=Path,
        help="Skip GVHMR/GMR and clean a previously produced intermediate",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Check the complete generator environment and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.doctor:
        from tools.reaction_generator.doctor import main as doctor_main

        return doctor_main([])
    if args.video is None or not args.name:
        print("ERROR: video and --name are required unless --doctor is used", file=sys.stderr)
        return 2
    try:
        target = generate_reaction_motion(
            args.video,
            name=args.name,
            output_dir=args.output_dir,
            work_dir=args.work_dir,
            gvhmr_root=args.gvhmr_root,
            gmr_root=args.gmr_root,
            gvhmr_python=args.gvhmr_python,
            gmr_python=args.gmr_python,
            config_path=args.config,
            static_camera=args.static_camera,
            from_gmr_npz=args.from_gmr_npz,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            print(f"Saved motion asset: {target}")
            from g1_bottle_reaction.motion import load_motion_asset

            asset = load_motion_asset(target)
            cleaner = asset.metadata["cleaner"]
            print(
                f"{asset.positions.shape[0]} frames at {asset.fps:g} Hz "
                f"({asset.duration:.3f} s); clipped samples: "
                f"source={cleaner['source_limit_clips']}, "
                f"offset={cleaner['offset_limit_clips']}"
            )
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
