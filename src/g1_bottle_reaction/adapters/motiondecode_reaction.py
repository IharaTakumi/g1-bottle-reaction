"""IPC boundary to the resident MotionDecode reaction worker."""
from __future__ import annotations

import json
import math
from pathlib import Path
import shlex
import socket
import subprocess
import threading
import time
from typing import Any, Callable

from .robot import RobotAdapter


MOTION_PREFIX = "motiondecode:"
VALIDATED_REACTIONS = frozenset({"frustration", "surprise", "found", "joy"})
# Keep real execution aligned with motiondecode-test/config/named_reactions.json.
# ``joy`` remains available for dry-run compatibility but is explicitly marked
# real_g1_validated=false by the owning runtime.
REAL_G1_VALIDATED_REACTIONS = frozenset({"frustration", "surprise", "found"})


class MotionDecodeSafeReturnMiss(RuntimeError):
    """A failed q0 return whose independent resident postflight is fully safe."""

    def __init__(self, reaction: str, result: dict[str, Any],
                 resident_status: dict[str, Any]) -> None:
        self.reaction = reaction
        self.result = dict(result)
        self.controlled_q0_return_error_rad = result.get(
            "controlled_q0_return_error_rad"
        )
        self.returned_to_q0 = result.get("returned_to_q0")
        self.resident_status = dict(resident_status)
        super().__init__(
            f"reaction={reaction} returned_to_q0={self.returned_to_q0} "
            f"controlled_q0_return_error_rad="
            f"{self.controlled_q0_return_error_rad}"
        )


def _resident_postflight_is_safe(status: dict[str, Any]) -> bool:
    """Require the complete post-reaction resident safety contract."""
    age = status.get("lowstate_age_s")
    weight = status.get("weight")
    return bool(
        status.get("accepted") is True
        and status.get("state") == "READY"
        and isinstance(age, (int, float))
        and not isinstance(age, bool)
        and math.isfinite(float(age))
        and 0.0 <= float(age) < 0.15
        and status.get("ownership_safe") is True
        and status.get("external_writers") == 0
        and isinstance(weight, (int, float))
        and not isinstance(weight, bool)
        and float(weight) == 0.0
        and status.get("fault") is None
        and "status_error" not in status
    )


class LocalResidentChannel:
    def __init__(self, socket_path: str, timeout: float) -> None:
        self.socket_path = socket_path; self.timeout = timeout

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout); client.connect(self.socket_path)
            stream = client.makefile("rwb")
            stream.write((json.dumps(payload, sort_keys=True) + "\n").encode()); stream.flush()
            raw = stream.readline()
        if not raw:
            raise RuntimeError("MotionDecode worker closed without a response")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise RuntimeError("MotionDecode worker response must be an object")
        return result

    def close(self) -> None:
        pass


