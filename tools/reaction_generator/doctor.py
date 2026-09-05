from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Iterable


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPOSITORY_ROOT))
sys.path.insert(0, str(_REPOSITORY_ROOT / "src"))

from tools.reaction_generator.environment import (  # noqa: E402
    LOCAL_CONFIG_PATH,
    PythonRuntime,
    load_local_config,
    resolve_environment,
)


@dataclass(frozen=True)
class Check:
    level: str
    name: str
    message: str


def _check_file(name: str, path: Path, *, minimum_bytes: int = 1) -> Check:
    if path.is_file() and path.stat().st_size >= minimum_bytes:
        return Check("OK", name, str(path))
    return Check("ERROR", name, f"Missing: {path}")


def _run(command: list[str], *, cwd: Path = _REPOSITORY_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )


def _probe_python(
    name: str,
    runtime: PythonRuntime,
    root: Path,
    modules: Iterable[str],
) -> list[Check]:
    module_list = list(modules)
    code = (
        "import importlib,json,shutil,sys; "
        f"mods={module_list!r}; "
        "loaded={m:importlib.import_module(m) for m in mods}; "
        "torch=loaded.get('torch'); "
        "print(json.dumps({'python':sys.version.split()[0],"
        "'modules':mods,'torch':getattr(torch,'__version__',None),"
        "'torch_cuda':getattr(getattr(torch,'version',None),'cuda',None),"
        "'cuda_available':bool(torch and torch.cuda.is_available()),"
        "'gpu':torch.cuda.get_device_name(0) if torch and torch.cuda.is_available() else None,"
        "'ffmpeg':shutil.which('ffmpeg')}))"
    )
    try:
        result = _run(runtime.probe_command(code, cwd=root), cwd=root)
    except (OSError, subprocess.SubprocessError) as exc:
        return [Check("ERROR", f"{name} Python", str(exc))]
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return [
            Check(
                "ERROR",
                f"{name} Python",
                detail[-1] if detail else f"probe exited {result.returncode}",
            )
        ]
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        return [Check("ERROR", f"{name} Python", f"invalid probe output: {exc}")]
    checks = [
        Check("OK", f"{name} Python", f"{payload['python']} ({runtime.serialize()})"),
        Check("OK", f"{name} packages", ", ".join(payload["modules"])),
        Check("OK" if payload["ffmpeg"] else "ERROR", f"{name} ffmpeg", payload["ffmpeg"] or "not found"),
    ]
    if "torch" in module_list:
        torch_message = f"{payload['torch']} (built for CUDA {payload['torch_cuda']})"
        checks.append(Check("OK", f"{name} PyTorch", torch_message))
        level = "OK" if name != "GVHMR" or payload["cuda_available"] else "ERROR"
        if payload["gpu"]:
            message = payload["gpu"]
        elif name == "GVHMR":
            message = "CUDA is not available; official GVHMR inference requires NVIDIA CUDA"
        else:
            message = "not required for the GMR retargeting stage"
        checks.append(Check(level, f"{name} CUDA", message))
    return checks


