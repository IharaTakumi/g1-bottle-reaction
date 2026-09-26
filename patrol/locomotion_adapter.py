"""Thin, lazy LocoClient adapter with explicit arm/reverse gates and watchdog."""

from __future__ import annotations

import threading
import time
import json
import socket
import math


MAX_SPEED_M_S = 0.30


class UdpLocomotionAdapter:
    """Send heartbeat velocity/stop commands to the G1-side relay."""

    def __init__(self, host: str = "10.42.0.76", port: int = 47622,
                 telemetry_bind: str = "10.42.0.1", telemetry_port: int = 47623):
        self._destination = (host, port)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._seq = 0
        self._closed = False
        self._imu_lock = threading.Lock()
        self._imu = None
        self._telemetry = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._telemetry.bind((telemetry_bind, telemetry_port))
        self._telemetry.settimeout(0.1)
        self._telemetry_thread = threading.Thread(target=self._receive_telemetry, daemon=True)
        self._telemetry_thread.start()

    def _receive_telemetry(self):
        while not self._closed:
            try:
                message = json.loads(self._telemetry.recvfrom(4096)[0])
                with self._imu_lock:
                    self._imu = (time.monotonic(), message)
            except socket.timeout:
                pass
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                pass

    def imu_sample(self):
        with self._imu_lock:
            value = self._imu
        if value is None:
            return None
        received, message = value
        result = dict(message)
        result["transport_age"] = max(0.0, time.monotonic() - received)
        return result

    def odom_sample(self):
        sample = self.imu_sample()
        if sample is None:
            return None
        return sample

    def _send(self, message):
        if self._closed:
            raise RuntimeError("UDP locomotion adapter is closed")
        message["seq"] = self._seq
        self._seq += 1
        self._socket.sendto(json.dumps(message, separators=(",", ":")).encode(), self._destination)

    def move(self, vx: float, vyaw: float = 0.0) -> None:
        vx = float(vx)
        if not -MAX_SPEED_M_S <= vx <= MAX_SPEED_M_S:
            raise ValueError(f"vx must be within +/-{MAX_SPEED_M_S:.2f} m/s")
        vyaw = float(vyaw)
        if not -0.50 <= vyaw <= 0.50:
            raise ValueError("vyaw must be within +/-0.50 rad/s")
        self._send({"vx": vx, "vy": 0.0, "vyaw": vyaw})

    def stop(self) -> None:
        # Duplicate STOP datagrams tolerate a single UDP loss. Sequence numbers
        # remain unique, so both are valid and idempotent at the relay.
        self._send({"command": "stop"})
        self._send({"command": "stop"})

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.stop()
        finally:
            self._closed = True
            self._telemetry_thread.join(timeout=0.5)
            self._telemetry.close()
            self._socket.close()


class DryRunLocomotionAdapter:
    def __init__(self):
        self.commands: list[tuple[str, float, float]] = []
        self._yaw = 0.0
        self._x = 0.0
        self._y = 0.0

    def move(self, vx: float, vyaw: float = 0.0) -> None:
        self.commands.append(("move", vx, vyaw))
        self._yaw += vyaw * 0.10
        self._x += vx * 0.10 * math.cos(self._yaw)
        self._y += vx * 0.10 * math.sin(self._yaw)

    def imu_sample(self):
        return {"yaw": self._yaw, "imu_age": 0.0, "imu_ready": True,
                "transport_age": 0.0, "imu_rate_hz": 1000.0}

    def odom_sample(self):
        return {"odom_x": self._x, "odom_y": self._y, "odom_yaw": self._yaw,
                "odom_age": 0.0, "odom_ready": True, "transport_age": 0.0,
                "odom_rate_hz": 10.0}

    def stop(self) -> None:
        self.commands.append(("stop", 0.0, 0.0))

    def close(self) -> None:
        self.stop()


class LocomotionAdapter:
    """Small boundary around the installed Unitree SDK's LocoClient.

    Imports and DDS initialization happen only after ``arm=True``.  A caller
    must refresh ``move`` faster than watchdog_timeout or StopMove is issued.
    """

    def __init__(self, interface: str, *, arm: bool, allow_reverse: bool,
                 watchdog_timeout: float = 0.35):
        if not arm:
            raise RuntimeError("real locomotion requires --arm")
        if watchdog_timeout <= 0:
            raise ValueError("watchdog timeout must be positive")
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
        ChannelFactoryInitialize(0, interface)
        self._client = LocoClient()
        self._client.SetTimeout(2.0)
        self._client.Init()
        self._allow_reverse = allow_reverse
        self._watchdog_timeout = watchdog_timeout
        self._last_command = time.monotonic()
        self._moving = False
        self._closed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._watchdog, name="patrol-command-watchdog", daemon=True)
        self._thread.start()

    def move(self, vx: float, vyaw: float = 0.0) -> None:
        vx = float(vx)
        if not -MAX_SPEED_M_S <= vx <= MAX_SPEED_M_S:
            raise ValueError(f"vx must be within +/-{MAX_SPEED_M_S:.2f} m/s")
        if vx < 0 and not self._allow_reverse:
            raise RuntimeError("reverse is not armed")
        with self._lock:
            if self._closed:
                raise RuntimeError("locomotion adapter is closed")
            self._last_command = time.monotonic()
            self._moving = True
        vyaw = float(vyaw)
        if not -0.50 <= vyaw <= 0.50:
            raise ValueError("vyaw must be within +/-0.50 rad/s")
        result = self._client.Move(vx, 0.0, vyaw, continous_move=True)
        if result not in (None, 0):
            self.stop()
            raise RuntimeError(f"Move returned {result!r}")

    def stop(self) -> None:
        with self._lock:
            self._moving = False
            self._last_command = time.monotonic()
        result = self._client.StopMove()
        if result not in (None, 0):
            raise RuntimeError(f"StopMove returned {result!r}")

    def _watchdog(self) -> None:
        while True:
            time.sleep(min(0.05, self._watchdog_timeout / 2))
            with self._lock:
                if self._closed:
                    return
                expired = self._moving and time.monotonic() - self._last_command > self._watchdog_timeout
                if expired:
                    self._moving = False
            if expired:
                try:
                    self._client.StopMove()
                except Exception:
                    pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._moving = False
        try:
            self._client.StopMove()
        finally:
            self._thread.join(timeout=0.5)
