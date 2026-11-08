#!/usr/bin/env python3
"""Operate the G1 sneaking game: check, worker, dry, stand, motion, run, stop.

Only `run` walks the robot, and it needs a typed confirmation or --yes.
`stand` moves the arms and speaks on detections while G1 stays in place.
SSH (camera, reactions) uses Tailscale, which has no stalls on the G1 link; the
UDP walking relays stay on the LAN. Override with G1_SSH_TARGET and G1_LAN_HOST.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import subprocess
import sys
import threading
import time

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
        f"cd {RELAY_DIR} && sha256sum {' '.join(RELAY_FILES)} | cut -c1-12; cd; "
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
                 for f in RELAY_FILES]
        remote_hashes = [x for x in lines if re.fullmatch(r"[0-9a-f]{12}", x)]
        results.append(("walking relays on G1", OK if remote_hashes == local else FAIL,
                        "match" if remote_hashes == local else "differ: run the relays command when G1 is free"))
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


VOICES = ROOT / "assets/audio/reactions"


def reaction_catalog() -> dict:
    """Which motions each detection can trigger in the game, with their paired voices."""
    from g1_bottle_reaction.game_vision.dual import REACTION_MOTION_POOLS
    pools = {target: [{"motion": name,
                       "voices": sorted(f"{target}/{p.name}" for p in (VOICES / target).glob(f"{target}_{name}_*.wav"))}
                      for name in names]
             for target, names in REACTION_MOTION_POOLS.items()}
    used = {name for names in REACTION_MOTION_POOLS.values() for name in names}
    return {"pools": pools, "unused": sorted(GAME_REACTIONS - used)}


def voice_path(voice: str) -> Path:
    """A paired reaction voice, given as <target>/<file>.wav; nothing outside that folder."""
    path = (VOICES / voice).resolve()
    if path.parent.parent != VOICES.resolve() or path.suffix != ".wav" or not path.is_file():
        raise ValueError(f"unknown voice {voice}")
    return path


def motion(name: str, assume_yes: bool, voice: str | None = None) -> int:
    """Play one reaction motion on the standing G1, with or without a voice; no walking."""
    if name not in GAME_REACTIONS:
        print(f"REFUSED: unknown motion {name}; choose from {sorted(GAME_REACTIONS)}")
        return 1
    try:
        sound = voice_path(voice) if voice else None
    except ValueError as exc:
        print(f"REFUSED: {exc}")
        return 1
    busy = subprocess.run(["pgrep", "-f", "[r]un_patrol.py|[g]1_dual_camera.py"], capture_output=True).stdout
    if busy:
        print("REFUSED: a game or standing session is running on this PC")
        return 1
    if not assume_yes:
        try:
            if input(f"G1 ARMS WILL MOVE ({name}). Type ARMS to start: ").strip() != "ARMS":
                print("CANCELLED: no G1 command sent")
                return 1
        except EOFError:
            return 1
    # As in the game: connect the speaker and the worker link first, then send
    # the voice and the motion together, so only the arm hand-over ramp separates them.
    bridge = WorkerBridge()
    speaker = None
    try:
        status = bridge.request({"operation": "status"}, timeout=20)
        if status.get("state") != "READY":
            print(f"REFUSED: resident worker is {status.get('state', 'not reachable')}")
            return 1
        if sound is not None:
            from g1_bottle_reaction.adapters.cached_audio import G1SshAudioOutput
            speaker = G1SshAudioOutput(SSH_TARGET)
            speaker.initialize()
            threading.Thread(target=speaker.play_wav, args=(sound,), daemon=True).start()
            print(f"Audio triggered: {sound}; monotonic={time.monotonic():.6f}", flush=True)
        print(f"Motion reaction triggered: motiondecode:{name}; monotonic={time.monotonic():.6f}", flush=True)
        result = bridge.request({"operation": "execute", "reaction": name,
                                 "trigger_monotonic_s": time.monotonic()}, timeout=60)
    finally:
        bridge.close()
        if speaker is not None:
            speaker.close()
    print("MOTIONDECODE RESULT: " + json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "pass" else 1


class WorkerBridge:
    """One SSH link to the resident worker's stdio client, opened before the trigger."""

    def __init__(self) -> None:
        command = (f"cd {RUNTIME}/code && PYTHONPATH={RUNTIME}/deps:scripts python3 -B "
                   "scripts/resident_worker_client.py --stdio --timeout 40")
        self.process = subprocess.Popen(
            ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "--", SSH_TARGET, command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def request(self, payload: dict, timeout: float) -> dict:
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        if not select.select([self.process.stdout], [], [], timeout)[0]:
            raise RuntimeError("resident worker did not answer")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("resident worker link closed")
        return json.loads(line)

    def close(self) -> None:
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


VOLUME_PROBE = """
import contextlib, json, sys, xml.etree.ElementTree as ET
with contextlib.redirect_stdout(sys.stderr):
    import unitree_sdk2py.core.channel as channel
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
    original = channel.ChannelConfigHasInterface
    root = ET.fromstring(original)
    for parent in root.iter():
        for child in list(parent):
            if child.tag == "Tracing":
                parent.remove(child)
    channel.ChannelConfigHasInterface = ET.tostring(root, encoding="unicode")
    channel.ChannelFactoryInitialize(0, "eth0")
    client = AudioClient(); client.SetTimeout(2.0); client.Init()
    level = int(sys.argv[1]) if len(sys.argv) > 1 else None
    code = client.SetVolume(level) if level is not None else 0
    read_code, value = client.GetVolume()
print("VOLUME=" + json.dumps({"set_code": code, "read_code": read_code, "volume": value}))
"""


def volume(level: int | None) -> int:
    """Read or set the G1 speaker volume; it applies to the next sound at once."""
    if level is not None and not 0 <= level <= 100:
        print("REFUSED: volume must be 0-100")
        return 1
    argument = "" if level is None else f" {level}"
    result = ssh(f"PYTHONPATH=/home/unitree/unitree_sdk2_python python3 -B -{argument}",
                 timeout=30, stdin=VOLUME_PROBE)
    found = re.search(r"^VOLUME=(.*)$", result.stdout, re.M)
    print(found.group(0) if found else "VOLUME FAILED: " + (result.stderr.strip()[-200:] or "no response"))
    return 0 if found and json.loads(found.group(1))["read_code"] == 0 else 1


RELAY_FILES = ("locomotion_relay.py", "lidar_guard_relay.py", "lidar_guard.py", "hackathon_profile.py")


def deploy_relays() -> int:
    """Copy the walking relays to G1; refuses while a relay is running there."""
    running = ssh(f"pgrep -f '[{RELAY_MARKER[0]}]{RELAY_MARKER[1:]}' | head -1; true", timeout=15).stdout.strip()
    if running:
        print("REFUSED: the walking relays are running on G1; stop the game first")
        return 1
    copied = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                             *[str(ROOT / "patrol" / name) for name in RELAY_FILES],
                             f"{SSH_TARGET}:{RELAY_DIR}/"], capture_output=True, text=True, timeout=60)
    print("RELAYS=" + ("copied " + ", ".join(RELAY_FILES) if copied.returncode == 0
                       else "FAILED " + copied.stderr.strip()[-200:]))
    return copied.returncode


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