def collect_checks() -> list[Check]:
    checks: list[Check] = []
    windows_build = sys.getwindowsversion().build if sys.platform == "win32" else None
    windows_name = "Windows 11" if windows_build is not None and windows_build >= 22000 else platform.system()
    checks.append(Check("OK", "OS", f"{windows_name} build {windows_build}" if windows_build else platform.platform()))
    checks.append(Check("OK", "Host Python", f"{platform.python_version()} ({sys.executable})"))
    try:
        import torch as host_torch

        checks.append(Check("OK", "Game PyTorch", str(host_torch.__version__)))
        checks.append(
            Check(
                "INFO" if not host_torch.cuda.is_available() else "OK",
                "Game CUDA",
                host_torch.cuda.get_device_name(0) if host_torch.cuda.is_available() else "not available (the game environment remains CPU-only by design)",
            )
        )
    except ImportError:
        checks.append(Check("INFO", "Game PyTorch", "not installed; it is not modified by generator setup"))
    checks.append(
        Check(
            "OK" if LOCAL_CONFIG_PATH.is_file() else "ERROR",
            "Saved configuration",
            str(LOCAL_CONFIG_PATH) if LOCAL_CONFIG_PATH.is_file() else f"Missing: {LOCAL_CONFIG_PATH}; run setup_ubuntu.sh (Ubuntu) or setup.ps1 (Windows)",
        )
    )
    try:
        environment = resolve_environment()
        config = load_local_config()
    except ValueError as exc:
        checks.append(Check("ERROR", "Configuration", str(exc)))
        return checks

    for label, root, marker in (
        ("GVHMR", environment.gvhmr_root, Path("tools/demo/demo.py")),
        ("GMR", environment.gmr_root, Path("general_motion_retargeting/__init__.py")),
    ):
        if root is None:
            checks.append(Check("ERROR", label, f"root not configured; run setup_ubuntu.sh (Ubuntu) or setup.ps1 (Windows) or set {label}_ROOT"))
        elif (root / marker).is_file():
            checks.append(Check("OK", label, str(root)))
        else:
            checks.append(Check("ERROR", label, f"invalid checkout: {root}"))

    for label, root, key in (
        ("GVHMR", environment.gvhmr_root, "gvhmr_revision"),
        ("GMR", environment.gmr_root, "gmr_revision"),
    ):
        expected = config.get(key)
        if root is None or not expected:
            continue
        revision = _run(["git", "-C", str(root), "rev-parse", "HEAD"])
        actual = revision.stdout.strip()
        checks.append(
            Check(
                "OK" if revision.returncode == 0 and actual == expected else "ERROR",
                f"{label} revision",
                actual if actual == expected else f"expected {expected}, found {actual or 'unavailable'}",
            )
        )

    gvhmr_root = environment.gvhmr_root
    gmr_root = environment.gmr_root
    if gvhmr_root is not None and (environment.gvhmr_runtime.is_wsl or sys.platform.startswith("linux")):
        checks.extend(
            _probe_python(
                "GVHMR",
                environment.gvhmr_runtime,
                gvhmr_root,
                ("torch", "torchvision", "pytorch3d", "pytorch3d.ops", "lightning", "pytorch_lightning", "hydra", "colorlog", "yacs", "cv2", "ultralytics", "hmr4d"),
            )
        )
    elif gvhmr_root is not None:
        checks.append(Check("ERROR", "GVHMR runtime", "native Windows is unsupported; run setup.ps1 for WSL2"))

    if gmr_root is not None:
        checks.extend(
            _probe_python(
                "GMR",
                environment.gmr_runtime,
                gmr_root,
                ("torch", "numpy", "mujoco", "mink", "smplx", "general_motion_retargeting"),
            )
        )

    if gvhmr_root is not None:
        checkpoint_root = gvhmr_root / "inputs" / "checkpoints"
        for name, relative in (
            ("GVHMR checkpoint", "gvhmr/gvhmr_siga24_release.ckpt"),
            ("HMR2 checkpoint", "hmr2/epoch=10-step=25000.ckpt"),
            ("ViTPose checkpoint", "vitpose/vitpose-h-multi-coco.pth"),
            ("YOLO checkpoint", "yolo/yolov8x.pt"),
            ("GVHMR SMPL-X model", "body_models/smplx/SMPLX_NEUTRAL.npz"),
            ("GVHMR SMPL model", "body_models/smpl/SMPL_NEUTRAL.pkl"),
        ):
            checks.append(_check_file(name, checkpoint_root / relative, minimum_bytes=1024 * 1024))

    if gmr_root is not None:
        model_root = gmr_root / "assets" / "body_models" / "smplx"
        candidates = (model_root / "SMPLX_NEUTRAL.npz",)
        existing = next((path for path in candidates if path.is_file() and path.stat().st_size >= 1024 * 1024), None)
        checks.append(
            Check(
                "OK" if existing else "ERROR",
                "GMR SMPL-X model",
                str(existing) if existing else "Missing: " + " or ".join(str(path) for path in candidates),
            )
        )

    if environment.gvhmr_runtime.is_wsl:
        distro = str(environment.gvhmr_runtime.wsl_distribution)
        os_result = _run(["wsl.exe", "-d", distro, "--exec", "sh", "-c", ". /etc/os-release; printf '%s' \"$PRETTY_NAME\""])
        if os_result.returncode:
            checks.append(Check("ERROR", "WSL2", (os_result.stderr or os_result.stdout).strip()))
        else:
            version = os_result.stdout.strip()
            supported = "Ubuntu 22.04" in version or "Ubuntu 20.04" in version
            checks.append(Check("OK" if supported else "WARN", "WSL distribution", version + ("" if supported else "; GMR upstream only tests 20.04/22.04")))
        gpu_result = _run(["wsl.exe", "-d", distro, "--exec", "nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
        checks.append(
            Check(
                "OK" if gpu_result.returncode == 0 else "ERROR",
                "NVIDIA GPU in WSL",
                gpu_result.stdout.strip() if gpu_result.returncode == 0 else "not available; this PC currently exposes no NVIDIA CUDA GPU",
            )
        )
        vswhere = Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        vs_detail = "detected but not required" if vswhere.is_file() else "not detected and not required"
        checks.append(Check("INFO", "Visual Studio Build Tools", f"{vs_detail}; the selected WSL2/Linux PyTorch3D wheel avoids a native build"))
    elif sys.platform.startswith("linux"):
        checks.extend(_ubuntu_checks(environment))
    elif shutil.which("cl.exe"):
        checks.append(Check("OK", "Visual Studio Build Tools", str(shutil.which("cl.exe"))))
    else:
        checks.append(Check("WARN", "Visual Studio Build Tools", "not detected"))
    return checks


def _ubuntu_checks(environment) -> list[Check]:
    checks = []
    tools = _REPOSITORY_ROOT / ".reaction-tools"
    checks.append(_check_file("micromamba", tools / "bin/micromamba"))
    for name in ("gvhmr", "gmr"):
        checks.append(_check_file(name + " environment", tools / "envs" / name / "bin/python"))
    try:
        result = _run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"])
        checks.append(Check("OK" if result.returncode == 0 else "ERROR", "NVIDIA GPU / driver", (result.stdout or result.stderr).strip()))
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(Check("ERROR", "NVIDIA GPU / driver", str(exc)))
    # Exercise real kernels; is_available() alone misses unsupported GPU architectures.
    code = (
        "import torch; from pytorch3d.ops import knn_points; "
        "x=torch.randn(1,32,3,device='cuda'); "
        "y=x@torch.eye(3,device='cuda'); "
        "assert torch.isfinite(knn_points(x,y).dists).all(); "
        "torch.cuda.synchronize(); print(torch.cuda.get_device_name(0))"
    )
    try:
        result = _run(environment.gvhmr_runtime.probe_command(code, cwd=_REPOSITORY_ROOT))
        checks.append(Check("OK" if result.returncode == 0 else "ERROR", "CUDA / PyTorch3D kernel", (result.stdout if result.returncode == 0 else result.stderr)[-1800:].strip()))
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(Check("ERROR", "CUDA / PyTorch3D kernel", str(exc)))
    baseline = tools / "locks/g1-before.txt"
    g1_python = _REPOSITORY_ROOT / ".venv-g1/bin/python"
    if baseline.is_file() and g1_python.is_file():
        try:
            result = _run([str(g1_python), "-m", "pip", "freeze"])
            unchanged = result.returncode == 0 and sorted(result.stdout.splitlines()) == sorted(baseline.read_text().splitlines())
            checks.append(Check("OK" if unchanged else "ERROR", "existing G1 environment unchanged", "pip freeze matches baseline" if unchanged else "dependency list differs or could not be read"))
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append(Check("ERROR", "existing G1 environment unchanged", str(exc)))
    else:
        checks.append(Check("WARN", "existing G1 environment unchanged", "baseline or .venv-g1 unavailable; not verified"))
    fingerprints = tools / "locks/host-before.json"
    if fingerprints.is_file():
        for path, expected in json.loads(fingerprints.read_text()).items():
            file = Path(path)
            matches = file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest() == expected
            checks.append(Check("OK" if matches else "ERROR", "host file unchanged", path))
    checks.append(Check("OK" if (_REPOSITORY_ROOT / "input/surprised_01.mp4").is_file() else "WARN", "sample video", str(_REPOSITORY_ROOT / "input/surprised_01.mp4")))
    if environment.gvhmr_root:
        checks.append(Check("INFO", "Licensed model placement", "Download from https://smpl.is.tue.mpg.de/ and https://smpl-x.is.tue.mpg.de/ under your license. Paths are listed above."))
    return checks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check video-to-G1 generator prerequisites")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    checks = collect_checks()
    if args.json:
        print(json.dumps([check.__dict__ for check in checks], indent=2, ensure_ascii=False))
    else:
        for check in checks:
            print(f"[{check.level}] {check.name}: {check.message}")
        if any(check.level == "ERROR" for check in checks):
            print("\nReaction generator prerequisites are incomplete.")
            print("Run tools/reaction_generator/setup_ubuntu.sh on Ubuntu, then rerun this doctor.")
        else:
            print("\nReaction generator is ready.")
    return 2 if any(check.level == "ERROR" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
