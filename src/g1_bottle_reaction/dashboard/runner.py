"""Start, watch and stop the g1_game.py commands for the dashboard.

Every command runs as its own detached process that writes a log file, so
closing the browser or restarting the dashboard does not stop the game. A
restarted dashboard finds the running command from its state file and reads
its log again. Stopping never cuts the arms' power: the emergency stop only
stops walking.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

from .logparse import GameView

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "patrol"))
import g1_game  # noqa: E402
from control_ipc import request as patrol_request  # noqa: E402

PATROL_SOCKET = Path("/tmp/g1-patrol-control.sock")
RELAY_PORT = 47622
# Above any sequence a Patrol run reaches, so the relay drops later moves
# from a Patrol that did not stop; the relay watchdog then holds STOP.
ESTOP_SEQUENCE_BASE = 10 ** 15
LONG_JOBS = {"dry", "stand", "run"}
JOBS = LONG_JOBS | {"check", "worker", "motion", "stop"}
CONFIRMED_JOBS = {"stand", "run", "motion"}


def process_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stream:
            state = stream.read().rsplit(")", 1)[1].split()[0]
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            command = stream.read()
    except OSError:
        return False
    return state != "Z" and b"g1_game.py" in command


def job_arguments(kind: str, params: dict) -> list[str]:
    if kind not in JOBS:
        raise ValueError(f"unknown command {kind}")
    if kind in {"dry", "stand"}:
        duration = float(params.get("duration", 90))
        if not 5 <= duration <= 1800:
            raise ValueError("duration must be 5 to 1800 seconds")
        return ["--duration", f"{duration:g}"] + (["--yes"] if kind == "stand" else [])
    if kind == "run":
        if params.get("minutes") not in (None, ""):
            minutes = float(params["minutes"])
            if not 0 < minutes <= 30:
                raise ValueError("minutes must be within (0, 30]")
            return ["--minutes", f"{minutes:g}", "--yes"]
        laps = int(params.get("laps", 3))
        if not 1 <= laps <= 100:
            raise ValueError("laps must be 1 to 100")
        return ["--laps", str(laps), "--yes"]
    if kind == "motion":
        name = str(params.get("name", ""))
        if name not in g1_game.GAME_REACTIONS:
            raise ValueError(f"unknown motion {name}")
        voice = params.get("voice")
        if not voice:
            return [name, "--yes"]
        paired = {v for pool in g1_game.reaction_catalog()["pools"].values()
                  for entry in pool if entry["motion"] == name for v in entry["voices"]}
        if voice not in paired:
            raise ValueError(f"{voice} is not a voice paired with {name}")
        return [name, "--voice", str(voice), "--yes"]
    return []


class Dashboard:
    def __init__(self, root: Path = ROOT, python: str | None = None) -> None:
        self.root = root
        self.python = python or sys.executable
        self.runtime = root / ".runtime" / "dashboard"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.frame_path = self.runtime / "frame.jpg"
        self.state_path = self.runtime / "job.json"
        self.lock = threading.RLock()
        self.job: dict | None = None
        self.process: subprocess.Popen | None = None
        self.view = GameView()
        self.patrol: dict | None = None
        self.patrol_at: float | None = None
        self.worker: dict = {}
        self.worker_at: float | None = None
        self.notices: list[dict] = []
        self._log_offset = 0
        self.catalog = g1_game.reaction_catalog()
        self._stop = threading.Event()
        self._reattach()
        for target in (self._follow_log, self._poll_patrol, self._poll_worker):
            threading.Thread(target=target, daemon=True).start()

    # ----- jobs -------------------------------------------------------------
    def _reattach(self) -> None:
        try:
            job = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(job, dict) and process_alive(int(job.get("pid", 0))):
            self.job = job
            self.note(f"再接続: 実行中の {job['kind']}（pid {job['pid']}）")

    def running(self) -> bool:
        with self.lock:
            if self.job is None:
                return False
            if self.process is not None:
                if self.process.poll() is None:
                    return True
                self.job["returncode"] = self.process.returncode
                self.process = None
            elif process_alive(int(self.job["pid"])):
                return True
            self.job["ended"] = self.job.get("ended") or time.time()
            return False

    def start(self, kind: str, params: dict | None = None) -> dict:
        params = params or {}
        if kind in CONFIRMED_JOBS and params.get("confirm") is not True:
            raise ValueError("this command moves G1 and needs the operator's confirmation")
        arguments = job_arguments(kind, params)
        with self.lock:
            if self.running():
                raise RuntimeError(f"{self.job['kind']} is still running; stop it first")
            logs = self.root / f"logs-{datetime.now():%Y%m%d}"
            logs.mkdir(exist_ok=True)
            log = logs / f"dash-{kind}-{datetime.now():%H%M%S}.log"
            env = dict(os.environ, PYTHONUNBUFFERED="1", G1_GAME_FRAME_OUTPUT=str(self.frame_path))
            if kind in LONG_JOBS:
                self.frame_path.unlink(missing_ok=True)
            with open(log, "ab") as output:
                self.process = subprocess.Popen(
                    [self.python, "-u", str(self.root / "scripts" / "g1_game.py"), kind, *arguments],
                    cwd=self.root, stdin=subprocess.DEVNULL, stdout=output,
                    stderr=subprocess.STDOUT, env=env, start_new_session=True)
            self.job = {"kind": kind, "pid": self.process.pid, "log": str(log),
                        "started": time.time(), "params": {k: v for k, v in params.items() if k != "confirm"}}
            self.state_path.write_text(json.dumps(self.job))
            self.view = GameView()
            self._log_offset = 0
            self.patrol = None
        self.note(f"開始: {kind} {' '.join(a for a in arguments if a != '--yes')}".strip())
        return self.job

    # ----- stopping ---------------------------------------------------------
    def finish(self) -> dict:
        """Safe stop: walking ends after the current loop; other commands end now."""
        with self.lock:
            kind = self.job["kind"] if self.job and self.running() else None
        if kind == "run":
            try:
                patrol_request(PATROL_SOCKET, {"operation": "finish"}, timeout=1.0)
                self.note("安全停止: 今の周を回り切ってから止まります")
                return {"ok": True}
            except OSError as exc:
                self.note(f"安全停止を送れませんでした（{exc}）。必要なら緊急停止を押してください", warn=True)
                return {"ok": False, "error": str(exc)}
        if kind in {"dry", "stand"}:
            self._interrupt_game()
            self.note(f"{kind} を終了しました")
            return {"ok": True}
        return {"ok": False, "error": "nothing to stop"}

    def emergency_stop(self) -> dict:
        """Stop walking now through every path; never cut the arms' power."""
        steps = {}
        try:
            patrol_request(PATROL_SOCKET, {"operation": "stop"}, timeout=0.5)
            steps["patrol"] = "stop sent"
        except OSError as exc:
            steps["patrol"] = f"no patrol control ({exc.__class__.__name__})"
        steps["relay"] = self._relay_stop()
        steps["processes"] = self._interrupt_game()
        self.note("緊急停止: 歩行停止を送りました（" + " / ".join(f"{k}: {v}" for k, v in steps.items()) + "）",
                  warn=True)
        return {"ok": True, "steps": steps}

    def _relay_stop(self) -> str:
        sequence = ESTOP_SEQUENCE_BASE + int(time.time() * 1000)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                for offset in range(3):
                    message = {"command": "stop", "seq": sequence + offset}
                    udp.sendto(json.dumps(message).encode(), (g1_game.LAN_HOST, RELAY_PORT))
            return "stop x3"
        except OSError as exc:
            return f"failed ({exc})"

    def _interrupt_game(self) -> str:
        # SIGINT runs each program's own stop path (Patrol: StopMove; supervisor:
        # child cleanup; g1_game run: relay shutdown).
        found = []
        for pattern in ("run_patrol.py", "run_integrated_demo.py", "g1_dual_camera.py"):
            if subprocess.run(["pkill", "-INT", "-f", pattern]).returncode == 0:
                found.append(pattern.split(".")[0])
        return ", ".join(found) or "none running"

    def release(self) -> dict:
        """Give G1 back: stop any game, then stop the worker and relays."""
        if self.running() and self.job["kind"] in LONG_JOBS:
            self.emergency_stop()
            deadline = time.time() + 15
            while self.running() and time.time() < deadline:
                time.sleep(0.2)
        if self.running():
            raise RuntimeError(f"{self.job['kind']} is still running")
        return self.start("stop")

    # ----- status -----------------------------------------------------------
    def note(self, text: str, warn: bool = False) -> None:
        entry = {"time": time.time(), "text": text, "warn": warn, "operator": True}
        with self.lock:
            self.notices.insert(0, entry)
            del self.notices[50:]
        logs = self.root / f"logs-{datetime.now():%Y%m%d}"
        logs.mkdir(exist_ok=True)
        with open(logs / "dashboard-operator.log", "a") as stream:
            stream.write(f"{datetime.now():%H:%M:%S} {text}\n")

    def _follow_log(self) -> None:
        while not self._stop.is_set():
            with self.lock:
                job, view = self.job, self.view
            if job:
                try:
                    with open(job["log"], "rb") as stream:
                        stream.seek(self._log_offset)
                        data = stream.read()
                    complete = data.rfind(b"\n") + 1
                    for line in data[:complete].decode(errors="replace").splitlines():
                        view.feed(line)
                    with self.lock:
                        if self.view is view:
                            self._log_offset += complete
                except OSError:
                    pass
            time.sleep(0.2)

    def _poll_patrol(self) -> None:
        while not self._stop.is_set():
            if self.job and self.job["kind"] == "run" and self.running() and PATROL_SOCKET.exists():
                try:
                    status = patrol_request(PATROL_SOCKET, {"operation": "status"}, timeout=0.5)
                    with self.lock:
                        self.patrol, self.patrol_at = status, time.time()
                except (OSError, ValueError, RuntimeError):
                    pass
            time.sleep(0.25)

    def _poll_worker(self) -> None:
        while not self._stop.is_set():
            # G1 is shared: ask it nothing until the operator has started a command.
            # After a release, G1 belongs to someone else again.
            if self.job is None or self.job["kind"] == "stop":
                with self.lock:
                    self.worker = {}
                self._stop.wait(1.0)
                continue
            try:
                status = g1_game.worker_status()
            except Exception:
                status = {}
            with self.lock:
                self.worker, self.worker_at = status, time.time()
            self._stop.wait(5.0)

    def snapshot(self) -> dict:
        with self.lock:
            running = self.running()
            job = dict(self.job) if self.job else None
            frame_age = None
            try:
                frame_age = time.time() - self.frame_path.stat().st_mtime
            except OSError:
                pass
            return {
                "now": time.time(), "job": job, "running": running,
                "mode": (job["kind"] if job and running else "idle"),
                "patrol": self.patrol, "patrol_age_s": (time.time() - self.patrol_at) if self.patrol_at else None,
                "worker": {k: self.worker.get(k) for k in ("state", "fault", "preflight", "weight")},
                "worker_age_s": (time.time() - self.worker_at) if self.worker_at else None,
                "frame_age_s": frame_age, "game": self.view.snapshot(), "notices": self.notices[:20],
                "motions": sorted(g1_game.GAME_REACTIONS), "catalog": self.catalog, "lan_host": g1_game.LAN_HOST,
                "ssh_target": g1_game.SSH_TARGET,
            }

    def close(self) -> None:
        self._stop.set()
