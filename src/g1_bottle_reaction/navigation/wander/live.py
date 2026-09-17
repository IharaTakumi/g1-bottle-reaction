from __future__ import annotations

import json
import math
from pathlib import Path
from queue import Empty, Queue
import sys
import threading
import time
from typing import IO, Any, Callable

from .core import MaplessWanderCore
from .models import LocalObstacleSnapshot, OdomSample, SECTOR_NAMES, WanderAction, WanderConfig


_EOF = object()


def _stop_line(reason: str) -> str:
    return f"LIVE SAFETY=INVALID ACTION={WanderAction.STOP.value} REASON={reason}"


def _finite_nonnegative(value: Any, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return result


class LiveShadowSession:
    def __init__(self, config: WanderConfig, *, seed: int | None = None) -> None:
        self.config = config
        self.core = MaplessWanderCore(config, seed=seed)

    def process(self, value: Any, *, now: float, transport_age_s: float = 0.0) -> str:
        if not isinstance(value, dict):
            raise ValueError("JSON record must be an object")
        if value.get("event") is not None:
            raise ValueError(f"source event: {value['event']}")
        if value.get("valid") is False:
            raise ValueError(str(value.get("invalid_reason") or "source marked sample invalid"))
        obstacle = value.get("obstacle_snapshot")
        if not isinstance(obstacle, dict) or set(obstacle) != set(SECTOR_NAMES):
            raise ValueError("required obstacle sectors missing")
        clearances = {
            name: _finite_nonnegative(obstacle[name], f"{name} clearance")
            for name in SECTOR_NAMES
        }
        odom = value.get("odom")
        if not isinstance(odom, dict):
            raise ValueError("required odometry missing")
        x = float(odom["x"])
        y = float(odom["y"])
        yaw = float(odom["yaw"])
        if not all(math.isfinite(item) for item in (x, y, yaw)):
            raise ValueError("odometry contains a non-finite value")
        cloud_age = _finite_nonnegative(value.get("cloud_age_s", 0.0), "cloud_age_s") + transport_age_s
        odom_age = _finite_nonnegative(value.get("odom_age_s", 0.0), "odom_age_s") + transport_age_s
        snapshot = LocalObstacleSnapshot.from_clearances(
            now - cloud_age,
            clearances,
            blocked_distance_m=self.config.policy.blocked_distance_m,
        )
        pose = OdomSample(x, y, yaw, now - odom_age)
        decision = self.core.decide(snapshot, pose, now=now)
        short_names = {"left": "L", "front_left": "FL", "front": "F", "front_right": "FR", "right": "R"}
        fields = " ".join(f"{short_names[name]}={clearances[name]:.2f}" for name in SECTOR_NAMES)
        return (
            f"LIVE cloud_age={cloud_age:.3f} odom_age={odom_age:.3f} {fields} "
            f"SAFETY={decision.safety.value} ACTION={decision.action.value} "
            f"REASON={decision.reason}"
        )


def _read_lines(stream: IO[str], queue: Queue[object], clock: Callable[[], float]) -> None:
    try:
        for line in stream:
            queue.put((line, clock()))
    finally:
        queue.put(_EOF)


def run_live_shadow(
    config: WanderConfig,
    *,
    stream: IO[str] = sys.stdin,
    record_path: Path | None = None,
    seed: int | None = None,
    debug: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
    output: Callable[[str], None] = print,
) -> int:
    """Consume a JSONL pipe; timeout, malformed input and EOF all display STOP."""
    queue: Queue[object] = Queue(maxsize=8)
    reader = threading.Thread(
        target=_read_lines,
        args=(stream, queue, monotonic),
        name="wander-live-jsonl-reader",
        daemon=True,
    )
    reader.start()
    session = LiveShadowSession(config, seed=seed)
    recorder = None
    if record_path is not None:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        recorder = record_path.open("a", encoding="utf-8")
    timeout_reported = False
    try:
        while True:
            try:
                item = queue.get(timeout=config.safety.stale_after_s)
            except Empty:
                if not timeout_reported:
                    output(_stop_line("stream stale or missing"))
                    timeout_reported = True
                continue
            if item is _EOF:
                output(_stop_line("stream disconnected (EOF)"))
                return 2
            timeout_reported = False
            line, received_at = item
            try:
                value = json.loads(line)
                rendered = session.process(
                    value,
                    now=monotonic(),
                    transport_age_s=max(0.0, monotonic() - received_at),
                )
                if recorder is not None:
                    recorder.write(json.dumps(value, separators=(",", ":")) + "\n")
                    recorder.flush()
                output(rendered)
                if debug and isinstance(value, dict) and value.get("diagnostics") is not None:
                    output("AXIS_DEBUG " + json.dumps(value["diagnostics"], separators=(",", ":")))
            except Exception as exc:
                output(_stop_line(f"malformed/invalid input: {type(exc).__name__}: {exc}"))
    finally:
        if recorder is not None:
            recorder.close()
