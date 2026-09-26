"""Minimal local pause/resume IPC for the separate Patrol process."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import threading


class PatrolControlServer:
    def __init__(self, path: str | Path, controller) -> None:
        self.path = Path(path)
        self.controller = controller
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._socket is not None:
            raise RuntimeError("Patrol control server is already running")
        if self.path.exists():
            raise RuntimeError(f"Patrol control socket already exists: {self.path}")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        server.listen(4)
        server.settimeout(0.2)
        self._socket = server
        self._thread = threading.Thread(
            target=self._serve, name="patrol-control", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                stream = connection.makefile("rwb")
                try:
                    request = json.loads(stream.readline())
                    response = self._dispatch(request)
                except Exception as exc:
                    response = {
                        "ok": False,
                        "error": str(exc),
                        **self.controller.control_status(),
                    }
                stream.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
                stream.flush()

    def _dispatch(self, request: object) -> dict[str, object]:
        if not isinstance(request, dict):
            raise ValueError("Patrol control request must be an object")
        operation = request.get("operation")
        if operation == "status":
            return {"ok": True, **self.controller.control_status()}
        if operation == "pause":
            reason = str(request.get("reason") or "reaction")
            timeout = float(request.get("timeout", 30.0))
            return {
                "ok": True,
                **self.controller.request_reaction_pause(reason, timeout),
            }
        if operation == "resume":
            self.controller.resume()
            return {"ok": True, **self.controller.control_status()}
        if operation == "reaction_ready":
            return {"ok": True, **self.controller.verify_reaction_ready()}
        if operation == "stop":
            self.controller.stop()
            return {"ok": True, **self.controller.control_status()}
        raise ValueError(f"Unsupported Patrol control operation: {operation!r}")

    def close(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def request(path: str | Path, payload: dict[str, object], timeout: float = 5.0) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        stream = client.makefile("rwb")
        stream.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        stream.flush()
        response = json.loads(stream.readline())
    if not isinstance(response, dict):
        raise RuntimeError("Invalid Patrol control response")
    return response
