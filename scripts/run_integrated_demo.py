#!/usr/bin/env python3
"""Run existing Reaction and Patrol entrypoints as isolated child processes."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import threading
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REACTION = ROOT / "tools/g1_dual_camera.py"
PATROL = ROOT / "patrol/run_patrol.py"
MODEL = ROOT / ".runtime/models/yolo11n.pt"
PYTHON = Path("/home/ubuntu/.venvs/g1-game-vision/bin/python")
MOTIONDECODE = Path("/home/ubuntu/dev/motiondecode-test")
DRY_RUN_SOCKET = Path("/tmp/g1-integrated-motiondecode-dry.sock")
PATROL_CONTROL_SOCKET = Path("/tmp/g1-patrol-control.sock")

from patrol.control_lease import load_control_timing
from patrol.cleanup import run_cleanup


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Thin supervisor for the existing camera/YOLO/Reaction and Patrol "
            "processes with a local stop-react-resume interlock."
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
        help="run the existing armed one-loop Patrol with Reaction interlock",
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
    value.add_argument("--yolo-model", type=Path, default=MODEL)
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
        "--yolo-model", str(args.yolo_model),
        "--yolo-confidence", "0.25",
        "--banana-confidence", "0.25",
        "--plushie-confidence", "0.25",
        "--reaction-target", "all",
        "--found-audio",
        "--found-output", "mock" if args.no_locomotion else "g1",
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
            "--patrol-control-socket", str(PATROL_CONTROL_SOCKET),
            "--patrol-pause-timeout", "30",
        ]
    if args.ssh_control:
        command += ["--ssh-control", args.ssh_control]
    command.append("--headless" if args.headless else "--windowed")
    # --with-wander is deliberately absent in both modes.
    return command


def patrol_command(lease_id: str) -> list[str]:
    if not isinstance(lease_id, str) or not lease_id.strip():
        raise ValueError("Patrol requires supervisor lease ID")
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
        "--heading-hold", "--loops", "1", "--arm", "--one-cycle",
        "--operator-approved-one-cycle",
        "--control-socket", str(PATROL_CONTROL_SOCKET), "--start-paused",
        "--require-control-lease", "--control-lease-id", lease_id,
    ]


def check_entrypoint(path: Path) -> None:
    if not PYTHON.is_file():
        raise RuntimeError(f"missing existing runtime: {PYTHON}")
    if not path.is_file():
        raise RuntimeError(f"missing entrypoint: {path}")
    subprocess.run([str(PYTHON), str(path), "--help"], cwd=ROOT,
                   check=True, stdout=subprocess.DEVNULL)


def preflight(args: argparse.Namespace) -> None:
    check_entrypoint(REACTION)
    check_entrypoint(PATROL)
    if not args.yolo_model.is_file() or args.yolo_model.stat().st_size == 0:
        raise RuntimeError(f"missing YOLO model: {args.yolo_model}")
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


def patrol_request(request_body: dict[str, object], timeout: float = 5.0) -> dict[str, object]:
    from patrol.control_ipc import request

    response = request(PATROL_CONTROL_SOCKET, request_body, timeout=timeout)
    if response.get("ok") is not True:
        raise RuntimeError(str(response.get("error") or "Patrol control failed"))
    return response


def refresh_patrol_lease(lease_id: str, interval: float) -> dict[str, object]:
    status = patrol_request({"operation": "heartbeat", "lease_id": lease_id}, timeout=interval)
    if (status.get("lease_active") is not True or status.get("error")
            or status.get("stopped")):
        raise RuntimeError("Patrol supervisor lease is not active")
    return status


def patrol_request_while_heartbeating(request_body: dict[str, object],
                                     lease_id: str, interval: float,
                                     timeout: float = 5.0) -> dict[str, object]:
    """Only main sends heartbeats; one bounded worker waits for this request.

    A stuck request cannot extend the main-owned lease beyond the overall
    deadline. A stuck main cannot be masked by the request worker.
    """
    if not all(math.isfinite(v) and v > 0 for v in (timeout, interval)):
        raise ValueError("request timeout and heartbeat interval must be positive")
    deadline = time.monotonic() + timeout
    done = threading.Event()
    result = []
    errors = []

    def request_once():
        try:
            result.append(patrol_request(request_body, timeout=timeout))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=request_once, name="patrol-request", daemon=True)
    worker.start()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Patrol request overall deadline exceeded")
        if done.is_set():
            if errors:
                raise errors[0]
            return result[0]
        refresh_patrol_lease(lease_id, interval)
        done.wait(min(interval, max(0.0, deadline - time.monotonic())))


def wait_for_patrol_paused(process: subprocess.Popen[bytes], lease_id: str,
                           interval: float) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Patrol exited before control socket became ready")
        if PATROL_CONTROL_SOCKET.exists():
            try:
                status = refresh_patrol_lease(lease_id, interval)
            except (OSError, RuntimeError):
                pass
            else:
                if status.get("paused") and not status.get("error"):
                    print("PATROL_INTERLOCK=PAUSED_READY", flush=True)
                    return
        time.sleep(interval)
    raise RuntimeError("Patrol did not reach initial PAUSED state")


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
            session = status.get("session_id")
            preflight_result = {"passed": False}
            if (status.get("accepted") is True and status.get("state") == "READY"
                    and type(status.get("protocol_version")) is int
                    and status["protocol_version"] == 2
                    and status.get("mode") == "dry-run"
                    and isinstance(session, str) and session.strip()):
                preflight_result = resident_request({
                    "operation": "preflight_bound", "expected_mode": "dry-run",
                    "expected_session_id": session,
                })
                if preflight_result.get("accepted") is not True:
                    stop_group(process, "MOTIONDECODE_DRY_RUN")
                    raise RuntimeError(f"MotionDecode bound preflight rejected: {preflight_result}")
            if (status.get("state") == "READY" and status.get("mode") == "dry-run"
                    and preflight_result.get("accepted") is True
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
    if process is None:
        return

    def attempt(sig, timeout):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass  # Child exited between poll and signal; still reap it below.
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if sig == signal.SIGKILL:
                raise

    # A failed signal/wait must not skip escalation or the other children.
    run_cleanup([
        (f"{name} SIGINT", lambda: attempt(signal.SIGINT, 8)),
        (f"{name} SIGTERM", lambda: attempt(signal.SIGTERM, 4)),
        (f"{name} SIGKILL", lambda: attempt(signal.SIGKILL, 4)),
    ])


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.duration <= 0:
        raise SystemExit("--duration must be positive")
    if args.real_patrol and not args.operator_approved_real_patrol:
        raise SystemExit("--real-patrol requires --operator-approved-real-patrol")
    if args.no_locomotion and args.operator_approved_real_patrol:
        raise SystemExit("real Patrol approval is invalid with --no-locomotion")

    preflight(args)
    reaction = reaction_command(args)
    timing = load_control_timing()
    lease_id = str(uuid.uuid4())
    patrol = patrol_command(lease_id)
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
    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    previous_handlers = {sig: signal.signal(sig, terminate)
                         for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if args.no_locomotion:
            motiondecode_process = start_dry_run_resident()
        if args.no_locomotion:
            reaction_process = subprocess.Popen(reaction, cwd=ROOT, start_new_session=True)
            print(f"REACTION_PID={reaction_process.pid}", flush=True)
            run_patrol_dry_run()
            print("LOCOMOTION=NOT ARMED", flush=True)
            return reaction_process.wait()
        patrol_process = subprocess.Popen(patrol, cwd=ROOT, start_new_session=True)
        print(f"PATROL_PID={patrol_process.pid}", flush=True)
        wait_for_patrol_paused(patrol_process, lease_id, timing.heartbeat_interval_s)
        reaction_process = subprocess.Popen(reaction, cwd=ROOT, start_new_session=True)
        print(f"REACTION_PID={reaction_process.pid}", flush=True)
        # Heartbeat runs in the supervising thread: a hung supervisor cannot
        # be masked by a free-running sender thread. Any send failure exits.
        release_at = time.monotonic() + 2.0
        while time.monotonic() < release_at:
            refresh_patrol_lease(lease_id, timing.heartbeat_interval_s)
            if reaction_process.poll() is not None or patrol_process.poll() is not None:
                raise RuntimeError("Child exited before Patrol release")
            time.sleep(timing.heartbeat_interval_s)
        if reaction_process.poll() is not None:
            raise RuntimeError("Reaction exited before Patrol release")
        refresh_patrol_lease(lease_id, timing.heartbeat_interval_s)
        status = patrol_request_while_heartbeating(
            {"operation": "resume"}, lease_id, timing.heartbeat_interval_s)
        if status.get("paused") or status.get("stopped") or status.get("error"):
            raise RuntimeError(str(status.get("error") or "Patrol resume failed"))
        print("PATROL_INTERLOCK=RUNNING", flush=True)
        while True:
            reaction_status = reaction_process.poll()
            patrol_status = patrol_process.poll()
            if patrol_status is not None:
                return patrol_status
            if reaction_status is not None:
                return reaction_status
            refresh_patrol_lease(lease_id, timing.heartbeat_interval_s)
            time.sleep(timing.heartbeat_interval_s)
    except KeyboardInterrupt:
        return 130
    finally:
        # Safety order: Patrol (whose adapter sends STOP), then Reaction.
        run_cleanup([
            ("Patrol process", lambda: stop_group(patrol_process, "PATROL")),
            ("Reaction process", lambda: stop_group(reaction_process, "REACTION")),
            ("Resident process", lambda: stop_group(motiondecode_process, "MOTIONDECODE_DRY_RUN")),
            *[(f"restore {sig.name}", lambda s=sig, h=handler: signal.signal(s, h))
              for sig, handler in previous_handlers.items()],
        ])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"INTEGRATED DEMO ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
