"""Turn g1_game.py output lines into dashboard state.

The game processes only print; the dashboard reads their log file, so a
restarted dashboard can rebuild the same view by reading the log again.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import re
import time

CHECK_LINE = re.compile(r"^\[(OK|WARN|FAIL)\s*\] ([^:]+): (.*)$")
REACTION_EVENT = re.compile(r"^(PERSON|BANANA|PLUSHIE) REACTION EVENT: .*motion=motiondecode:(\w+)")
DETECTION = re.compile(r"^\[[\d:.]+\] (PERSON|BANANA|PLUSHIE) (DETECTED|LOST)(?: count=(\d+) conf=([\d.]+))?")
CAMERA = re.compile(r"^g1: (\d+x\d+), FPS=([\d.]+), local_age=([\d.]+)ms.*status=(\w+)")
YOLO = re.compile(r"YOLO FPS=([\d.]+), inference=([\d.]+)ms")
AUDIO = re.compile(r"^Audio triggered: (\S+?);")
LOOP = re.compile(r"^\[patrol\] loop complete count=(\d+)")
# Lines worth showing in the event log; everything is still in the log file.
NOTABLE = re.compile(
    r"^\[(patrol|guard)\]|REACTION EVENT|MOTIONDECODE RESULT|FAULT|FAILED|ERROR|REFUSED|"
    r"FINAL STOP|STOP|CHECK=|WORKER=|RELEASED|CANCELLED|Traceback|STILL RUNNING|DROP")
WARNING = re.compile(r"FAULT|FAILED|ERROR|REFUSED|FINAL STOP|Traceback|STALE|BLOCKED|recoverable_abort")
# Periodic progress lines would flood the event log.
QUIET = re.compile(r"odom_progress=|TELEMETRY RECOVERED|TELEMETRY TRANSPORT STALE")


@dataclass
class GameView:
    checks: dict[str, dict[str, str]] = field(default_factory=dict)
    check_result: str | None = None
    worker_line: str | None = None
    reactions: deque = field(default_factory=lambda: deque(maxlen=50))
    visible: dict[str, dict] = field(default_factory=dict)
    camera: dict[str, object] = field(default_factory=dict)
    loops_completed: int = 0
    events: deque = field(default_factory=lambda: deque(maxlen=300))
    _manual_voice: str | None = None

    def feed(self, line: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        line = line.rstrip("\n")
        if not line.strip():
            return
        if match := CHECK_LINE.match(line):
            self.checks[match[2]] = {"level": match[1], "detail": match[3]}
        elif line.startswith("CHECK="):
            self.check_result = line[6:]
        elif line.startswith("WORKER="):
            self.worker_line = line[7:]
        elif match := REACTION_EVENT.match(line):
            self.reactions.appendleft({"time": now, "target": match[1].lower(),
                                       "motion": match[2], "voice": None, "status": "running"})
        elif match := AUDIO.match(line):
            voice = match[1].rsplit("/", 1)[-1]
            if self.reactions and self.reactions[0]["status"] == "running" and self.reactions[0]["voice"] is None:
                self.reactions[0]["voice"] = voice
            else:
                self._manual_voice = voice
        elif line.startswith("MOTIONDECODE RESULT: "):
            self._result(line[len("MOTIONDECODE RESULT: "):])
        elif match := DETECTION.match(line):
            target = match[1].lower()
            if match[2] == "DETECTED":
                self.visible[target] = {"since": self.visible.get(target, {}).get("since", now),
                                        "count": int(match[3] or 1), "conf": float(match[4] or 0)}
            else:
                self.visible.pop(target, None)
        elif match := CAMERA.match(line):
            self.camera.update(resolution=match[1], fps=float(match[2]),
                               local_age_ms=float(match[3]), status=match[4], at=now)
        if match := YOLO.search(line):
            self.camera.update(yolo_fps=float(match[1]), inference_ms=float(match[2]))
        if match := LOOP.match(line):
            self.loops_completed = int(match[1])
        if NOTABLE.search(line) and not QUIET.search(line):
            self.events.appendleft({"time": now, "text": line[:300],
                                    "warn": bool(WARNING.search(line))})

    def _result(self, raw: str) -> None:
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(result, dict):
            return
        entry = next((r for r in self.reactions
                      if r["motion"] == result.get("reaction") and r["status"] == "running"), None)
        if entry is None:
            entry = {"time": time.time(), "target": "manual", "motion": result.get("reaction"),
                     "voice": self._manual_voice, "status": "running"}
            self._manual_voice = None
            self.reactions.appendleft(entry)
        entry["status"] = str(result.get("status") or ("pass" if result.get("accepted") else "fail"))
        entry["reason"] = result.get("recoverable_reason") or result.get("hard_fault") or result.get("reason")
        start, end = result.get("clip_start_monotonic_s"), result.get("execution_completed_monotonic_s")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            entry["duration_s"] = round(end - start, 1)
        entry["q0_error_rad"] = result.get("q0_return_error_rad")
        entry["leg_deviation_rad"] = result.get("max_leg_q0_deviation_rad")

    def snapshot(self) -> dict[str, object]:
        return {
            "checks": self.checks, "check_result": self.check_result,
            "worker_line": self.worker_line, "reactions": list(self.reactions)[:12],
            "visible": self.visible, "camera": self.camera,
            "loops_completed": self.loops_completed, "events": list(self.events)[:80],
        }
