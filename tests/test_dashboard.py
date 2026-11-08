from __future__ import annotations

import json
import sys
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "patrol"))

from g1_bottle_reaction.dashboard import runner  # noqa: E402
from g1_bottle_reaction.dashboard.logparse import GameView  # noqa: E402
from g1_bottle_reaction.dashboard.server import Handler  # noqa: E402
from g1_bottle_reaction.game_vision.dual import write_frame  # noqa: E402
from locomotion_adapter import DryRunLocomotionAdapter  # noqa: E402
from patrol_controller import PatrolConfig, PatrolController  # noqa: E402
from run_patrol import AlwaysClear, VirtualTime  # noqa: E402


def test_log_lines_become_reactions_detections_and_checks():
    view = GameView()
    for line in [
        "[WARN] arm temperature: 61 C (stop at 65)",
        "CHECK=PASS",
        "[17:58:48.979] PERSON DETECTED count=2 conf=0.88 bbox=(1,2,3,4)",
        "PERSON REACTION EVENT: audio + motion=motiondecode:salute; confirmed_monotonic=1.0",
        "Audio triggered: /a/person/person_salute_2.wav; monotonic=1.0",
        'MOTIONDECODE RESULT: {"reaction": "salute", "status": "pass", "clip_start_monotonic_s": 1.0,'
        ' "execution_completed_monotonic_s": 8.3, "q0_return_error_rad": 0.024}',
        "g1: 1280x720, FPS=30.0, local_age=21.7ms, frames=240, display_skips=0, RTP_lost=0, status=LIVE",
        "display_loop FPS=97.5; YOLO FPS=14.7, inference=5.4ms, result_age=88.2ms, status=RUNNING",
        "[patrol] FORWARD_OUT odom_progress=1.000m lateral=0.010m",
        "[patrol] loop complete count=2",
        "[patrol] ODOM SOURCE STALE -> FINAL STOP",
    ]:
        view.feed(line, now=100.0)
    state = view.snapshot()
    assert state["checks"]["arm temperature"]["level"] == "WARN"
    assert state["check_result"] == "PASS"
    assert state["visible"]["person"]["count"] == 2
    reaction = state["reactions"][0]
    assert (reaction["target"], reaction["motion"], reaction["voice"], reaction["status"]) == (
        "person", "salute", "person_salute_2.wav", "pass")
    assert reaction["duration_s"] == 7.3
    assert state["camera"]["fps"] == 30.0 and state["camera"]["yolo_fps"] == 14.7
    assert state["loops_completed"] == 2
    texts = [event["text"] for event in state["events"]]
    assert not any("odom_progress" in text for text in texts)
    assert any(event["warn"] for event in state["events"] if "FINAL STOP" in event["text"])


def test_manual_motion_keeps_its_voice():
    view = GameView()
    view.feed("Audio triggered: /a/person/person_kinniku_2.wav; monotonic=1.0")
    view.feed('MOTIONDECODE RESULT: {"reaction": "kinniku", "status": "pass"}')
    row = view.snapshot()["reactions"][0]
    assert (row["target"], row["motion"], row["voice"]) == ("manual", "kinniku", "person_kinniku_2.wav")


def test_job_arguments_are_bounded():
    assert runner.job_arguments("run", {"laps": 3}) == ["--laps", "3", "--speed", "0.5", "--course", "4", "--yes"]
    assert runner.job_arguments("run", {"minutes": 5, "speed": 0.8, "course": 2}) == [
        "--minutes", "5", "--speed", "0.8", "--course", "2", "--yes"]
    assert runner.job_arguments("stand", {"duration": 90}) == ["--duration", "90", "--yes"]
    assert runner.job_arguments("motion", {"name": "salute"}) == ["salute", "--yes"]
    assert runner.job_arguments("motion", {"name": "salute", "voice": "person/person_salute_1.wav"}) == [
        "salute", "--voice", "person/person_salute_1.wav", "--yes"]
    for kind, params in [("run", {"laps": 0}), ("run", {"minutes": 31}), ("dry", {"duration": 1}),
                         ("motion", {"name": "rm -rf"}), ("walk", {}),
                         ("motion", {"name": "salute", "voice": "banana/banana_surprise_1.wav"}),
                         ("run", {"laps": 1, "speed": 0.85}), ("run", {"laps": 1, "course": 6})]:
        with pytest.raises(ValueError):
            runner.job_arguments(kind, params)


def test_moving_commands_need_confirmation(tmp_path):
    dashboard = runner.Dashboard.__new__(runner.Dashboard)
    for kind in ("run", "stand", "motion"):
        with pytest.raises(ValueError, match="confirmation"):
            dashboard.start(kind, {"laps": 1, "name": "salute"})


