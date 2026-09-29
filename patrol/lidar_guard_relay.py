#!/usr/bin/env python3
"""Read-only G1 LiDAR guard relay; sends compact JSON over UDP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
import time

from hackathon_profile import LIDAR_STOP_DISTANCE_M as HACKATHON_LIDAR_STOP_DISTANCE_M


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--interface", default="eth0")
    value.add_argument("--destination", required=True)
    value.add_argument("--port", type=int, default=47621)
    value.add_argument("--seconds", type=float, default=10.0)
    value.add_argument("--guard-path", type=Path, required=True)
    value.add_argument("--lidar-stop-distance", type=float, choices=(0.50, 0.80),
                       default=0.80)
    value.add_argument("--hackathon-runtime", action="store_true")
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    if args.hackathon_runtime:
        args.lidar_stop_distance = HACKATHON_LIDAR_STOP_DISTANCE_M
    sys.path.insert(0, str(args.guard_path))
    from lidar_guard import GuardConfig, LidarGuard
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.sensor_msgs.msg.dds_ import PointCloud2_

    guard = LidarGuard(GuardConfig(stop_distance_m=args.lidar_stop_distance))
    ChannelFactoryInitialize(0, args.interface)
    subscriber = ChannelSubscriber(
        "rt/utlidar/cloud_livox_mid360", PointCloud2_)
    subscriber.Init(guard.update_cloud, 1)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    deadline = time.monotonic() + args.seconds
    sequence = 0
    try:
        while time.monotonic() < deadline:
            snapshot = guard.snapshot()
            message = {
                "sequence": sequence,
                "timestamp": time.time(),
                "front_clear": snapshot["front_state"] == "CLEAR",
                "rear_clear": snapshot["rear_state"] == "CLEAR",
                "front_state": snapshot["front_state"],
                "rear_state": snapshot["rear_state"],
                "turn_state": snapshot["turn_state"],
                "scan_age": snapshot["last_scan_age_s"],
                "lidar_rate_hz": snapshot["rate_hz"],
                "sensor_health": snapshot["sensor_health"],
                "stop_distance_m": args.lidar_stop_distance,
                "total_scan_points": snapshot["total_scan_points"],
                "front_ready": snapshot["front_ready"],
                "rear_ready": snapshot["rear_ready"],
                "stop_points": snapshot["stop_points"],
                "coverage_points": snapshot["coverage_points"],
                "nearest_m": snapshot["nearest_m"],
                "error": snapshot["error"],
            }
            udp.sendto(json.dumps(message, separators=(",", ":")).encode(),
                       (args.destination, args.port))
            sequence += 1
            time.sleep(0.1)
    finally:
        subscriber.Close()
        udp.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
