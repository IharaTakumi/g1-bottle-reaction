#!/usr/bin/env python3
"""Fail-closed UDP-to-Unitree locomotion relay for the G1-side PC."""

from __future__ import annotations

import argparse
import json
import math
import socket
import threading
import time


MAX_VX = 0.30
MAX_VY = 0.20
MAX_VYAW = 0.50


class DryRunClient:
    def Move(self, vx, vy, vyaw, continous_move=False):
        print(f"[loco-relay] DRY RUN MOVE vx={vx:+.3f} vy={vy:+.3f} vyaw={vyaw:+.3f}", flush=True)

    def StopMove(self):
        print("[loco-relay] DRY RUN STOP", flush=True)


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--bind", default="10.42.0.76")
    value.add_argument("--port", type=int, default=47622)
    value.add_argument("--interface", default="eth0")
    value.add_argument("--watchdog-timeout", type=float, default=0.40)
    value.add_argument("--telemetry-host", default="10.42.0.1")
    value.add_argument("--telemetry-port", type=int, default=47623)
    value.add_argument("--seconds", type=float, default=0.0, help="0 runs until stopped")
    value.add_argument("--arm", action="store_true")
    value.add_argument("--telemetry-only", action="store_true")
    value.add_argument("--allow-reverse", action="store_true")
    value.add_argument("--start-mapping-if-needed", action="store_true")
    return value


def real_runtime(interface, create_loco):
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
    from unitree_sdk2py.idl.nav_msgs.msg.dds_ import Odometry_
    ChannelFactoryInitialize(0, interface)
    client = None
    if create_loco:
        client = LocoClient()
        client.SetTimeout(2.0)
        client.Init()
    imu = {"yaw": None, "received": None, "count": 0, "started": time.monotonic()}
    odom = {"x": None, "y": None, "yaw": None, "received": None,
            "count": 0, "started": time.monotonic()}
    lock = threading.Lock()
    def lowstate(sample):
        yaw = float(sample.imu_state.rpy[2])
        with lock:
            imu.update(yaw=yaw, received=time.monotonic(), count=imu["count"] + 1)
    subscriber = ChannelSubscriber("rt/lowstate", LowState_)
    subscriber.Init(lowstate, 10)
    def mapping_odom(sample):
        pose = sample.pose.pose
        q = pose.orientation
        yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                         1.0 - 2.0 * (q.y * q.y + q.z * q.z))
        with lock:
            odom.update(x=float(pose.position.x), y=float(pose.position.y), yaw=float(yaw),
                        received=time.monotonic(), count=odom["count"] + 1)
    odom_subscriber = ChannelSubscriber("rt/unitree/slam_mapping/odom", Odometry_)
    odom_subscriber.Init(mapping_odom, 10)
    return client, (subscriber, odom_subscriber), imu, odom, lock


def slam_call(api_id, parameter):
    from unitree_sdk2py.rpc.client import Client
    client = Client("slam_operate", False)
    client.SetTimeout(10.0)
    client._SetApiVerson("1.0.0.1")
    client._RegistApi(api_id, 0)
    code, raw = client._Call(api_id, json.dumps(parameter, allow_nan=False))
    print(f"[loco-relay] SLAM api={api_id} code={code} response={raw}", flush=True)
    if code != 0:
        raise RuntimeError(f"SLAM API {api_id} failed: {code}")


def decode(payload, allow_reverse):
    message = json.loads(payload)
    if not isinstance(message, dict) or not isinstance(message.get("seq"), int):
        raise ValueError("packet requires integer seq")
    if message.get("command") == "stop":
        return message["seq"], "stop", (0.0, 0.0, 0.0)
    values = tuple(float(message[name]) for name in ("vx", "vy", "vyaw"))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("velocity must be finite")
    vx, vy, vyaw = values
    if abs(vx) > MAX_VX or abs(vy) > MAX_VY or abs(vyaw) > MAX_VYAW:
        raise ValueError("velocity exceeds relay clamp")
    if vx < 0 and not allow_reverse:
        raise ValueError("reverse is not enabled")
    return message["seq"], "move", values