class SshResidentChannel:
    """Persistent SSH stdio bridge; no process is started on reaction trigger."""
    def __init__(self, *, target: str, control: str | None, remote_root: str,
                 socket_path: str, timeout: float,
                 popen_factory: Callable[..., Any] = subprocess.Popen) -> None:
        remote = (f"cd {shlex.quote(remote_root)} && python3 scripts/resident_worker_client.py "
                  f"--stdio --socket {shlex.quote(socket_path)} --timeout {timeout:g}")
        command = ["ssh", "-T", "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
                   "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=2",
                   "-o", "ServerAliveCountMax=2"]
        if control:
            command.extend(["-S", control])
        command.extend(["--", target, remote])
        self.process = popen_factory(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self._lock = threading.Lock()

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.process.poll() is not None:
                detail = self.process.stderr.read()[-1000:]
                raise RuntimeError(f"MotionDecode SSH bridge exited: {detail}")
            self.process.stdin.write(json.dumps(payload, sort_keys=True) + "\n")
            self.process.stdin.flush(); raw = self.process.stdout.readline()
        if not raw:
            raise RuntimeError("MotionDecode SSH bridge closed without a response")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise RuntimeError("MotionDecode worker response must be an object")
        return result

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.stdin.close(); self.process.terminate()
            try: self.process.wait(timeout=2.)
            except subprocess.TimeoutExpired: self.process.kill()


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
        allow_hackathon_joy: bool = False,
        python: Path | None = None,
        environ: dict[str, str] | None = None,
        run_factory: Callable[..., Any] = subprocess.run,
        resident: bool = True,
        socket_path: str = "/tmp/motiondecode-reaction.sock",
        remote_root: str = "/tmp/motiondecode-current",
        channel_factory: Callable[[], Any] | None = None,
        fallback: RobotAdapter | None = None,
    ) -> None:
        if real and not enabled:
            raise RuntimeError("Real MotionDecode requires explicit real-robot enable")
        if attended_real and not real:
            raise ValueError("Attended gate is valid only for real MotionDecode")
        if allow_hackathon_joy and not real:
            raise ValueError("Hackathon JOY gate is valid only for real MotionDecode")
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
        self.allow_hackathon_joy = bool(allow_hackathon_joy)
        self.python = python or self.repository / ".venv" / "bin" / "python"
        self.environ = environ
        self._run_factory = run_factory
        self.resident = bool(resident)
        self.socket_path = socket_path
        self.fallback = fallback
        self._operation_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._last_motion: str | None = None
        self._last_succeeded = False
        self._last_result: dict[str, Any] | None = None
        self._channel = None
        if self.resident:
            self._channel = (channel_factory() if channel_factory else
                (LocalResidentChannel(socket_path, timeout_seconds)
                 if transport == "local" else
                 SshResidentChannel(target=ssh_target, control=ssh_control,
                                    remote_root=remote_root, socket_path=socket_path,
                                    timeout=timeout_seconds)))
            status = self._channel.request({"operation": "status"})
            if not status.get("accepted") or status.get("state") != "READY":
                self._channel.close(); self._channel = None
                raise RuntimeError(f"MotionDecode resident worker is not READY: {status}")

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
        hackathon_joy_allowed = (
            reaction == "joy" and self.allow_hackathon_joy
        )
        if (self.real and reaction not in REAL_G1_VALIDATED_REACTIONS
                and not hackathon_joy_allowed):
            raise RuntimeError(
                f"MotionDecode reaction is not validated for real G1: {reaction}; "
                "JOY requires the explicit hackathon allow gate"
            )
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("Another robot motion is already executing")
        self._last_motion = motion
        self._last_succeeded = False
        try:
            trigger = time.monotonic()
            if self.resident:
                result = self._channel.request({"operation": "execute", "reaction": reaction,
                                                "trigger_monotonic_s": trigger})
                if not result.get("accepted"):
                    raise RuntimeError(f"MotionDecode worker rejected request: {result}")
            else:
                completed = self._run_factory(
                    self._command(reaction), cwd=self.repository, env=self.environ,
                    text=True, capture_output=not self.attended_real,
                    timeout=self.timeout_seconds + 45.0)
                if completed.returncode != 0:
                    detail = ((completed.stderr or completed.stdout)[-2000:].strip()
                              if not self.attended_real else "attended MotionDecode CLI failed")
                    raise RuntimeError(
                        f"MotionDecode CLI exited with code {completed.returncode}: {detail}")
                result = ({"reaction": reaction, "status": "pass", "executed": True,
                           "released": True, "returned_to_q0": True}
                          if self.attended_real else parse_named_result(completed.stdout))
            if result.get("reaction") != reaction or result.get("status") != "pass":
                raise RuntimeError(f"MotionDecode CLI returned failure: {result}")
            if self.real and self.resident:
                # Preserve the complete fail-closed evidence even when a
                # cleanup field below rejects the result.
                print(
                    "MOTIONDECODE RESULT: " + json.dumps(result, sort_keys=True),
                    flush=True,
                )
            self._last_result = result
            if self.real:
                proof = bool(result.get("executed") and result.get("released"))
                if self.resident:
                    cleanup_except_return = proof and bool(
                        result.get("motion_completed")
                        and result.get("weight_zero")
                        and result.get("hard_fault") is None
                    )
                    if (cleanup_except_return
                            and result.get("returned_to_q0") is False):
                        post_status = self._channel.request({"operation": "status"})
                        if _resident_postflight_is_safe(post_status):
                            raise MotionDecodeSafeReturnMiss(
                                reaction, result, post_status
                            )
                        raise RuntimeError(
                            "Real MotionDecode safe-return miss has unsafe or "
                            "ambiguous resident postflight: "
                            + json.dumps(post_status, sort_keys=True)
                        )
                    proof = cleanup_except_return and bool(
                        result.get("returned_to_q0")
                    )
                else:
                    proof = proof and bool(result.get("returned_to_q0"))
                if not proof:
                    raise RuntimeError("Real MotionDecode result lacks required cleanup proof")
            if not self.real and result.get("executed"):
                raise RuntimeError("MotionDecode dry-run unexpectedly executed")
            timing = {
                key: result.get(key)
                for key in (
                    "reaction_engine_trigger_monotonic_s",
                    "worker_receive_monotonic_s",
                    "acquire_start_monotonic_s",
                    "clip_start_monotonic_s",
                    "execution_completed_monotonic_s",
                    "worker_to_acquire_s",
                    "trigger_to_clip_s",
                )
                if key in result
            }
            if timing:
                print(
                    "MOTIONDECODE TIMING: "
                    + json.dumps(timing, sort_keys=True),
                    flush=True,
                )
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
        if self._channel is not None:
            self._channel.close(); self._channel = None
        if self.fallback is not None:
            self.fallback.close()
