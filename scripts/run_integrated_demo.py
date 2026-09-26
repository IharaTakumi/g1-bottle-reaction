#!/usr/bin/env python3
"""Run existing Reaction and Patrol entrypoints as isolated child processes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
REACTION = ROOT / "tools/g1_dual_camera.py"
PATROL = ROOT / "patrol/run_patrol.py"
MODEL = ROOT / ".runtime/models/yolo11n.pt"
PYTHON = Path("/home/ubuntu/.venvs/g1-game-vision/bin/python")
MOTIONDECODE = Path("/home/ubuntu/dev/motiondecode-test")
DRY_RUN_SOCKET = Path("/tmp/g1-integrated-motiondecode-dry.sock")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Thin supervisor for the existing camera/YOLO/Reaction and Patrol "
            "processes. Reaction never controls Patrol."
        )
    )
    mode = value.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--no-locomotion",
        action="store_true",
        help="run Patrol preflight only and keep all robot motion disabled",
    )
    mode.add_argument(
        "--real-patrol",
        action="store_true",
        help="run the existing armed three-loop Patrol in parallel",
    )
    value.add_argument(
        "--operator-approved-real-patrol",
        action="store_true",
        help="required second gate for --real-patrol",
    )
    value.add_argument("--duration", type=float, default=30.0)
    value.add_argument("--headless", action="store_true")
    value.add_argument(
        "--camera-transport", choices=("ssh-rtp", "ssh-jpeg", "direct-dds"),
        default="ssh-jpeg",
    )
    value.add_argument("--ssh-target", default="unitree@10.42.0.76")
    value.add_argument("--ssh-control")
    value.add_argument(
        "--dry-run",
        action="store_true",
        help="run local preflight and print child commands without starting camera or Patrol",
    )
    return value


def reaction_command(args: argparse.Namespace) -> list[str]:
    command = [
        str(PYTHON), "-B", str(REACTION),
        "--usb-bind", "10.42.0.1",
        "--usb-host", "10.42.0.76",
        "--network-interface", "wlp128s20f3",
        "--ssh-target", args.ssh_target,
        "--g1-camera-transport", args.camera_transport,
        "--g1-camera-port", "56001",
        "--g1-camera-fps", "30",
        "--no-usb-camera",
        "--yolo",
        "--yolo-model", str(MODEL),
        "--yolo-confidence", "0.25",
        "--banana-confidence", "0.25",
        "--plushie-confidence", "0.25",
        "--reaction-target", "all",
        "--found-audio",
        "--found-output", "mock",
        "--found-duration", "0.3",
        "--audio-cooldown", "2.0",
        "--duration", str(args.duration),
    ]
    if args.no_locomotion:
        command += [
            "--robot", "motiondecode",
            "--motiondecode-repository", str(MOTIONDECODE),
            "--motiondecode-transport", "local",
            "--motiondecode-socket", str(DRY_RUN_SOCKET),
        ]
    else:
        command += [
            "--robot", "motiondecode",
            "--motiondecode-transport", "ssh",
            "--motiondecode-socket", "/tmp/motiondecode-reaction.sock",
            "--enable-real-robot",
            "--confirm-site-ready",
            "--allow-hackathon-joy",
        ]
    if args.ssh_control:
        command += ["--ssh-control", args.ssh_control]
    command.append("--headless" if args.headless else "--windowed")
    # --with-wander is deliberately absent in both modes.
    return command


def patrol_command() -> list[str]:
    return [
        str(PYTHON), str(PATROL),
        "--mode", "real",
        "--lidar-source", "relay",
        "--relay-bind", "10.42.0.1", "--relay-port", "47621",
        "--locomotion-relay-host", "10.42.0.76",
        "--locomotion-relay-port", "47622",
        "--forward-distance", "2.0", "--return-distance", "4.0",
        "--home-distance", "2.0", "--forward-speed", "0.30",
        "--turn-yaw-rate", "0.50", "--max-lateral-drift", "0.80",
        "--heading-hold", "--loops", "3", "--arm", "--one-cycle",
        "--operator-approved-one-cycle",
    ]


def check_entrypoint(path: Path) -> None:
    if not PYTHON.is_file():
        raise RuntimeError(f"missing existing runtime: {PYTHON}")
    if not path.is_file():
        raise RuntimeError(f"missing entrypoint: {path}")
    subprocess.run([str(PYTHON), str(path), "--help"], cwd=ROOT,
                   check=True, stdout=subprocess.DEVNULL)


def preflight() -> None:
    check_entrypoint(REACTION)
    check_entrypoint(PATROL)
    if not MODEL.is_file() or MODEL.stat().st_size == 0:
        raise RuntimeError(f"missing YOLO model: {MODEL}")
    worker = MOTIONDECODE / "scripts/resident_worker.py"
    if not worker.is_file():
        raise RuntimeError(f"missing MotionDecode resident worker: {worker}")
    sounds = (
        ROOT / "assets/audio/reactions/person/detected.wav",
        ROOT / "assets/audio/reactions/banana/detected.wav",
        ROOT / "assets/audio/reactions/plushie/plushie_affectionate.wav",
    )
    missing = [str(path) for path in sounds if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise RuntimeError("missing reaction WAV: " + ", ".join(missing))
    conflicts = subprocess.run(
        ["ps", "-eo", "pid=,args="], check=True, capture_output=True, text=True
    ).stdout
    own = os.getpid()
    markers = ("run_patrol.py", "g1-wander-reactive-mvp.py", "locomotion_relay.py")
    found = []
    for line in conflicts.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and int(fields[0]) != own and any(m in fields[1] for m in markers):
            found.append(line.strip())
    if found:
        raise RuntimeError("writer conflict: " + "; ".join(found))
    print("PREFLIGHT=PASS", flush=True)
    print("WANDER=OFF", flush=True)


def resident_request(request: dict[str, object]) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2.0)
        client.connect(str(DRY_RUN_SOCKET))
        stream = client.makefile("rwb")
        stream.write((json.dumps(request) + "\n").encode())
        stream.flush()
        response = json.loads(stream.readline())
    if not isinstance(response, dict):
        raise RuntimeError("invalid dry-run resident response")
    return response


def start_dry_run_resident() -> subprocess.Popen[bytes]:
    if DRY_RUN_SOCKET.exists():
        raise RuntimeError(f"dry-run resident socket already exists: {DRY_RUN_SOCKET}")
    process = subprocess.Popen(
        [
            str(PYTHON), "-u", str(MOTIONDECODE / "scripts/resident_worker.py"),
            "--dry-run", "--reaction", "found", "--reaction", "surprise",
            "--socket", str(DRY_RUN_SOCKET),
        ],
        cwd=MOTIONDECODE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("MotionDecode dry-run resident exited during startup")
        if DRY_RUN_SOCKET.exists():
            status = resident_request({"operation": "status"})
            preflight_result = resident_request({"operation": "preflight"})
            if (status.get("state") == "READY" and status.get("mode") == "dry-run"
                    and preflight_result.get("passed") is True):
                print(f"MOTIONDECODE_DRY_RUN_PID={process.pid}", flush=True)
                print("MOTIONDECODE_PREFLIGHT=PASS", flush=True)
                return process
        time.sleep(0.05)
    stop_group(process, "MOTIONDECODE_DRY_RUN")
    raise RuntimeError("MotionDecode dry-run resident did not become READY")


def run_patrol_dry_run() -> None:
    subprocess.run(
        [str(PYTHON), str(PATROL), "--mode", "dry-run", "--loops", "1"],
        cwd=ROOT, check=True,
    )


def stop_group(process: subprocess.Popen[bytes] | None, name: str) -> None:
    if process is None or process.poll() is not None:
        return
    print(f"STOPPING={name}", flush=True)
    os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.duration <= 0:
        raise SystemExit("--duration must be positive")
    if args.real_patrol and not args.operator_approved_real_patrol:
        raise SystemExit("--real-patrol requires --operator-approved-real-patrol")
    if args.no_locomotion and args.operator_approved_real_patrol:
        raise SystemExit("real Patrol approval is invalid with --no-locomotion")

    preflight()
    reaction = reaction_command(args)
    patrol = patrol_command()
    if args.dry_run:
        run_patrol_dry_run()
        print("REACTION_COMMAND=" + subprocess.list2cmdline(reaction), flush=True)
        print("PATROL_COMMAND=" + ("NOT ARMED" if args.no_locomotion
                                    else subprocess.list2cmdline(patrol)), flush=True)
        print("G1_WALK_COMMAND_SENT=NONE", flush=True)
        return 0

    reaction_process: subprocess.Popen[bytes] | None = None
    patrol_process: subprocess.Popen[bytes] | None = None
    motiondecode_process: subprocess.Popen[bytes] | None = None
    try:
        if args.no_locomotion:
            motiondecode_process = start_dry_run_resident()
        reaction_process = subprocess.Popen(reaction, cwd=ROOT, start_new_session=True)
        print(f"REACTION_PID={reaction_process.pid}", flush=True)
        if args.no_locomotion:
            run_patrol_dry_run()
            print("LOCOMOTION=NOT ARMED", flush=True)
            return reaction_process.wait()
        patrol_process = subprocess.Popen(patrol, cwd=ROOT, start_new_session=True)
        print(f"PATROL_PID={patrol_process.pid}", flush=True)
        while True:
            reaction_status = reaction_process.poll()
            patrol_status = patrol_process.poll()
            if patrol_status is not None:
                return patrol_status
            if reaction_status is not None:
                return reaction_status
            time.sleep(0.2)
    except KeyboardInterrupt:
        return 130
    finally:
        # Safety order: Patrol (whose adapter sends STOP), then Reaction.
        stop_group(patrol_process, "PATROL")
        stop_group(reaction_process, "REACTION")
        stop_group(motiondecode_process, "MOTIONDECODE_DRY_RUN")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"INTEGRATED DEMO ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
