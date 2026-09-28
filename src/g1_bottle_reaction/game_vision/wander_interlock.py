"""Small SSH interlock for the separately running Mapless Wander process."""
from __future__ import annotations

from pathlib import PurePosixPath
import shlex
import subprocess


class WanderInterlockError(RuntimeError):
    pass


def require_wander_approval(*, robot: str, enable_real_robot: bool,
                            operator_approved: bool) -> None:
    """Require caller intent before creating any remote Wander operation."""
    if (robot not in {"g1", "g1-ssh", "motiondecode"}
            or enable_real_robot is not True or operator_approved is not True):
        raise ValueError(
            "Remote Wander requires a real --robot, --enable-real-robot and "
            "--operator-approved-wander (site ready; robot will move)"
        )


class RemoteWanderController:
    """Start and safely stop one verified remote Wander process."""

    PID_FILE = "/tmp/g1-mapless-wander.pid"
    LOG_FILE = "/tmp/g1-mapless-wander.log"
    SCRIPT_MARKER = "scripts/g1-wander-reactive-mvp.py"
    OWNERSHIP_CONTRACT = "--require-locomotion-ownership-v1"

    def __init__(
        self,
        ssh_target: str,
        remote_dir: str = "/home/ubuntu/dev/g1-bottle-reaction-wander",
        *,
        robot: str = "mock",
        enable_real_robot: bool = False,
        operator_approved: bool = False,
        ssh_control: str | None = None,
        stop_timeout: float = 5.0,
        runner=subprocess.run,
    ) -> None:
        require_wander_approval(
            robot=robot, enable_real_robot=enable_real_robot,
            operator_approved=operator_approved,
        )
        if not ssh_target:
            raise ValueError("Wander SSH target is required")
        if stop_timeout <= 0:
            raise ValueError("Wander stop timeout must be positive")
        self.ssh_target = ssh_target
        self.remote_dir = str(PurePosixPath(remote_dir))
        self.ssh_control = ssh_control
        self.stop_timeout = float(stop_timeout)
        self._runner = runner
        self._running = False
        self._closed = False

    @property
    def running(self) -> bool:
        return self._running

    def _ssh(self, script: str, *, timeout: float) -> subprocess.CompletedProcess:
        command = ["ssh"]
        if self.ssh_control:
            command.extend(["-S", self.ssh_control])
        command.extend([
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            self.ssh_target,
            "bash -lc " + shlex.quote(script),
        ])
        try:
            return self._runner(
                command, check=True, capture_output=True, text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise WanderInterlockError(detail.strip()) from exc

    @classmethod
    def _process_helpers(cls) -> str:
        marker = shlex.quote(cls.SCRIPT_MARKER)
        return (
            "is_wander() { "
            "case \"$1\" in ''|*[!0-9]*) return 1;; esac; "
            "[ -r \"/proc/$1/cmdline\" ] || return 1; "
            f"tr '\\0' ' ' < \"/proc/$1/cmdline\" | grep -F -- {marker} >/dev/null; "
            "};"
        )

    def start(self) -> None:
        if self._closed:
            raise WanderInterlockError("Wander controller is closed")
        remote_dir = shlex.quote(self.remote_dir)
        pid_file = shlex.quote(self.PID_FILE)
        log_file = shlex.quote(self.LOG_FILE)
        python = shlex.quote(str(PurePosixPath(self.remote_dir) / ".venv-wander/bin/python"))
        script = shlex.quote(str(PurePosixPath(self.remote_dir) / self.SCRIPT_MARKER))
        ownership = shlex.quote(self.OWNERSHIP_CONTRACT)
        command = (
            f"{self._process_helpers()} "
            f"pid_file={pid_file}; "
            "if [ -f \"$pid_file\" ]; then "
            "IFS= read -r old_pid < \"$pid_file\" || old_pid=; "
            "if kill -0 \"$old_pid\" 2>/dev/null && is_wander \"$old_pid\"; then "
            f"if tr '\\0' '\\n' < \"/proc/$old_pid/cmdline\" | grep -Fx -- {ownership} >/dev/null; then "
            "echo RUNNING; exit 0; fi; "
            "echo 'Existing Wander lacks ownership contract' >&2; exit 2; "
            "fi; rm -f -- \"$pid_file\"; fi; "
            f"cd -- {remote_dir}; "
            f"nohup env G1_SDK_PATH=/home/unitree/unitree_sdk2_python {python} {script} "
            "--duration 3600 --max-pulses 10000 --robot g1 --enable-real-robot "
            "--execute-real-g1 --i-understand-this-will-move-the-robot "
            f"{ownership} "
            f"> {log_file} 2>&1 < /dev/null & pid=$!; "
            "tmp=\"${pid_file}.$$\"; printf '%s\\n' \"$pid\" > \"$tmp\"; "
            "mv -f -- \"$tmp\" \"$pid_file\"; sleep 0.5; "
            "if kill -0 \"$pid\" 2>/dev/null && is_wander \"$pid\"; then "
            "echo STARTED; exit 0; fi; "
            "rm -f -- \"$pid_file\"; "
            f"tail -n 20 -- {log_file} >&2 2>/dev/null || true; exit 1"
        )
        result = self._ssh(command, timeout=10.0)
        if not any(line in {"STARTED", "RUNNING"} for line in result.stdout.splitlines()):
            raise WanderInterlockError("remote Wander startup was not confirmed")
        self._running = True
        print("WANDER START: remote process confirmed", flush=True)

    def _stop_remote(self, *, require_running: bool) -> None:
        pid_file = shlex.quote(self.PID_FILE)
        tenths = max(1, int(self.stop_timeout * 10))
        command = (
            f"{self._process_helpers()} pid_file={pid_file}; "
            "if [ ! -f \"$pid_file\" ]; then echo ABSENT; exit 0; fi; "
            "IFS= read -r pid < \"$pid_file\" || pid=; "
            "case \"$pid\" in ''|*[!0-9]*) echo 'invalid Wander PID file' >&2; exit 2;; esac; "
            "if ! kill -0 \"$pid\" 2>/dev/null; then rm -f -- \"$pid_file\"; "
            "echo ABSENT; exit 0; fi; "
            "if ! is_wander \"$pid\"; then echo 'Wander PID cmdline mismatch' >&2; exit 3; fi; "
            "kill -TERM \"$pid\"; "
            f"i=0; while [ \"$i\" -lt {tenths} ]; do "
            "if ! kill -0 \"$pid\" 2>/dev/null || ! is_wander \"$pid\"; then "
            "rm -f -- \"$pid_file\"; echo STOPPED; exit 0; fi; "
            "sleep 0.1; i=$((i + 1)); done; "
            "echo 'Wander did not exit after SIGTERM' >&2; exit 4"
        )
        result = self._ssh(command, timeout=self.stop_timeout + 7.0)
        states = set(result.stdout.splitlines())
        if "STOPPED" in states:
            self._running = False
            print("WANDER STOP: process exit confirmed", flush=True)
            return
        if "ABSENT" in states and not require_running:
            self._running = False
            return
        raise WanderInterlockError("remote Wander stop was not confirmed")

    def stop_and_wait(self) -> None:
        if not self._running:
            raise WanderInterlockError("Wander startup/running state is not confirmed")
        self._stop_remote(require_running=True)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._stop_remote(require_running=self._running)
        finally:
            self._closed = True
