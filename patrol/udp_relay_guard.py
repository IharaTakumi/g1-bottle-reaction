"""Patrol guard adapter for compact G1-side UDP guard messages."""

from __future__ import annotations

import json
import socket
import threading
import time

from lidar_guard import GuardState


class UdpRelayGuard:
    def __init__(self, bind: str, port: int, stale_timeout_s: float = 0.5,
                 clock=time.monotonic):
        self._clock = clock
        self._stale_timeout = stale_timeout_s
        self._lock = threading.Lock()
        self._last = None
        self._received_at = None
        self._times = []
        self._closed = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind((bind, port))
        self._socket.settimeout(0.1)
        self._thread = threading.Thread(target=self._receive, name="patrol-guard-relay", daemon=True)
        self._thread.start()

    def _receive(self):
        while not self._closed.is_set():
            try:
                payload, _ = self._socket.recvfrom(65535)
                message = json.loads(payload)
                if not all(key in message for key in ("front_state", "rear_state", "scan_age", "sensor_health")):
                    continue
                received = self._clock()
                with self._lock:
                    self._last = message
                    self._received_at = received
                    self._times = [t for t in self._times if received - t <= 2.0] + [received]
            except socket.timeout:
                pass
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                pass

    def state(self, direction: str) -> GuardState:
        if direction not in ("front", "rear"):
            raise ValueError("direction must be front or rear")
        with self._lock:
            message, received = self._last, self._received_at
        if message is None or received is None:
            return GuardState.STALE
        relay_age = self._clock() - received
        scan_age = message.get("scan_age")
        if (relay_age > self._stale_timeout or scan_age is None
                or float(scan_age) > self._stale_timeout
                or message.get("sensor_health") != "READY"):
            return GuardState.STALE
        value = message.get(f"{direction}_state")
        return GuardState.CLEAR if value == "CLEAR" else GuardState.BLOCKED

    def snapshot(self):
        now = self._clock()
        with self._lock:
            message = dict(self._last) if self._last else None
            received = self._received_at
            times = tuple(self._times)
        age = None if received is None else max(0.0, now - received)
        span = times[-1] - times[0] if len(times) > 1 else 0.0
        return {
            "relay_age_s": age,
            "relay_rate_hz": (len(times) - 1) / span if span > 0 else 0.0,
            "front_state": self.state("front").value,
            "rear_state": self.state("rear").value,
            "last": message,
        }

    def close(self):
        self._closed.set()
        self._thread.join(timeout=1.0)
        self._socket.close()
