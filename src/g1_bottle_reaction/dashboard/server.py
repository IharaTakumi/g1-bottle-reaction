"""HTTP server for the dashboard; listens on 127.0.0.1 only."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import time

from .runner import Dashboard

STATIC = Path(__file__).resolve().parent / "static"
STREAM_PERIOD_S = 0.25
CAMERA_PERIOD_S = 0.1


class Handler(BaseHTTPRequestHandler):
    dashboard: Dashboard
    port: int

    def log_message(self, *_args) -> None:
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, value: object) -> None:
        self._send(code, json.dumps(value, ensure_ascii=False, default=str).encode(), "application/json")

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._json(200, self.dashboard.snapshot())
        elif self.path == "/api/stream":
            self._stream()
        elif self.path.startswith("/api/camera.mjpg"):
            self._camera()
        else:
            self._send(404, b"not found", "text/plain")

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                data = json.dumps(self.dashboard.snapshot(), ensure_ascii=False, default=str)
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()
                time.sleep(STREAM_PERIOD_S)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _camera(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        try:
            while True:
                try:
                    stamp = self.dashboard.frame_path.stat().st_mtime
                    if stamp != last:
                        frame = self.dashboard.frame_path.read_bytes()
                        last = stamp
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                        self.wfile.flush()
                except FileNotFoundError:
                    pass
                time.sleep(CAMERA_PERIOD_S)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:
        # Only this dashboard's own page may command G1 (no cross-site requests).
        origin = self.headers.get("Origin")
        allowed = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
        if origin not in allowed or self.headers.get("Content-Type") != "application/json":
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        if self.path != "/api/action":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            action = body.get("action")
            params = body.get("params") or {}
            if action == "estop":
                result = self.dashboard.emergency_stop()
            elif action == "finish":
                result = self.dashboard.finish()
            elif action == "settings":
                result = self.dashboard.change_settings(params)
            elif action == "volume":
                result = self.dashboard.set_volume(params.get("level"))
            elif action == "release":
                result = {"ok": True, "job": self.dashboard.release()}
            else:
                result = {"ok": True, "job": self.dashboard.start(str(action), params)}
            self._json(200, result)
        except (ValueError, RuntimeError, TypeError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})


def serve(port: int = 8770, dashboard: Dashboard | None = None) -> None:
    dashboard = dashboard or Dashboard()
    handler = type("DashboardHandler", (Handler,), {"dashboard": dashboard, "port": port})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    print(f"G1 dashboard: http://127.0.0.1:{port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        dashboard.close()
