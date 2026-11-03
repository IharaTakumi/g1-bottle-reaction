#!/usr/bin/env python3
"""Operate the G1 sneaking game: check, worker, dry, run, stop.

Only `run` walks the robot, and it needs a typed confirmation or --yes.
SSH (camera, reactions) uses Tailscale, which has no stalls on the G1 link; the
UDP walking relays stay on the LAN. Override with G1_SSH_TARGET and G1_LAN_HOST.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from g1_bottle_reaction.adapters.motiondecode_reaction import GAME_REACTIONS  # noqa: E402

SSH_TARGET = os.environ.get("G1_SSH_TARGET", "unitree@100.78.135.14")
LAN_HOST = os.environ.get("G1_LAN_HOST", "192.168.0.82")
PYTHON = "/home/ubuntu/.venvs/g1-game-vision/bin/python"
MOTIONDECODE = Path("/home/ubuntu/dev/motiondecode-test")
YOLO_MODEL = Path("/home/ubuntu/dev/g1-bottle-reaction/.runtime/models/yolo11n.pt")
REF_POSE = MOTIONDECODE / "output/fast_game_reactions_20260916/found_replacement_target_retry4/fresh_q0.json"
RUNTIME = "/home/unitree/g1-runtime/motiondecode"
RELAY_DIR = "/home/unitree/g1-patrol-relays"
RELAY_PYTHON = "/home/unitree/g1-bottle-reaction-wander/.venv-wander/bin/python"
RELAY_LOG = "/tmp/g1-game-relay-{}.log"
RELAY_MARKER = "g1-game-relay"
# found stays loaded because the runtime readiness check requires it.
WORKER_REACTIONS = ",".join(["found", *sorted(GAME_REACTIONS)])
MINUTES_MAX_LAPS = 100

OK, WARN, FAIL = "OK", "WARN", "FAIL"

STATE_PROBE = """
import json, sys, time
sys.path.insert(0, "scripts")
from robot_transport import ReadOnlyState, state_summary
reader = ReadOnlyState("eth0", [], "0", lowstate_backend="cyclonedds"); reader.wait(1.0)
tilt = gyro = 0.
for _ in range(60):
    s = state_summary(reader.get()); time.sleep(.03)
    tilt = max(tilt, max(abs(x) for x in s["imu_rpy"][:2])); gyro = max(gyro, max(abs(x) for x in s["gyro"]))
print("STATE=" + json.dumps({"tilt": tilt, "gyro": gyro, "all_q": list(map(float, s["all_q"])),
                             "temps": list(map(int, s["arm_temperature"])),
                             "motor_error": any(s["arm_motorstate"])}))
