"""Mock cleanup, actual local IPC waits, and OS-process supervisor loss."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from unittest.mock import Mock

import pytest

from test_integrated_control_lease import load_supervisor
from test_patrol_control_lease import build, moves, wait_until
from cleanup import CleanupError
from control_ipc import PatrolControlServer, request
from locomotion_adapter import DryRunLocomotionAdapter
from patrol_controller import PatrolController
from run_patrol import AlwaysClear
import run_patrol


def realtime_controller():
    return PatrolController(DryRunLocomotionAdapter(), AlwaysClear(), emit=lambda _: None,
                            lease_id="wait-session", lease_timeout_s=.3)


@pytest.mark.parametrize("reject", [False, True])
def test_actual_ipc_long_resume_keeps_main_owned_heartbeat(tmp_path, monkeypatch, reject):
    module = load_supervisor()
    controller = realtime_controller()
    server = PatrolControlServer(tmp_path / "wait.sock", controller)
    server.start()
    monkeypatch.setattr(module, "PATROL_CONTROL_SOCKET", server.path)
    owner = threading.get_ident()
    heartbeat_threads = []
    refresh = module.refresh_patrol_lease
    def record_heartbeat(*args):
        heartbeat_threads.append(threading.get_ident())
        return refresh(*args)
    monkeypatch.setattr(module, "refresh_patrol_lease", record_heartbeat)
    # Exercise production resume's actual telemetry barrier for > one lease.
    ready_at = time.monotonic() + .5
    sample = controller.locomotion.imu_sample
    controller.locomotion.imu_sample = lambda: dict(
        sample(), imu_ready=time.monotonic() >= ready_at)
    original_resume = controller.resume
    def resume():
        original_resume()
        if reject:
            raise RuntimeError("delayed request rejection")
    controller.resume = resume
    try:
        refresh("wait-session", .1)
        started = time.monotonic()
        if reject:
            with pytest.raises(RuntimeError, match="delayed request rejection"):
                module.patrol_request_while_heartbeating(
                    {"operation": "resume"}, "wait-session", .1)
        else:
            response = module.patrol_request_while_heartbeating(
                {"operation": "resume"}, "wait-session", .1)
            assert response["ok"] and not response["paused"]
        assert time.monotonic() - started > .3
        assert len(heartbeat_threads) >= 3
        assert set(heartbeat_threads) == {owner}
        assert controller.control_status()["lease_active"]
        assert controller.control_status()["control_fault"] is None
    finally:
        server.close()


def test_request_worker_hang_has_overall_deadline_and_cannot_send_heartbeats(tmp_path, monkeypatch):
    module = load_supervisor()
    controller = realtime_controller()
    server = PatrolControlServer(tmp_path / "deadline.sock", controller)
    server.start()
    monkeypatch.setattr(module, "PATROL_CONTROL_SOCKET", server.path)
    release, finished = threading.Event(), threading.Event()
    original = module.patrol_request
    def request_once(payload, timeout=5):
        if payload["operation"] == "resume":
            try:
                release.wait(3)
                return {"ok": True}
            finally:
                finished.set()
        return original(payload, timeout)
    monkeypatch.setattr(module, "patrol_request", request_once)
    try:
        controller.heartbeat("wait-session")
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="overall deadline"):
            module.patrol_request_while_heartbeating(
                {"operation": "resume"}, "wait-session", .1, timeout=.45)
        assert .45 <= time.monotonic() - started < 2
        # Worker is still waiting, but it cannot refresh the lease on its own.
        assert not finished.is_set()
        time.sleep(.35)
        with pytest.raises(RuntimeError, match="expired"):
            controller._send_move(.3, 0)
        assert controller.control_status()["control_fault"]
    finally:
        release.set()
        assert finished.wait(3)
        server.close()


def test_ipc_stop_failure_still_closes_socket_joins_and_unlinks(tmp_path):
    controller, loco, _ = build()
    server = PatrolControlServer(tmp_path / "close.sock", controller)
    server.start()
    thread = server._thread
    loco.stop = Mock(side_effect=OSError("STOP failed"))
    with pytest.raises(CleanupError, match="Patrol STOP.*STOP failed"):
        server.close()
    assert server._stop.is_set()
    assert server._socket is None
    assert not thread.is_alive()
    assert not server.path.exists()
    assert controller.control_status()["stopped"]
    assert controller.control_status()["control_fault"] is None
    assert not controller._send_move(.3, 0)


@pytest.mark.parametrize("failures", [[], ["ipc"], ["ipc", "loco", "source"]])
def test_patrol_main_attempts_all_resource_cleanup(monkeypatch, failures):
    events = []
    def closer(name):
        def close():
            events.append(name)
            if name in failures:
                raise OSError(name + " failed")
        return close
    source = Mock(close=closer("source"))
    loco = DryRunLocomotionAdapter()
    loco.close = closer("loco")
    control = Mock(close=closer("ipc"))
    monkeypatch.setattr(run_patrol, "observe_lidar", lambda _: (AlwaysClear(), source, {"front_ready": True}))
    monkeypatch.setattr(run_patrol, "writer_conflicts", lambda: [])
    monkeypatch.setattr(run_patrol, "UdpLocomotionAdapter", lambda *args: loco)
    monkeypatch.setattr(run_patrol, "PatrolControlServer", lambda *args: control)
    monkeypatch.setattr(run_patrol.PatrolController, "run", lambda *a, **k: None)
    args = ["--mode", "real", "--arm", "--one-cycle", "--operator-approved-one-cycle",
            "--control-socket", "/unused"]
    if failures:
        with pytest.raises(CleanupError) as exc:
            run_patrol.main(args)
        for name in failures:
            assert name + " failed" in str(exc.value)
    else:
        assert run_patrol.main(args) == 0
    assert events == ["ipc", "loco", "source"]
    assert moves(loco) == []


def test_multiple_ipc_teardown_failures_do_not_skip_later_steps(tmp_path):
    controller, loco, _ = build()
    server = PatrolControlServer(tmp_path / "failed-close.sock", controller)
    server.path.touch()
    loco.stop = Mock(side_effect=OSError("stop error"))
    server._socket = Mock(close=Mock(side_effect=OSError("socket error")))
    thread = Mock(is_alive=Mock(return_value=False))
    server._thread = thread
    with pytest.raises(CleanupError) as exc:
        server.close()
    assert "stop error" in str(exc.value) and "socket error" in str(exc.value)
    thread.join.assert_called_once()
    assert not server.path.exists()


def test_cleanup_client_failure_race_is_expected_and_cannot_resume(tmp_path):
    controller, loco, _ = build()
    controller.control_fault = Mock(wraps=controller.control_fault)
    server = PatrolControlServer(tmp_path / "race.sock", controller)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def dispatch(_):
        entered.set()
        assert release.wait(3)
        raise AssertionError("client teardown race")
    server._dispatch = dispatch
    server.start()
    def client():
        try:
            request(server.path, {"operation": "status"})
        except (ValueError, OSError):
            pass
        finally:
            finished.set()
    worker = threading.Thread(target=client)
    worker.start()
    try:
        assert entered.wait(3)
        server.close()
        with pytest.raises(RuntimeError, match="closing"):
            PatrolControlServer._dispatch(server, {"operation": "resume"})
        release.set()
        assert finished.wait(3)
        controller.control_fault.assert_not_called()
        with pytest.raises(RuntimeError):
            controller.resume()
        assert not controller._send_move(.3, 0)
        assert moves(loco) == []
    finally:
        release.set()
        worker.join(3)


def test_supervisor_signal_failure_still_escalates(monkeypatch):
    module = load_supervisor()
    process = Mock(pid=123, poll=Mock(return_value=None))
    process.wait.side_effect = [subprocess.TimeoutExpired("mock", 4), None]
    signals = []
    def send(pid, sig):
        signals.append(sig)
        if sig == signal.SIGINT:
            raise OSError("fake SIGINT failure")
    monkeypatch.setattr(module.os, "killpg", send)
    with pytest.raises(RuntimeError, match="fake SIGINT failure"):
        module.stop_group(process, "PATROL")
    assert signals == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]


def test_supervisor_request_deadline_runs_all_cleanup_even_when_children_fail(monkeypatch):
    module = load_supervisor()
    monkeypatch.setattr(module, "preflight", lambda _: None)
    monkeypatch.setattr(module, "wait_for_patrol_paused", lambda *a: None)
    monkeypatch.setattr(module, "refresh_patrol_lease", lambda *a: {})
    monkeypatch.setattr(module, "patrol_request_while_heartbeating",
                        Mock(side_effect=TimeoutError("overall deadline")))
    process = Mock(pid=123, poll=Mock(return_value=None))
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=process))
    clock = iter([0, 3])  # Bypass only the unrelated startup delay.
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    restored = []
    def install(sig, handler):
        if handler == signal.SIG_DFL:
            restored.append(sig)
        return signal.SIG_DFL
    monkeypatch.setattr(module.signal, "signal", install)
    closed = []
    def close(process, name):
        closed.append(name)
        raise OSError(name + " failed")
    monkeypatch.setattr(module, "stop_group", close)
    with pytest.raises(RuntimeError, match="cleanup partial failure") as exc:
        module.main(["--real-patrol", "--operator-approved-real-patrol"])
    assert isinstance(exc.value.__context__, TimeoutError)
    assert closed == ["PATROL", "REACTION", "MOTIONDECODE_DRY_RUN"]
    assert all(name + " failed" in str(exc.value) for name in closed)
    assert restored == [signal.SIGINT, signal.SIGTERM]


def events(path):
    if not path.exists():
        return []
    lines = path.read_text().splitlines(keepends=True)
    return [json.loads(line) for line in lines if line.endswith("\n")]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process signals and Unix IPC")
@pytest.mark.parametrize("loss", ["kill", "hang"])
def test_actual_supervisor_process_loss_self_stops_patrol(tmp_path, loss):
    fixture = Path(__file__).parent / "fixtures/control_lease_process.py"
    path = tmp_path / "process.sock"
    patrol_log, supervisor_log = tmp_path / "patrol.jsonl", tmp_path / "supervisor.jsonl"
    lease_id = str(uuid.uuid4())
    children = []
    with (tmp_path / "children.log").open("w") as output:
        try:
            patrol = subprocess.Popen([sys.executable, "-B", str(fixture), "patrol",
                str(path), str(patrol_log), lease_id], stdout=output, stderr=output)
            children.append(patrol)
            wait_until(lambda: any(e["event"] == "ready" for e in events(patrol_log)))
            supervisor = subprocess.Popen([sys.executable, "-B", str(fixture), "supervisor",
                str(path), str(supervisor_log), lease_id], stdout=output, stderr=output)
            children.append(supervisor)
            wait_until(lambda: any(e["event"] == "move" for e in events(patrol_log)))
            assert patrol.poll() is None and supervisor.poll() is None
            if loss == "kill":
                supervisor.kill()  # Actual SIGKILL: no finally / cleanup.
                supervisor.wait(timeout=3)
            else:
                os.kill(supervisor.pid, signal.SIGSTOP)  # Main cannot make progress.
            wait_until(lambda: any(e["event"] == "fault" for e in events(patrol_log)))
            assert patrol.poll() is None
            journal = events(patrol_log)
            fault = next(e for e in journal if e["event"] == "fault")
            assert fault["status"]["control_fault"] == "control lease expired"
            assert any(e["event"] == "stop" and e["fault"] == "control lease expired"
                       for e in journal)
            assert not any(e["event"] == "supervisor_cleanup" for e in events(supervisor_log))
            count = sum(e["event"] == "move" for e in journal)
            for payload in ({"operation": "heartbeat", "lease_id": lease_id},
                            {"operation": "resume"}):
                assert request(path, payload)["ok"] is False
            time.sleep(.35)
            later = events(patrol_log)
            assert all(e["fault"] is None for e in later if e["event"] == "move")
            assert sum(e["event"] == "move" for e in later) == count
            assert not any(e["event"] == "move" and e["time"] >= fault["time"] for e in later)
            assert patrol.poll() is None
        finally:
            # Kill even a SIGSTOP'ed fixture; no child can leak on assertion failure.
            for child in reversed(children):
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