def supervisor_args(mode: list[str], duration: float, laps: int = 1,
                    speed: float = 0.50, course: float = 4.0) -> list[str]:
    return [PYTHON, "-B", str(ROOT / "scripts/run_integrated_demo.py"), *mode, "--headless",
            "--duration", str(duration), "--laps", str(laps), "--speed", f"{speed:g}",
            "--course-length", f"{course:g}", "--ssh-target", SSH_TARGET,
            "--patrol-host", LAN_HOST, "--yolo-model", str(YOLO_MODEL)]


def dry(duration: float) -> int:
    return subprocess.run(supervisor_args(["--no-locomotion"], duration), cwd=ROOT).returncode


# Patrol interlock arguments: a standing session has no Patrol to pause.
PATROL_ONLY_FLAGS = {"--patrol-control-socket": 1, "--patrol-pause-timeout": 1, "--hackathon-runtime": 0}


def stand_command(duration: float) -> list[str]:
    """Real reactions (arms and voice) on detections, without walking."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import run_integrated_demo
    args = run_integrated_demo.parser().parse_args(
        supervisor_args(["--real-patrol", "--operator-approved-real-patrol"], duration)[3:])
    command, skip = [], 0
    for item in run_integrated_demo.reaction_command(args):
        if skip:
            skip -= 1
        elif item in PATROL_ONLY_FLAGS:
            skip = PATROL_ONLY_FLAGS[item]
        else:
            command.append(item)
    return command


def stand(duration: float, assume_yes: bool) -> int:
    if worker_status().get("state") != "READY":
        print("REFUSED: resident worker is not READY; run the worker command")
        return 1
    if not assume_yes:
        print("G1 ARMS WILL MOVE on detections (no walking). Keep people out of arm reach.")
        try:
            if input("Type ARMS to start: ").strip() != "ARMS":
                print("CANCELLED: no G1 command sent")
                return 1
        except EOFError:
            return 1
    return subprocess.run(stand_command(duration), cwd=ROOT).returncode


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


def run(laps: int | None, minutes: float | None, assume_yes: bool,
        speed: float = 0.50, course: float = 4.0) -> int:
    loops, duration, summary = run_plan(laps, minutes)
    if not 0.10 <= speed <= 0.80 or not 1.0 <= course <= 5.0:
        print("REFUSED: speed must be 0.10-0.80 m/s and course 1-5 m")
        return 1
    summary += f", {speed:g} m/s" + (" (above the validated 0.50 m/s)" if speed > 0.50 else "") + f", course {course:g} m"
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
            supervisor_args(["--real-patrol", "--operator-approved-real-patrol"], duration, loops, speed, course),
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
    stand_parser = commands.add_parser("stand", help="real arm and voice reactions; G1 does not walk")
    stand_parser.add_argument("--duration", type=float, default=90.)
    stand_parser.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    motion_parser = commands.add_parser("motion", help="play one motion on the standing G1")
    motion_parser.add_argument("name")
    motion_parser.add_argument("--voice", help="paired voice as <target>/<file>.wav (default: no voice)")
    motion_parser.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    run_parser = commands.add_parser("run", help="walk the patrol with reactions")
    group = run_parser.add_mutually_exclusive_group()
    group.add_argument("--laps", type=int)
    group.add_argument("--minutes", type=float)
    run_parser.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    run_parser.add_argument("--speed", type=float, default=0.50, help="m/s, 0.10-0.80 (0.50 validated)")
    run_parser.add_argument("--course", type=float, default=4.0, help="course length end to end, 1-5 m")
    volume_parser = commands.add_parser("volume", help="read or set the G1 speaker volume (0-100)")
    volume_parser.add_argument("level", nargs="?", type=int)
    commands.add_parser("relays", help="copy this PC's walking relays to G1 (does not start them)")
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
    if args.command == "motion":
        return motion(args.name, args.yes, args.voice)
    if args.command == "stand":
        return stand(args.duration, args.yes)
    if args.command == "run":
        return run(args.laps, args.minutes, args.yes, args.speed, args.course)
    if args.command == "volume":
        return volume(args.level)
    if args.command == "relays":
        return deploy_relays()
    return stop()


if __name__ == "__main__":
    raise SystemExit(main())