def test_finish_stops_after_the_current_loop():
    time = VirtualTime()
    controller = PatrolController(DryRunLocomotionAdapter(), AlwaysClear(), PatrolConfig(),
                                  clock=time.clock, sleep=time.sleep, emit=lambda _line: None)
    controller.request_finish()
    controller.run(cycles=3)
    status = controller.control_status()
    assert status["loops_completed"] == 1 and status["finish_requested"]
    assert status["origin"] == {"x": 0.0, "y": 0.0, "yaw": 0.0}
    assert status["odom"]["x"] == pytest.approx(0.0, abs=0.2)
    assert status["route"]["return_m"] == 4.0


def test_status_survives_a_guard_without_snapshot():
    controller = PatrolController(DryRunLocomotionAdapter(), AlwaysClear(), PatrolConfig())
    status = controller.control_status()
    assert status["guard"] is None and status["loops_completed"] == 0


def test_frame_output_replaces_the_file_atomically(tmp_path):
    path = tmp_path / "frame.jpg"
    write_frame(path, np.zeros((8, 8, 3), np.uint8))
    assert cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR).shape == (8, 8, 3)
    assert not (tmp_path / "frame.jpg.tmp").exists()


class FakeDashboard:
    def __init__(self):
        self.started = []

    def snapshot(self):
        return {"mode": "idle"}

    def start(self, kind, params):
        self.started.append(kind)
        return {"kind": kind}


def test_only_the_dashboard_page_can_send_commands():
    fake = FakeDashboard()
    server = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"dashboard": fake, "port": 0}))
    port = server.server_address[1]
    server.RequestHandlerClass.port = port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        def post(origin):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            connection.request("POST", "/api/action", json.dumps({"action": "check"}), headers)
            return connection.getresponse().status
        assert post(None) == 403
        assert post("http://evil.example") == 403
        assert post(f"http://127.0.0.1:{port}") == 200
        assert fake.started == ["check"]
    finally:
        server.shutdown()


def patrol(**config):
    time = VirtualTime()
    lines = []
    controller = PatrolController(DryRunLocomotionAdapter(), AlwaysClear(),
                                  PatrolConfig(max_forward_speed_m_s=0.8, **config),
                                  clock=time.clock, sleep=time.sleep, emit=lines.append)
    return controller, lines


def test_settings_wait_for_the_next_loop():
    controller, lines = patrol()
    seen = []
    original = controller._run_forward

    def forward(state, target):
        seen.append((controller._loops_completed, state.value, target, controller.config.forward_speed_m_s))
        if controller._loops_completed == 0 and state.value == "FORWARD_RETURN":
            controller.request_settings(speed_m_s=0.8, course_length_m=2.0)
        original(state, target)
    controller._run_forward = forward
    controller.run(cycles=2)
    first = [entry for entry in seen if entry[0] == 0]
    second = [entry for entry in seen if entry[0] == 1]
    assert {entry[3] for entry in first} == {0.30} and first[1][2] == 4.0
    assert [entry[2] for entry in second] == [1.0, 2.0, 1.0] and {entry[3] for entry in second} == {0.8}
    assert controller.config.max_physical_speed_m_s == 0.8
    assert any("SETTINGS applied" in line for line in lines)


def test_settings_reject_values_outside_the_limits():
    controller, _ = patrol()
    for bad in ({"speed_m_s": 0.81}, {"speed_m_s": 0.05}, {"course_length_m": 0.5}, {"course_length_m": 5.5}, {}):
        with pytest.raises(ValueError):
            controller.request_settings(**bad)
    assert controller.control_status()["pending_settings"] is None


def test_patrol_ipc_reports_the_request_error(tmp_path):
    from control_ipc import PatrolControlServer, request
    controller, _ = patrol()
    controller._last_error = "older failure"
    server = PatrolControlServer(tmp_path / "p.sock", controller)
    server.start()
    try:
        reply = request(tmp_path / "p.sock", {"operation": "settings", "speed_m_s": 2.0})
    finally:
        server.close()
    assert reply["ok"] is False and "speed must be" in reply["error"]


def test_relay_accepts_the_new_speed_limit_only_up_to_0_8():
    from locomotion_relay import decode
    assert decode('{"seq": 1, "vx": 0.8, "vy": 0, "vyaw": 0}', False, 0.80)[1] == "move"
    with pytest.raises(ValueError):
        decode('{"seq": 2, "vx": 0.81, "vy": 0, "vyaw": 0}', False, 0.80)
    with pytest.raises(ValueError):
        decode('{"seq": 3, "vx": 0.6, "vy": 0, "vyaw": 0}', False, 0.50)