def main(argv=None):
    args = parser().parse_args(argv)
    if args.arm and args.telemetry_only:
        raise ValueError("--arm and --telemetry-only are mutually exclusive")
    if not 0.35 <= args.watchdog_timeout <= 0.50:
        raise ValueError("watchdog timeout must be within 0.35..0.50 seconds")
    if args.arm and not args.allow_reverse:
        print("[loco-relay] reverse gate remains closed", flush=True)
    subscriber = None
    imu = None
    imu_lock = None
    odom = None
    started_mapping = False
    if args.arm or args.telemetry_only:
        client, subscriber, imu, odom, imu_lock = real_runtime(args.interface, args.arm)
        if client is None:
            client = DryRunClient()
        deadline = time.monotonic() + 2.0
        while odom["received"] is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if odom["received"] is None and args.start_mapping_if_needed:
            slam_call(1801, {"data": {"slam_type": "indoor"}})
            started_mapping = True
            deadline = time.monotonic() + 15.0
            while odom["received"] is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if odom["received"] is None:
                raise RuntimeError("mapping odometry did not start after API 1801")
    else:
        client = DryRunClient()
    mode = ("ARMED" if args.arm else "TELEMETRY ONLY"
            if args.telemetry_only else "DRY RUN (SDK not initialized)")
    print(f"[loco-relay] READY mode={mode} bind={args.bind}:{args.port} watchdog={args.watchdog_timeout:.2f}s", flush=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    telemetry = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind, args.port))
    sock.settimeout(0.05)
    started = time.monotonic()
    last_valid = time.monotonic()
    last_seq = -1
    moving = False
    next_telemetry = time.monotonic()

    def stop(reason):
        nonlocal moving
        client.StopMove()
        moving = False
        print(f"[loco-relay] STOP reason={reason}", flush=True)

    try:
        while args.seconds == 0 or time.monotonic() - started < args.seconds:
            now = time.monotonic()
            if imu is not None and now >= next_telemetry:
                with imu_lock:
                    sample = dict(imu)
                    odom_sample = dict(odom)
                received = sample["received"]
                age = None if received is None else max(0.0, now - received)
                yaw = sample["yaw"]
                ready = bool(received is not None and age <= 0.10 and yaw is not None and math.isfinite(yaw))
                elapsed = max(1e-9, now - sample["started"])
                odom_received = odom_sample["received"]
                odom_age = None if odom_received is None else max(0.0, now - odom_received)
                odom_values = (odom_sample["x"], odom_sample["y"], odom_sample["yaw"])
                odom_ready = bool(odom_received is not None and odom_age <= 0.50 and
                                  all(value is not None and math.isfinite(value)
                                      for value in odom_values))
                odom_elapsed = max(1e-9, now - odom_sample["started"])
                telemetry.sendto(json.dumps({
                    "yaw": yaw, "imu_age": age, "imu_ready": ready,
                    "imu_rate_hz": sample["count"] / elapsed,
                    "odom_ready": odom_ready, "odom_x": odom_sample["x"],
                    "odom_y": odom_sample["y"], "odom_yaw": odom_sample["yaw"],
                    "odom_age": odom_age,
                    "odom_rate_hz": odom_sample["count"] / odom_elapsed,
                    "timestamp": time.time(),
                }, separators=(",", ":")).encode(),
                    (args.telemetry_host, args.telemetry_port))
                next_telemetry = now + 0.02
            try:
                payload, peer = sock.recvfrom(4096)
                seq, command, velocity = decode(payload, args.allow_reverse)
                if seq <= last_seq:
                    raise ValueError("stale or duplicate sequence")
                last_seq = seq
                last_valid = time.monotonic()
                if command == "stop":
                    stop(f"command seq={seq}")
                else:
                    vx, vy, vyaw = velocity
                    client.Move(vx, vy, vyaw, continous_move=True)
                    moving = True
                    print(f"[loco-relay] MOVE seq={seq} vx={vx:+.3f} peer={peer[0]}", flush=True)
            except socket.timeout:
                if moving and time.monotonic() - last_valid > args.watchdog_timeout:
                    stop("watchdog timeout")
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                stop(f"invalid packet: {exc}")
    except KeyboardInterrupt:
        stop("Ctrl+C")
    except BaseException:
        stop("exception")
        raise
    finally:
        stop("process exit")
        if subscriber is not None:
            for item in subscriber:
                item.Close()
        if started_mapping:
            slam_call(1901, {"data": {}})
        sock.close()
        telemetry.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
