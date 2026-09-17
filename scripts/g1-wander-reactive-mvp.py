#!/usr/bin/env python3
"""Run fail-closed Mapless Wander as sequential bounded motion pulses."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robot_side.adapters.g1_robot import UnitreeSdkRuntime
from robot_side.wander_reactive_mvp import ReactiveMvpPlan, run_reactive_mvp


def body_writer_conflicts():
    result = subprocess.run(
        ["ps", "-eo", "pid=,comm=,args="], check=True, capture_output=True,
        text=True, timeout=5)
    markers = (
        "resident_worker.py", "motiondecode", "g1-wander-loco-once.py",
        "g1-wander-forward-distance.py", "g1-wander-reactive-mvp.py",
    )
    conflicts = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 3:
            continue
        pid, command, args = fields
        if int(pid) == os.getpid() or "python" not in command.lower():
            continue
        if any(marker in args.lower() for marker in markers):
            conflicts.append(line.strip())
    return conflicts


class JsonlReactiveTelemetry:
    """Keep DDS point-cloud work isolated from the locomotion RPC process."""

    def __init__(self, interface, config_path):
        self.lock = threading.Lock()
        self.state = {
            "odom": None, "cloud_changed": None, "cloud_valid": False,
            "cloud_invalid_reason": "missing", "obstacle_snapshot": None,
            "error": None,
        }
        self.latest_cloud_stamp = None
        command = [
            sys.executable, "-B", str(ROOT / "scripts/g1-wander-live-source.py"),
            "--interface", interface, "--config", str(config_path),
        ]
        self.process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            bufsize=1)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                record = json.loads(line)
                stamp = record.get("cloud_source_timestamp")
                valid = bool(record.get("valid"))
                with self.lock:
                    if stamp is not None and stamp != self.latest_cloud_stamp:
                        self.latest_cloud_stamp = stamp
                        self.state["cloud_changed"] = time.monotonic()
                    self.state["cloud_valid"] = valid
                    self.state["cloud_invalid_reason"] = record.get("invalid_reason")
                    self.state["obstacle_snapshot"] = record.get("obstacle_snapshot")
                    self.state["odom"] = record.get("odom")
            if self.process.poll() not in (None, 0):
                detail = self.process.stderr.read().strip()
                with self.lock:
                    self.state["error"] = "LiDAR source exited: " + detail[-500:]
        except BaseException as exc:
            with self.lock:
                self.state["error"] = "%s: %s" % (type(exc).__name__, exc)

    def latest(self):
        now = time.monotonic()
        with self.lock:
            state = dict(self.state)
        state["cloud_age_s"] = (
            None if state["cloud_changed"] is None
            else max(0.0, now - state["cloud_changed"]))
        return state

    def wait_ready(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.latest()
            if state["error"]:
                raise RuntimeError(state["error"])
            if state["cloud_changed"] is not None:
                return state
            time.sleep(0.01)
        raise RuntimeError("timed out waiting for MID-360")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise RuntimeError("read-only LiDAR source did not terminate")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-seconds", type=float, default=30.0)
    parser.add_argument("--max-pulses", type=int, default=100)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--interface", default="eth0")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--config", type=Path, default=ROOT / "config/wander_live_pc2.json")
    parser.add_argument("--robot", choices=("mock", "g1"), default="mock")
    parser.add_argument("--enable-real-robot", action="store_true")
    parser.add_argument("--execute-real-g1", action="store_true")
    parser.add_argument("--i-understand-this-will-move-the-robot", action="store_true")
    return parser


def main(argv=None, runtime=None):
    args = build_parser().parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    plan = ReactiveMvpPlan(
        run_seconds=args.run_seconds, max_pulses=args.max_pulses,
        blocked_distance_m=float(config["blocked_distance_m"]), seed=args.seed)
    plan.validate()
    print("MVP INTENT: forward=0.20/0.50 turn=+/-0.25/1.00 blocked=%.2f run=%.1f" % (
        plan.blocked_distance_m, plan.run_seconds), flush=True)
    armed = (args.robot == "g1" and args.enable_real_robot and args.execute_real_g1
             and args.i_understand_this_will_move_the_robot)
    if not armed:
        print("DRY RUN", flush=True)
        print("NO G1 COMMAND SENT", flush=True)
        return 0
    conflicts = body_writer_conflicts()
    if conflicts:
        raise RuntimeError("body writer conflict: " + "; ".join(conflicts))
    runtime = runtime or UnitreeSdkRuntime()
    client = runtime.create_loco_client(args.interface, args.timeout)
    code, fsm_id = client.GetFsmId()
    print("FSM: code=%r id=%r" % (code, fsm_id), flush=True)
    if code != 0 or fsm_id != 501:
        raise RuntimeError("expected locomotion FSM 501, got code=%r id=%r" % (code, fsm_id))
    telemetry = JsonlReactiveTelemetry(args.interface, args.config)
    try:
        result = run_reactive_mvp(
            plan, telemetry, client, body_writer_conflicts,
            emit=lambda value: print("EVENT " + json.dumps(value, sort_keys=True), flush=True))
        print("SUMMARY " + json.dumps(result, sort_keys=True), flush=True)
        return 0 if result["status"] == "pass" else 2
    finally:
        telemetry.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("ERROR: operator abort", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        raise SystemExit(2)