"""


def ssh(command: str, *, timeout: float = 30., stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "--", SSH_TARGET, command],
        input=stdin, capture_output=True, text=True, timeout=timeout)


def evaluate_state(state: dict, reference: list[float]) -> list[tuple[str, str, str]]:
    """Judge a standing G1 against the start limits; only hard limits are FAIL."""
    upper = max(abs(a - b) for a, b in zip(state["all_q"][12:], reference[12:]))
    temp = max(state["temps"])
    return [
        ("motors", FAIL if state["motor_error"] else OK, "error" if state["motor_error"] else "no error"),
        ("arm temperature", FAIL if temp >= 65 else WARN if temp >= 60 else OK, f"{temp} C (stop at 65)"),
        ("tilt", FAIL if state["tilt"] > .20 else WARN if state["tilt"] > .12 else OK, f"{state['tilt']:.3f} rad"),
        ("gyro", FAIL if state["gyro"] > .50 else WARN if state["gyro"] > .10 else OK, f"{state['gyro']:.3f} rad/s"),
        ("upper-body pose", FAIL if upper > .30 else WARN if upper > .10 else OK,
         f"{upper:.3f} rad from reference; re-stand G1 if arms look wrong"),
    ]


def tailscale_route(status_text: str, host: str) -> tuple[str, str]:
    for line in status_text.splitlines():
        if line.startswith(host):
            if "direct" in line:
                return OK, "direct"
            if "relay" in line:
                return WARN, "relayed: expect latency, check the network"
            return WARN, line.split("linux", 1)[-1].strip() or "idle"
    return FAIL, "not listed"


def check() -> int:
    results: list[tuple[str, str, str]] = []
    lan = subprocess.run(["ping", "-c", "3", "-i", "0.2", "-W", "1", LAN_HOST], capture_output=True, text=True)
    results.append(("LAN to walking relay", OK if lan.returncode == 0 else FAIL, LAN_HOST))
    for path in (YOLO_MODEL, REF_POSE, Path(PYTHON)):
        results.append((path.name, OK if path.exists() else FAIL, str(path)))
    busy = subprocess.run(["pgrep", "-fa", "[r]un_patrol.py|[g]1_dual_camera.py|[r]un_integrated_demo.py"],
                          capture_output=True, text=True).stdout.strip()
    results.append(("no game running on PC", WARN if busy else OK, busy[:80] or "clear"))
    remote = ssh(
        f"sed -n 2p {RUNTIME}/BUNDLE_INFO.txt; ls /dev/v4l/by-id/ 2>/dev/null | head -2; "
        f"sha256sum {RELAY_DIR}/locomotion_relay.py {RELAY_DIR}/lidar_guard_relay.py | cut -c1-12; "
        f"pgrep -fa '[l]ocomotion_relay|[r]un_patrol' | head -2", timeout=25)
    if remote.returncode != 0:
        results.append(("SSH", FAIL, remote.stderr.strip()[:90] or "no response"))
    else:
        results.append(("SSH", OK, SSH_TARGET))
        lines = remote.stdout.splitlines()
        head = subprocess.run(["git", "-C", str(MOTIONDECODE), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        deployed = lines[0].split()[-1] if lines else "?"
        results.append(("bundle on G1", OK if deployed == head else WARN, f"{deployed[:7]}, local HEAD {head[:7]}"))
        results.append(("camera", OK if any("video-index" in x for x in lines) else WARN,
                        next((x for x in lines if "video-index0" in x), "no USB camera; will try the video service")))
        local = [subprocess.run(["sha256sum", str(ROOT / "patrol" / f)], capture_output=True, text=True).stdout[:12]
                 for f in ("locomotion_relay.py", "lidar_guard_relay.py")]
        remote_hashes = [x for x in lines if re.fullmatch(r"[0-9a-f]{12}", x)]
        results.append(("walking relays on G1", OK if remote_hashes == local else FAIL, "match" if remote_hashes == local else "differ"))
        state = ssh(f"cd {RUNTIME}/code && PYTHONPATH={RUNTIME}/deps:scripts:/home/unitree/unitree_sdk2_python "
                    "LD_LIBRARY_PATH=/home/unitree/work/unitree_sdk2/thirdparty/lib/aarch64 python3 -B -",
                    timeout=30, stdin=STATE_PROBE)
        found = re.search(r"^STATE=(.*)$", state.stdout, re.M)
        if found:
            results += evaluate_state(json.loads(found.group(1)), json.loads(REF_POSE.read_text())["all_q"])
        else:
            results.append(("robot state", FAIL, state.stderr.strip()[-90:] or "no LowState"))
        worker = worker_status()
        results.append(("resident worker", OK if worker.get("state") == "READY" else WARN,
                        f"{worker.get('state', 'not running')}; failed preflight: "
                        f"{[k for k, v in (worker.get('preflight') or {}).items() if not v]}"))
    # After the SSH traffic above, so the route shows direct or relayed, not idle.
    host = SSH_TARGET.rsplit("@", 1)[-1]
    if shutil.which("tailscale") and host.startswith("100."):
        status = subprocess.run(["tailscale", "status"], capture_output=True, text=True, timeout=10)
        results.append(("tailscale route", *tailscale_route(status.stdout, host)))
    for name, level, detail in results:
        print(f"[{level:4s}] {name}: {detail}")
    failed = [name for name, level, _ in results if level == FAIL]
    print("CHECK=" + ("FAIL " + ", ".join(failed) if failed else "PASS"))
    return 1 if failed else 0


def worker_status() -> dict:
    result = ssh(f"cd {RUNTIME}/code && PYTHONPATH={RUNTIME}/deps:scripts python3 -B "
                 "scripts/resident_worker_client.py --timeout 3 2>/dev/null | tail -1", timeout=15)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def start_worker() -> int:
    env = dict(os.environ, G1_SSH_TARGET=SSH_TARGET, MOTIONDECODE_REACTIONS=WORKER_REACTIONS)
    subprocess.run(["bash", str(ROOT / "scripts/start-motiondecode-resident.sh")], env=env, timeout=400)
    status = worker_status()
    print("WORKER=" + str(status.get("state", "NOT RUNNING")), "failed preflight:",
          [k for k, v in (status.get("preflight") or {}).items() if not v])
    return 0 if status.get("state") == "READY" else 1


def relay_commands(pc_ip: str) -> dict[str, list[str]]:
    return {
        "lidar_guard_relay": ["--interface", "eth0", "--destination", pc_ip, "--port", "47621",
                              "--seconds", "3600", "--guard-path", f"{RELAY_DIR}/lidar_guard.py",
                              "--hackathon-runtime"],
        "locomotion_relay": ["--bind", LAN_HOST, "--port", "47622", "--interface", "eth0",
                             "--watchdog-timeout", "0.40", "--telemetry-host", pc_ip,
                             "--telemetry-port", "47623", "--hackathon-runtime", "--arm",
                             "--start-mapping-if-needed"],
    }


def relay_start_script(pc_ip: str) -> str:
    """Start relays so SIGTERM runs their cleanup (stop walking, close SLAM)."""
    lines = ["set -e", "cd /home/unitree/g1-bottle-reaction-wander"]
    for name, args in relay_commands(pc_ip).items():
        wrapper = ("import runpy, signal, sys; "
                   "[signal.signal(s, signal.default_int_handler) for s in (signal.SIGINT, signal.SIGTERM)]; "
                   f"sys.path.insert(0, {RELAY_DIR!r}); sys.argv = {[name] + args!r}; "
                   f"runpy.run_path({RELAY_DIR + '/' + name + '.py'!r}, run_name='__main__')")
        lines.append(f"nohup setsid {RELAY_PYTHON} -B -c {shlex.quote(wrapper + ' #' + RELAY_MARKER)} "
                     f"> {RELAY_LOG.format(name)} 2>&1 < /dev/null &")
    lines += ["sleep 4", f"pgrep -f {RELAY_MARKER} | grep -v $$ | wc -l",
              f"tail -n 2 {RELAY_LOG.format('lidar_guard_relay')} {RELAY_LOG.format('locomotion_relay')}"]
    return "\n".join(lines)


def stop_relays() -> None:
    ssh(f"pkill -TERM -f '[{RELAY_MARKER[0]}]{RELAY_MARKER[1:]}'; sleep 3; "
        f"pkill -KILL -f '[{RELAY_MARKER[0]}]{RELAY_MARKER[1:]}'; true", timeout=20)


def supervisor_args(mode: list[str], duration: float, laps: int = 1) -> list[str]:
    return [PYTHON, "-B", str(ROOT / "scripts/run_integrated_demo.py"), *mode, "--headless",
            "--duration", str(duration), "--laps", str(laps), "--ssh-target", SSH_TARGET,
            "--patrol-host", LAN_HOST, "--yolo-model", str(YOLO_MODEL)]


def dry(duration: float) -> int:
    return subprocess.run(supervisor_args(["--no-locomotion"], duration), cwd=ROOT).returncode


def run_plan(laps: int | None, minutes: float | None) -> tuple[int, float, str]:
    """Laps finish on their own; a timed game ends at the duration."""
    if minutes is not None:
        if not 0 < minutes <= 30:
            raise ValueError("--minutes must be within (0, 30]")
        return MINUTES_MAX_LAPS, minutes * 60., f"{minutes:g} minutes"
    laps = 3 if laps is None else laps
    if not 1 <= laps <= 100:
        raise ValueError("--laps must be within 1..100")
    return laps, 1800., f"{laps} laps"


def confirmed(summary: str, assume_yes: bool, read=input) -> bool:
    if assume_yes:
        return True
    print(f"G1 WILL WALK: {summary}. Clear the course and hold the remote.")
    try:
        return read("Type WALK to start: ").strip() == "WALK"
    except EOFError:
        return False


def run(laps: int | None, minutes: float | None, assume_yes: bool) -> int:
    loops, duration, summary = run_plan(laps, minutes)
    if check() != 0:
        print("REFUSED: fix the FAIL items first")
        return 1
    if worker_status().get("state") != "READY":
        print("REFUSED: resident worker is not READY; run the worker command")
        return 1
    if not confirmed(summary, assume_yes):
        print("CANCELLED: no G1 command sent")
        return 1
    pc_ip = json.loads(subprocess.run(["ip", "-j", "route", "get", LAN_HOST], capture_output=True,
                                      text=True, check=True).stdout)[0]["prefsrc"]
    try:
        started = ssh(relay_start_script(pc_ip), timeout=30)
        print(started.stdout[-400:], started.stderr[-200:])
        if started.returncode != 0:
            return 1
        return subprocess.run(
            supervisor_args(["--real-patrol", "--operator-approved-real-patrol"], duration, loops),
            cwd=ROOT).returncode
    finally:
        stop_relays()


def stop() -> int:
    """Stop everything this tool starts and release the robot."""
    for pattern in ("run_integrated_demo.py", "run_patrol.py", "g1_dual_camera.py"):
        subprocess.run(["pkill", "-INT", "-f", pattern])
    stop_relays()
    ssh(f"cd {RUNTIME}/code && PYTHONPATH={RUNTIME}/deps:scripts python3 -B - <<'EOF' 2>/dev/null\n"
        "from resident_protocol import unix_request\n"
        "print(unix_request('/tmp/motiondecode-reaction.sock', {'operation': 'stop'}, timeout=5))\nEOF\n"
        "sleep 2; P=$(cat /tmp/motiondecode-resident.pid 2>/dev/null); [ -n \"$P\" ] && kill \"$P\" 2>/dev/null; "
        "rm -f /tmp/motiondecode-reaction.sock /tmp/motiondecode-resident.pid; true", timeout=30)
    left = ssh(f"pgrep -fa '[r]esident_worker|[{RELAY_MARKER[0]}]{RELAY_MARKER[1:]}|[g]st-launch' | cut -c1-70; true", timeout=15)
    print("STILL RUNNING ON G1:\n" + left.stdout if left.stdout.strip() else "RELEASED")
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    commands = value.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="read-only readiness check")
    commands.add_parser("worker", help="start the resident reaction worker (no motion)")
    dry_parser = commands.add_parser("dry", help="camera, YOLO and reaction choice; robot stays still")
    dry_parser.add_argument("--duration", type=float, default=30.)
    run_parser = commands.add_parser("run", help="walk the patrol with reactions")
    group = run_parser.add_mutually_exclusive_group()
    group.add_argument("--laps", type=int)
    group.add_argument("--minutes", type=float)
    run_parser.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    commands.add_parser("stop", help="stop everything and release the robot")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "check":
        return check()
    if args.command == "worker":
        return start_worker()
    if args.command == "dry":
        return dry(args.duration)
    if args.command == "run":
        return run(args.laps, args.minutes, args.yes)
    return stop()


if __name__ == "__main__":
    raise SystemExit(main())
