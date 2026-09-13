"""One-shot G1-local safe Arm Action transport over authenticated SSH."""
from __future__ import annotations

import inspect
import json
import logging
import os
import shlex
import subprocess
import threading
from typing import Any, Callable

from .g1_robot import g1_local_safe_action_main
from .robot import RobotAdapter


LOGGER = logging.getLogger(__name__)
RESULT_PREFIX = "G1_SAFE_ACTION_RESULT="
ALLOWED_OPERATIONS = frozenset({"probe", "notice"})


def parse_safe_action_result(output: str) -> dict[str, Any]:
    """Extract exactly one prefixed JSON result despite unrelated SDK logs."""

    matches = [
        line[len(RESULT_PREFIX) :]
        for line in output.splitlines()
        if line.startswith(RESULT_PREFIX)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one {RESULT_PREFIX} line, found {len(matches)}"
        )
    try:
        result = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid G1 safe-action JSON result") from exc
    if not isinstance(result, dict):
        raise RuntimeError("G1 safe-action result must be a JSON object")
    return result


class SshG1SafeActionAdapter(RobotAdapter):
    """Run only the fixed ``notice`` operation inside a G1-local process.

    The PC never sends Action IDs.  Each remote helper owns Action 23 through
    its one best-effort Action 99 cleanup, including uncertain RPC outcomes.
    """

    def __init__(
        self,
        ssh_target: str,
        ssh_control: str | None = None,
        *,
        enabled: bool = False,
        motion_mode: str = "disabled",
        execute_real_action: bool = False,
        helper_timeout_seconds: float = 40.0,
        termination_grace_seconds: float = 12.0,
        environ: dict[str, str] | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        if motion_mode not in {"disabled", "safe-actions"}:
            raise ValueError("G1 motion mode must be disabled or safe-actions")
        if motion_mode == "safe-actions" and not enabled:
            raise RuntimeError(
                "SSH G1 motion requires --enable-real-robot and safe-actions"
            )
        if helper_timeout_seconds <= 0 or termination_grace_seconds <= 0:
            raise ValueError("SSH helper timeouts must be positive")
        self.ssh_target = ssh_target
        self.ssh_control = ssh_control
        self.enabled = enabled
        self.motion_mode = motion_mode
        self.execute_real_action = execute_real_action
        self.helper_timeout_seconds = helper_timeout_seconds
        self.termination_grace_seconds = termination_grace_seconds
        self._environ = os.environ if environ is None else environ
        self._popen_factory = popen_factory
        self._shutdown_requested = threading.Event()
        self._start_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_process: Any | None = None
        self._motion_disabled_reason = ""
        self._last_motion_succeeded = False
        self._last_result: dict[str, Any] | None = None

    @property
    def motion_enabled(self) -> bool:
        return self.enabled and self.motion_mode == "safe-actions"

    @property
    def motion_disabled_reason(self) -> str:
        with self._state_lock:
            return self._motion_disabled_reason

    @property
    def last_result(self) -> dict[str, Any] | None:
        with self._state_lock:
            return None if self._last_result is None else dict(self._last_result)

    def initialize(self) -> None:
        if not self.motion_enabled:
            LOGGER.info("SSH G1 safe action is disabled; motion is a safe no-op")
            return
        if not self.execute_real_action or self._environ.get(
            "G1_ALLOW_REAL_ACTION"
        ) != "1":
            raise RuntimeError(
                "SSH G1 notice requires both --execute-real-action and "
                "G1_ALLOW_REAL_ACTION=1"
            )
        print("*** REAL G1 MOTION VIA G1-LOCAL SSH HELPER ENABLED ***", flush=True)
        print("Motion: fixed notice (Action 23 -> 2s -> Action 99)", flush=True)

    def _remote_source(self) -> str:
        return (
            inspect.getsource(g1_local_safe_action_main)
            + "\n_result = g1_local_safe_action_main()\n"
            + "raise SystemExit(0 if _result['ok'] else 2)\n"
        )

    def _build_command(self, operation: str) -> list[str]:
        if operation not in ALLOWED_OPERATIONS:
            raise ValueError(f"Unsupported G1 safe-action operation: {operation}")
        remote = []
        if operation == "notice":
            if not self.execute_real_action or self._environ.get(
                "G1_ALLOW_REAL_ACTION"
            ) != "1":
                raise RuntimeError(
                    "notice requires both --execute-real-action and "
                    "G1_ALLOW_REAL_ACTION=1"
                )
            remote.extend(["env", "G1_ALLOW_REAL_ACTION=1"])
        remote.extend(
            ["python3", "-u", "-B", "-c", self._remote_source(), operation]
        )
        if operation == "notice":
            remote.append("--execute-real-action")

        command = [
            "ssh",
            "-T",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=2",
            "-o",
            "ServerAliveCountMax=2",
        ]
        if self.ssh_control:
            command.extend(["-S", self.ssh_control])
        command.extend(["--", self.ssh_target, shlex.join(remote)])
        return command

    def _latch_failure(self, reason: str) -> None:
        with self._state_lock:
            if not self._motion_disabled_reason:
                self._motion_disabled_reason = reason

    def _run_helper(self, operation: str) -> dict[str, Any]:
        command = self._build_command(operation)
        with self._start_lock:
            if operation == "notice" and self._shutdown_requested.is_set():
                raise RuntimeError("shutdown requested before SSH notice start")
            process = self._popen_factory(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            with self._state_lock:
                self._active_process = process
        try:
            try:
                stdout, stderr = process.communicate(
                    timeout=self.helper_timeout_seconds
                )
            except subprocess.TimeoutExpired as exc:
                # The timeout already includes both 10-second RPC boundaries,
                # the 2-second hold, and margin.  Only then interrupt SSH and
                # allow a further full RPC timeout for G1-local cleanup.
                process.terminate()
                try:
                    stdout, stderr = process.communicate(
                        timeout=self.termination_grace_seconds
                    )
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                raise RuntimeError(
                    f"G1 SSH safe-action helper timed out after "
                    f"{self.helper_timeout_seconds:.1f}s"
                ) from exc
        finally:
            with self._state_lock:
                if self._active_process is process:
                    self._active_process = None

        try:
            result = parse_safe_action_result(stdout)
        except Exception as exc:
            detail = stderr.strip()[-1000:]
            if detail:
                raise RuntimeError(f"{exc}; remote stderr: {detail}") from exc
            raise
        if process.returncode != 0:
            raise RuntimeError(
                "G1 SSH safe-action helper exited with code "
                f"{process.returncode}: {result.get('error', '')}"
            )
        if not result.get("ok"):
            raise RuntimeError(
                f"G1 SSH safe-action helper failed: {result.get('error', '')}"
            )
        if result.get("operation") != operation:
            raise RuntimeError("G1 safe-action result operation does not match request")
        if not (
            result.get("dds_initialized")
            and result.get("action_list_ok")
            and result.get("action23_available")
            and result.get("action99_available")
        ):
            raise RuntimeError("G1 safe-action result failed required preflight fields")
        if operation == "probe" and (
            result.get("action23_invoked") or result.get("action99_invoked")
        ):
            raise RuntimeError("G1 probe unexpectedly reported Action execution")
        if operation == "notice" and not (
            result.get("action23_invoked")
            and result.get("action23_code") == 0
            and result.get("action99_invoked")
            and result.get("action99_code") == 0
            and not result.get("motion_uncertain")
        ):
            raise RuntimeError("G1 notice result did not confirm one safe action cycle")
        with self._state_lock:
            self._last_result = dict(result)
        return result

    def probe(self) -> dict[str, Any]:
        """Run the read-only remote helper operation exactly once."""
        try:
            return self._run_helper("probe")
        except Exception as exc:
            self._latch_failure(str(exc))
            raise

    def play_motion(self, motion: str) -> None:
        self._last_motion_succeeded = False
        if motion != "notice":
            LOGGER.warning("Unsupported SSH G1 motion '%s' is a safe no-op", motion)
            return
        if not self.motion_enabled:
            LOGGER.warning("SSH G1 notice skipped because motion is disabled")
            return
        if self._shutdown_requested.is_set():
            LOGGER.warning("SSH G1 notice skipped because shutdown was requested")
            return
        reason = self.motion_disabled_reason
        if reason:
            LOGGER.warning("SSH G1 notice disabled after failure: %s", reason)
            return

        with self._operation_lock:
            if self._shutdown_requested.is_set():
                return
            reason = self.motion_disabled_reason
            if reason:
                return
            try:
                self._run_helper("notice")
            except Exception as exc:
                self._latch_failure(str(exc))
                raise
            self._last_motion_succeeded = True

    def wait_for_motion_complete(
        self, motion: str, timeout: float | None = None
    ) -> bool:
        del timeout
        return motion == "notice" and self._last_motion_succeeded

    def request_shutdown(self) -> None:
        self._shutdown_requested.set()
        # Serialize with the final Popen boundary.  An already-running helper
        # is deliberately left alive to own its Action 99 cleanup.
        with self._start_lock:
            pass

    def close(self) -> None:
        self.request_shutdown()
        with self._state_lock:
            process = self._active_process
        if process is None or process.poll() is not None:
            return
        try:
            process.wait(timeout=self.helper_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=self.termination_grace_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
