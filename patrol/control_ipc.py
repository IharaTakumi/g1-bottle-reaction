"""Minimal local pause/resume IPC for the separate Patrol process."""

from __future__ import annotations

import json
import math
from pathlib import Path
import socket
import threading
import time

try:
    from .control_lease import load_control_timing
    from .cleanup import run_cleanup
except ImportError:  # run_patrol.py uses the standalone patrol directory.
    from control_lease import load_control_timing
    from cleanup import run_cleanup


class PatrolControlServer:
    def __init__(self, path: str | Path, controller) -> None:
        self.path = Path(path)
        self.controller = controller
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._read_timeout = load_control_timing().ipc_read_timeout_s
        # A waiting pause/readiness request must not block supervisor heartbeat.
        # Bound concurrent clients so malformed/partial clients cannot spawn
        # unbounded workers; saturation still expires the independent lease.
        self._slots = threading.BoundedSemaphore(8)

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
        try:
            self._accept_clients()
        except BaseException as exc:
            if not self._stop.is_set():
                self.controller.control_fault(f"control IPC failed: {exc}")
        finally:
            if not self._stop.is_set():
                self.controller.control_fault("control IPC server exited unexpectedly")

    def _accept_clients(self) -> None:
        server = self._socket
        assert server is not None
        while not self._stop.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            if not self._slots.acquire(blocking=False):
                connection.close()
                continue
            try:
                threading.Thread(target=self._client, args=(connection,),
                                 name="patrol-control-client", daemon=True).start()
            except BaseException:
                connection.close()
                self._slots.release()
                raise

    def _client(self, connection: socket.socket) -> None:
        try:
            with connection:
                try:
                    payload = bytearray()
                    deadline = time.monotonic() + self._read_timeout
                    while b"\n" not in payload:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("control request deadline exceeded")
                        connection.settimeout(remaining)
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        payload.extend(chunk)
                        if len(payload) > 4096:
                            raise ValueError("control request too large")
                    request_body = json.loads(payload.split(b"\n", 1)[0])
                    response = self._dispatch(request_body)
                except (ValueError, UnicodeError, RuntimeError, TimeoutError) as exc:
                    response = {
                        "ok": False,
                        "error": str(exc),
                    }
                connection.settimeout(self._read_timeout)
                connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass  # A disappearing client does not kill the listening server.
        except BaseException as exc:
            if not self._stop.is_set():
                self.controller.control_fault(f"control IPC client failed: {exc}")
        finally:
            self._slots.release()

    def _dispatch(self, request: object) -> dict[str, object]:
        if self._stop.is_set():
            raise RuntimeError("Patrol control is closing")
        if not isinstance(request, dict):
            raise ValueError("Patrol control request must be an object")
        operation = request.get("operation")
        if operation == "heartbeat":
            return {"ok": True, **self.controller.heartbeat(request.get("lease_id"))}
        if operation == "status":
            return {"ok": True, **self.controller.control_status()}
        if operation == "pause":
            reason = str(request.get("reason") or "reaction")
            try:
                timeout = float(request.get("timeout", 30.0))
            except (TypeError, ValueError) as exc:
                raise ValueError("pause timeout must be numeric") from exc
            if not math.isfinite(timeout) or not 0 < timeout <= 30.0:
                raise ValueError("pause timeout must be within (0, 30] seconds")
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
        # Mark expected shutdown before any teardown can wake server callbacks.
        self._stop.set()

        def close_socket():
            if self._socket is not None:
                self._socket.close()
                self._socket = None

        def join_server():
            if self._thread is not None:
                self._thread.join(timeout=1.0)
                if self._thread.is_alive():
                    raise RuntimeError("Patrol IPC thread did not exit")
                self._thread = None

        run_cleanup([
            ("Patrol STOP", self.controller.stop),
            ("IPC socket close", close_socket),
            ("IPC thread join", join_server),
            ("IPC socket unlink", lambda: self.path.unlink(missing_ok=True)),
        ])


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
