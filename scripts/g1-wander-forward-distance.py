#!/usr/bin/env python3
"""Commission a bounded forward distance with dog_odom and MID-360 safety."""

import argparse
import json
import math
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
from robot_side.wander_forward_distance import ForwardDistancePlan, run_forward_distance
from robot_side.wander_live import decode_xyz, odom_payload, reduce_points, source_stamp_seconds


CLOUD_TOPIC = "rt/utlidar/cloud_livox_mid360"
ODOM_TOPIC = "rt/dog_odom"


def body_writer_conflicts():
    result = subprocess.run(
        ["ps", "-eo", "pid=,comm=,args="], check=True, capture_output=True,
        text=True, timeout=5)
    conflicts = []
    own_pid = os.getpid()
    markers = ("resident_worker.py", "motiondecode", "g1-wander-loco-once.py",
               "g1-wander-forward-distance.py")
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 3:
            continue
        pid, command, args = fields
        if int(pid) == own_pid or "python" not in command.lower():
            continue
        if any(marker in args.lower() for marker in markers):
            conflicts.append(line.strip())
    return conflicts


class LiveSafetyTelemetry:
    def __init__(self, runtime, config):
        self.config = config
        self.lock = threading.Lock()
        self.state = {"odom": None, "odom_changed": None, "cloud_changed": None,
                      "cloud_valid": False, "cloud_invalid_reason": "missing",
                      "forward_clearance_m": None, "error": None}
        self.latest_odom_stamp = None
        self.latest_cloud_stamp = None
        self.subscribers = runtime.create_readonly_navigation_subscribers(
            self._on_odom, self._on_cloud)

    def start(self):
        return self

    def _on_odom(self, sample):
        try:
            payload = odom_payload(sample)
            stamp = payload["source_timestamp"]
            with self.lock:
                self.state["odom"] = payload
                if stamp is not None and stamp != self.latest_odom_stamp:
                    self.latest_odom_stamp = stamp
                    self.state["odom_changed"] = time.monotonic()
        except BaseException as exc:
            with self.lock:
                self.state["error"] = "%s: %s" % (type(exc).__name__, exc)

    def _on_cloud(self, sample):
        try:
            stamp = source_stamp_seconds(sample.header)
            if stamp is None:
                raise ValueError("cloud source timestamp missing")
            reduced = reduce_points(decode_xyz(sample), self.config, False)
            clearance = min(reduced["obstacle_snapshot"][name]
                            for name in ("front_left", "front", "front_right"))
            with self.lock:
                if stamp != self.latest_cloud_stamp:
                    self.latest_cloud_stamp = stamp
                    self.state["cloud_changed"] = time.monotonic()
                self.state["cloud_valid"] = bool(reduced["valid"])
                self.state["cloud_invalid_reason"] = reduced["invalid_reason"]
                self.state["forward_clearance_m"] = clearance
        except BaseException as exc:
            with self.lock:
                self.state["cloud_valid"] = False
                self.state["cloud_invalid_reason"] = "%s: %s" % (
                    type(exc).__name__, exc)

    def latest(self):
        now = time.monotonic()
        with self.lock:
            state = dict(self.state)
        state["odom_age_s"] = (None if state["odom_changed"] is None else
                               max(0.0, now - state["odom_changed"]))
        state["cloud_age_s"] = (None if state["cloud_changed"] is None else
                                max(0.0, now - state["cloud_changed"]))
        return state

    def wait_ready(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.latest()
            if state["error"]:
                raise RuntimeError(state["error"])
            if state["odom"] is not None and state["cloud_changed"] is not None:
                return state
            time.sleep(0.01)
        raise RuntimeError("timed out waiting for dog_odom and MID-360")

    def close(self):
        for subscriber in self.subscribers.values():
            subscriber.Close()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-distance", type=float, default=1.0)
    parser.add_argument("--speed", type=float, default=0.10)
    parser.add_argument("--pulse-duration", type=float, default=0.50)
    parser.add_argument("--hard-cap", type=float, default=1.20)
    parser.add_argument("--wall-clock-cap", type=float, default=15.0)
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
    plan = ForwardDistancePlan(
        target_distance_m=args.target_distance, speed_m_s=args.speed,
        pulse_duration_s=args.pulse_duration, hard_cap_m=args.hard_cap,
        wall_clock_cap_s=args.wall_clock_cap)
    plan.validate()
    print("DISTANCE INTENT: vx=%.3f pulse=%.3f target=%.3f hard_cap=%.3f wall_cap=%.1f" % (
        plan.speed_m_s, plan.pulse_duration_s, plan.target_distance_m,
        plan.hard_cap_m, plan.wall_clock_cap_s), flush=True)
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
    config = json.loads(args.config.read_text(encoding="utf-8"))
    client = runtime.create_loco_client(args.interface, args.timeout)
    code, fsm_id = client.GetFsmId()
    print("FSM: code=%r id=%r" % (code, fsm_id), flush=True)
    if code != 0:
        raise RuntimeError("GetFsmId returned %r" % (code,))
    telemetry = LiveSafetyTelemetry(runtime, config).start()
    try:
        first = telemetry.wait_ready(3.0)
        print("PREFLIGHT: odom_age=%.3f cloud_age=%.3f clearance=%.3f" % (
            first["odom_age_s"], first["cloud_age_s"],
            first["forward_clearance_m"]), flush=True)
        result = run_forward_distance(
            plan, telemetry, client, body_writer_conflicts,
            emit=lambda value: print("STEP " + json.dumps(value, sort_keys=True), flush=True))
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
