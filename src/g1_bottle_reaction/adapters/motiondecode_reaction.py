"""Subprocess boundary to validated named reactions in motiondecode-test."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading
from typing import Any, Callable

from .robot import RobotAdapter


MOTION_PREFIX = "motiondecode:"
VALIDATED_REACTIONS = frozenset({"frustration"})


def parse_named_result(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
    if len(lines) != 1:
        raise RuntimeError(f"Expected one MotionDecode JSON result, found {len(lines)}")
    result = json.loads(lines[0])
    if not isinstance(result, dict):
        raise RuntimeError("MotionDecode result must be a JSON object")
    return result


class MotionDecodeReactionAdapter(RobotAdapter):
    """Resolve a Reaction Engine motion to the validated external CLI only."""

    def __init__(
        self,
        repository: Path,
        *,
        real: bool = False,
        enabled: bool = False,
        transport: str = "ssh",
        ssh_target: str = "unitree@10.42.0.76",
        ssh_control: str | None = None,
        timeout_seconds: float = 420.0,
        attended_real: bool = False,
        python: Path | None = None,
        environ: dict[str, str] | None = None,
        run_factory: Callable[..., Any] = subprocess.run,
        fallback: RobotAdapter | None = None,
    ) -> None:
        if real and not enabled:
            raise RuntimeError("Real MotionDecode requires explicit real-robot enable")
        if attended_real and not real:
            raise ValueError("Attended gate is valid only for real MotionDecode")
        if transport not in {"local", "ssh"}:
            raise ValueError("MotionDecode transport must be local or ssh")
        if timeout_seconds <= 0:
            raise ValueError("MotionDecode timeout must be positive")
        self.repository = Path(repository).resolve()
        self.real = real
        self.enabled = enabled
        self.transport = transport
        self.ssh_target = ssh_target
        self.ssh_control = ssh_control
        self.timeout_seconds = timeout_seconds
        self.attended_real = attended_real
        self.python = python or self.repository / ".venv" / "bin" / "python"
        self.environ = environ
        self._run_factory = run_factory
        self.fallback = fallback
        self._operation_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._last_motion: str | None = None
        self._last_succeeded = False
        self._last_result: dict[str, Any] | None = None

    @property
    def last_result(self) -> dict[str, Any] | None:
        return None if self._last_result is None else dict(self._last_result)

    @staticmethod
    def reaction_name(motion: str) -> str | None:
        return motion[len(MOTION_PREFIX) :] if motion.startswith(MOTION_PREFIX) else None

    def _command(self, reaction: str) -> list[str]:
        if reaction not in VALIDATED_REACTIONS:
            raise ValueError(f"MotionDecode reaction is not allowlisted: {reaction}")
        command = [
            str(self.python),
            str(self.repository / "scripts" / "run_named_reaction.py"),
            reaction,
            "--transport", self.transport,
            "--timeout", str(self.timeout_seconds),
        ]
        if self.real:
            command.extend(["--real", "--confirm-site-ready"])
            if not self.attended_real:
                command.extend(["--engine-authorized", "--json"])
        else:
            command.extend(["--dry-run", "--json"])
        if self.transport == "ssh":
            command.extend(["--ssh-target", self.ssh_target])
            if self.ssh_control:
                command.extend(["--ssh-control", self.ssh_control])
        return command

    def play_motion(self, motion: str) -> None:
        reaction = self.reaction_name(motion)
        if reaction is None:
            if self.fallback is None:
                raise ValueError(f"Unsupported motion for MotionDecode adapter: {motion}")
            with self._operation_lock:
                self.fallback.play_motion(motion)
            return
        if self._shutdown.is_set():
            raise RuntimeError("MotionDecode adapter is shutting down")
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("Another robot motion is already executing")
        self._last_motion = motion
        self._last_succeeded = False
        try:
            completed = self._run_factory(
                self._command(reaction),
                cwd=self.repository,
                env=self.environ,
                text=True,
                capture_output=not self.attended_real,
                timeout=self.timeout_seconds + 45.0,
            )
            if completed.returncode != 0:
                detail = (
                    (completed.stderr or completed.stdout)[-2000:].strip()
                    if not self.attended_real
                    else "attended MotionDecode CLI failed"
                )
                raise RuntimeError(
                    f"MotionDecode CLI exited with code {completed.returncode}: {detail}"
                )
            result = (
                {
                    "reaction": reaction,
                    "status": "pass",
                    "executed": True,
                    "released": True,
                    "returned_to_q0": True,
                }
                if self.attended_real
                else parse_named_result(completed.stdout)
            )
            if result.get("reaction") != reaction or result.get("status") != "pass":
                raise RuntimeError(f"MotionDecode CLI returned failure: {result}")
            if self.real and not (
                result.get("executed")
                and result.get("released")
                and result.get("returned_to_q0")
            ):
                raise RuntimeError("Real MotionDecode result lacks execution/release/q0 proof")
            if not self.real and result.get("executed"):
                raise RuntimeError("MotionDecode dry-run unexpectedly executed")
            self._last_result = result
            self._last_succeeded = True
        finally:
            self._operation_lock.release()

    def wait_for_motion_complete(self, motion: str, timeout: float | None = None) -> bool:
        del timeout
        reaction = self.reaction_name(motion)
        if reaction is None and self.fallback is not None:
            return self.fallback.wait_for_motion_complete(motion)
        return motion == self._last_motion and self._last_succeeded

    def request_shutdown(self) -> None:
        self._shutdown.set()
        if self.fallback is not None:
            self.fallback.request_shutdown()

    def close(self) -> None:
        self.request_shutdown()
        if self.fallback is not None:
            self.fallback.close()
