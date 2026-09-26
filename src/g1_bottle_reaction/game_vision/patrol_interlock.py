"""Local IPC client for stop-react-resume Patrol coordination."""

from __future__ import annotations

import json
from pathlib import Path
import socket


class PatrolInterlockError(RuntimeError):
    pass


class LocalPatrolController:
    def __init__(self, socket_path: str | Path, *, pause_timeout: float = 30.0) -> None:
        self.socket_path = Path(socket_path)
        if not self.socket_path.is_absolute():
            raise ValueError("Patrol control socket must be absolute")
        if pause_timeout <= 0:
            raise ValueError("Patrol pause timeout must be positive")
        self.pause_timeout = float(pause_timeout)
        self._pause_owned = False
        self._failed = False
        self.last_stop_sent_monotonic: float | None = None

    @property
    def running(self) -> bool:
        return not self._pause_owned and not self._failed

    def _request(self, payload: dict[str, object], timeout: float) -> dict[str, object]:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(str(self.socket_path))
                stream = client.makefile("rwb")
                stream.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
                stream.flush()
                response = json.loads(stream.readline())
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            raise PatrolInterlockError(str(exc)) from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            detail = response.get("error") if isinstance(response, dict) else response
            raise PatrolInterlockError(str(detail or "Patrol control request failed"))
        return response

    def stop_and_wait(self) -> None:
        if self._failed:
            raise PatrolInterlockError("Patrol interlock is fault-latched")
        response = self._request(
            {
                "operation": "pause",
                "reason": "reaction",
                "timeout": self.pause_timeout,
            },
            self.pause_timeout + 2.0,
        )
        if response.get("error") or response.get("stopped") or not response.get("paused"):
            raise PatrolInterlockError(
                str(response.get("error") or "Patrol did not confirm PAUSED")
            )
        self._pause_owned = True
        value = response.get("stop_sent_monotonic")
        self.last_stop_sent_monotonic = (
            float(value) if isinstance(value, (int, float)) else None
        )

    def wait_reaction_ready(self) -> None:
        if self._failed or not self._pause_owned:
            raise PatrolInterlockError("Patrol pause is not safely owned")
        response = self._request({"operation": "reaction_ready"}, 3.0)
        if (response.get("error") or response.get("stopped")
                or not response.get("paused")
                or response.get("telemetry_recovering")):
            raise PatrolInterlockError(
                str(response.get("error") or "Patrol telemetry is not reaction-ready")
            )

    def start(self) -> None:
        if self._failed:
            raise PatrolInterlockError("Patrol resume is inhibited after a fault")
        if not self._pause_owned:
            raise PatrolInterlockError("Reaction does not own a Patrol pause")
        response = self._request({"operation": "resume"}, 5.0)
        if response.get("error") or response.get("stopped") or response.get("paused"):
            raise PatrolInterlockError(
                str(response.get("error") or "Patrol resume was not confirmed")
            )
        self._pause_owned = False

    def abort(self, reason: str) -> None:
        self._failed = True
        try:
            self._request({"operation": "stop", "reason": reason}, 5.0)
        except PatrolInterlockError:
            pass
        self._pause_owned = False

    def close(self) -> None:
        if self._pause_owned:
            self.abort("Reaction process closed while Patrol was paused")
