from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from typing import Sequence

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPOSITORY_ROOT))

from tools.reaction_generator.environment import PythonRuntime
from tools.reaction_generator.runner import run_external_stage

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm"})


def build_gvhmr_command(
    video: Path,
    *,
    gvhmr_root: Path,
    python_executable: str,
    output_root: Path,
    static_camera: bool,
) -> list[str]:
    runtime = PythonRuntime.parse(python_executable)
    command = runtime.command(
        gvhmr_root / "tools" / "demo" / "demo.py",
        [
        "--video",
        runtime.path(video.resolve()),
        "--output_root",
        runtime.path(output_root.resolve()),
        ],
        cwd=gvhmr_root,
    )
    if static_camera:
        command.append("-s")
    return command


def extract_human_motion(
    video: str | Path,
    *,
    gvhmr_root: str | Path,
    python_executable: str = sys.executable,
    output_root: str | Path,
    static_camera: bool = False,
    dry_run: bool = False,
) -> Path:
    video_path = Path(video)
    root = Path(gvhmr_root)
    destination_root = Path(output_root)
    _validate_video(video_path, require_file=not dry_run)
    script = root / "tools" / "demo" / "demo.py"
    if not dry_run and not script.is_file():
        raise FileNotFoundError(f"GVHMR demo script not found: {script}")
    expected = destination_root / video_path.stem / "hmr4d_results.pt"
    command = build_gvhmr_command(
        video_path,
        gvhmr_root=root,
        python_executable=python_executable,
        output_root=destination_root,
        static_camera=static_camera,
    )
    if dry_run:
        print(_format_command(command))
        return expected
    destination_root.mkdir(parents=True, exist_ok=True)
    run_external_stage(command, cwd=root, stage="GVHMR")
    if not expected.is_file():
        raise RuntimeError(
            "GVHMR completed but did not produce the expected result: "
            f"{expected}"
        )
    return expected


def _validate_video(path: Path, *, require_file: bool = True) -> None:
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        supported = ", ".join(sorted(VIDEO_EXTENSIONS))
        raise ValueError(f"Unsupported video extension; expected one of: {supported}")
    if require_file and not path.is_file():
        raise FileNotFoundError(f"Input video not found: {path}")


def _format_command(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(command))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run official GVHMR on one video")
    parser.add_argument("video", type=Path)
    parser.add_argument("--gvhmr-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable, dest="python_executable")
    parser.add_argument(
        "--static-camera",
        action="store_true",
        help="Pass -s only when the source camera was physically static",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = extract_human_motion(
            args.video,
            gvhmr_root=args.gvhmr_root,
            python_executable=args.python_executable,
            output_root=args.output_root,
            static_camera=args.static_camera,
            dry_run=args.dry_run,
        )
        print(f"GVHMR output: {result}")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
