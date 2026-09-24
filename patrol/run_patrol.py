#!/usr/bin/env python3
"""Dry-run, stationary LiDAR check, or explicitly armed one-cycle patrol."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from lidar_guard import GuardState, LidarGuard
from locomotion_adapter import DryRunLocomotionAdapter, UdpLocomotionAdapter
from patrol_controller import PatrolConfig, PatrolController
from udp_relay_guard import UdpRelayGuard

CLOUD_TOPIC = "rt/utlidar/cloud_livox_mid360"


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--mode", choices=("dry-run", "lidar-check", "real"), default="dry-run")
    value.add_argument("--network-interface", default="enp129s0")
    value.add_argument("--seconds", type=float, default=10.0, help="stationary LiDAR observation time")
    value.add_argument("--lidar-source", choices=("relay", "direct"), default="relay")
    value.add_argument("--relay-bind", default="10.42.0.1")
    value.add_argument("--relay-port", type=int, default=47621)
    value.add_argument("--locomotion-relay-host", default="10.42.0.76")
    value.add_argument("--locomotion-relay-port", type=int, default=47622)
    value.add_argument("--forward-speed", type=float, default=0.30)
    value.add_argument("--forward-distance", type=float, default=2.0)
    value.add_argument("--return-distance", type=float, default=4.0)
    value.add_argument("--home-distance", type=float, default=2.0)
    value.add_argument("--turn-yaw-rate", type=float, default=0.50)
    value.add_argument("--turn-angle-degrees", type=float, default=180.0)
    value.add_argument("--max-lateral-drift", type=float, default=0.80)
    value.add_argument("--heading-hold", action=argparse.BooleanOptionalAction, default=True)
    value.add_argument("--heading-kp", type=float, default=0.8)
    value.add_argument("--heading-max-yaw", type=float, default=0.12)
    value.add_argument("--heading-deadband-deg", type=float, default=2.0)
    value.add_argument("--loops", type=int, default=1)
    value.add_argument("--arm", action="store_true")
    value.add_argument("--one-cycle", action="store_true")
    value.add_argument("--turn-only", action="store_true")
    value.add_argument("--operator-approved-one-cycle", action="store_true")
    value.add_argument("--operator-approved-turn-only", action="store_true")
    return value


def config(args) -> PatrolConfig:
    if not 0 < args.forward_speed <= 0.30:
        raise ValueError("forward speed must be in (0, 0.30]")
    if args.forward_distance <= 0 or args.return_distance <= 0 or args.home_distance <= 0:
        raise ValueError("distances must be positive")
    if not 0 < abs(args.turn_yaw_rate) <= 0.50:
        raise ValueError("turn yaw rate must be in (0, 0.50]")
    if not 0 < args.turn_angle_degrees <= 360:
        raise ValueError("turn angle must be in (0, 360]")
    if args.max_lateral_drift <= 0:
        raise ValueError("max lateral drift must be positive")
    if args.heading_kp < 0 or not 0 < args.heading_max_yaw <= 0.50:
        raise ValueError("heading gains must be non-negative and within yaw limits")
    if not 0 <= args.heading_deadband_deg < 45:
        raise ValueError("heading deadband must be within [0, 45) degrees")
    if not 1 <= args.loops <= 100:
        raise ValueError("loops must be within 1..100")
    import math
    return PatrolConfig(
        forward_speed_m_s=args.forward_speed,
        forward_distance_m=args.forward_distance,
        return_distance_m=args.return_distance,
        home_distance_m=args.home_distance,
        turn_yaw_rate_rad_s=args.turn_yaw_rate,
        turn_angle_rad=math.radians(args.turn_angle_degrees),
        max_lateral_drift_m=args.max_lateral_drift,
        heading_hold=args.heading_hold,
        heading_kp=args.heading_kp,
        heading_max_yaw_rad_s=args.heading_max_yaw,
        heading_deadband_rad=math.radians(args.heading_deadband_deg),
    )


class AlwaysClear:
    def state(self, _direction):
        return GuardState.CLEAR


class VirtualTime:
    def __init__(self):
        self.now = 0.0
    def clock(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class DDSLidarSource:
    def __init__(self, interface: str, guard: LidarGuard):
        socket.if_nametoindex(interface)
        vendor = Path(os.environ.get(
            "G1_SDK_PATH",
            "/home/ubuntu/dev/g1-bottle-reaction/.vendor/unitree_sdk2_python",
        ))
        if not (vendor / "unitree_sdk2py/__init__.py").is_file():
            raise RuntimeError(f"Unitree SDK not found: {vendor}")
        sys.path.insert(0, str(vendor))
        from unitree_sdk2py.idl.sensor_msgs.msg.dds_ import PointCloud2_
        from cyclonedds.domain import Domain, DomainParticipant
        from cyclonedds.internal import InvalidSample
        from cyclonedds.qos import Policy, Qos
        from cyclonedds.sub import DataReader
        from cyclonedds.topic import Topic

        dds_xml = (
            '<CycloneDDS><Domain Id="0"><General><Interfaces>'
            f'<NetworkInterface name="{interface}"/>'
            '</Interfaces></General><Tracing><Verbosity>none</Verbosity>'
            '</Tracing></Domain></CycloneDDS>'
        )
        # Domain owns the XML string for this process; this creates DataReaders
        # only and avoids Unitree's architecture-specific channel initializer.
        self.domain = Domain(0, dds_xml)
        self.participant = DomainParticipant(0)
        qos = Qos(Policy.Reliability.BestEffort, Policy.Durability.Volatile,
                  Policy.History.KeepLast(1))
        self.topic = Topic(self.participant, CLOUD_TOPIC, PointCloud2_)
        self.reader = DataReader(self.participant, self.topic, qos)
        self._invalid_sample = InvalidSample
        self._guard = guard
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, name="patrol-lidar-reader", daemon=True)
        self._thread.start()

    def _poll(self):
        while not self._stop.is_set():
            for sample in self.reader.take(1):
                if not isinstance(sample, self._invalid_sample):
                    self._guard.update_cloud(sample)
            time.sleep(0.005)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=1.0)


def writer_conflicts() -> list[str]:
    output = subprocess.run(["ps", "-eo", "pid=,comm=,args="], check=True,
                            capture_output=True, text=True, timeout=5).stdout
    markers = ("resident_worker.py", "motiondecode", "walk_forward_real", "run_patrol.py")
    own = os.getpid()
    found = []
    for line in output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3 and int(fields[0]) != own and "python" in fields[1].lower():
            if any(marker in fields[2].lower() for marker in markers):
                found.append(line.strip())
    return found


def dry_run(args) -> int:
    virtual = VirtualTime()
    loco = DryRunLocomotionAdapter()
    controller = PatrolController(loco, AlwaysClear(), config(args), clock=virtual.clock,
                                  sleep=virtual.sleep)
    if args.turn_only:
        controller.run_turn_only()
    else:
        controller.run(cycles=args.loops)
    print(f"[dry-run] elapsed={virtual.now:.1f}s commands={len(loco.commands)}")
    print("NO G1 COMMAND SENT")
    return 0


def observe_lidar(args):
    if args.lidar_source == "relay":
        guard = UdpRelayGuard(args.relay_bind, args.relay_port)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            time.sleep(0.1)
        report = guard.snapshot()
        print(json.dumps(report, indent=2, sort_keys=True))
        last = report["last"] or {}
        normalized = {
            "front_ready": bool(last.get("front_ready")) and report["front_state"] != "STALE",
            "rear_ready": bool(last.get("rear_ready")) and report["rear_state"] != "STALE",
            "rate_hz": last.get("lidar_rate_hz", 0.0),
            "last_scan_age_s": last.get("scan_age"),
        }
        print("FRONT GUARD:", "READY" if normalized["front_ready"] else "NOT READY")
        print("REAR GUARD: ", "READY" if normalized["rear_ready"] else "NOT READY")
        print("LIDAR RATE: %.2f Hz" % normalized["rate_hz"])
        print("RELAY RATE: %.2f Hz" % report["relay_rate_hz"])
        return guard, guard, normalized
    guard = LidarGuard()
    source = DDSLidarSource(args.network_interface, guard)
    try:
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            time.sleep(0.1)
        report = guard.snapshot()
        print(json.dumps(report, indent=2, sort_keys=True))
        print("FRONT GUARD:", "READY" if report["front_ready"] else "NOT READY")
        print("REAR GUARD: ", "READY" if report["rear_ready"] else "NOT READY")
        print("LIDAR RATE: %.2f Hz" % report["rate_hz"])
        age = report["last_scan_age_s"]
        print("LAST SCAN AGE:", "N/A" if age is None else "%.3f s" % age)
        return guard, source, report
    except Exception:
        source.close()
        raise


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    config(args)
    if args.mode == "dry-run":
        return dry_run(args)
    guard, source, report = observe_lidar(args)
    if args.mode == "lidar-check":
        source.close()
        print("NO G1 COMMAND SENT")
        return 0 if report["front_ready"] else 2
    turn_authorized = args.turn_only and args.operator_approved_turn_only and not args.one_cycle
    cycle_authorized = args.one_cycle and args.operator_approved_one_cycle and not args.turn_only
    if not (args.arm and (turn_authorized or cycle_authorized)):
        source.close()
        raise RuntimeError("real mode requires one explicitly approved turn-only or one-cycle gate")
    if not report["front_ready"]:
        source.close()
        raise RuntimeError("front stationary LiDAR guard must be READY")
    conflicts = writer_conflicts()
    if conflicts:
        source.close()
        raise RuntimeError("writer ownership conflict: " + "; ".join(conflicts))
    loco = UdpLocomotionAdapter(args.locomotion_relay_host, args.locomotion_relay_port)
    try:
        controller = PatrolController(loco, guard, config(args))
        if args.turn_only:
            controller.run_turn_only()
        else:
            controller.run(cycles=args.loops)
    finally:
        loco.close()
        source.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[patrol] operator abort -> STOP", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
