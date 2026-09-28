"""Run the real supervisor main with fake processes and local controller only."""
import importlib.util
from pathlib import Path
import signal
import uuid
from unittest.mock import Mock

import pytest

from test_patrol_control_lease import build, moves


def load_supervisor():
    spec = importlib.util.spec_from_file_location(
        "integrated_lease_test", Path(__file__).resolve().parents[1] / "scripts/run_integrated_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("ending", ["sigterm", "sigint", "heartbeat_error", "normal"])
def test_main_owns_heartbeat_and_runs_stop_cleanup(monkeypatch, ending):
    module = load_supervisor()
    controller, loco, clock = build()
    processes, commands, calls = [], [], []
    handlers, restored = {}, []
    active_at = []
    def install(sig, handler):
        if callable(handler):
            handlers[sig] = handler
        else:
            restored.append(sig)
        return signal.SIG_DFL
    monkeypatch.setattr(module.signal, "signal", install)
    monkeypatch.setattr(module, "preflight", lambda _: None)
    monkeypatch.setattr(module.time, "monotonic", clock.clock)
    triggered = False
    def sleep(seconds):
        nonlocal triggered
        # Record fake commands via the production send gate after release.
        if not controller.control_status()["paused"]:
            active_at.append(clock.now)
            controller._send_move(.3, 0)
            if not triggered and ending.startswith("sig"):
                triggered = True
                handlers[signal.SIGTERM if ending == "sigterm" else signal.SIGINT](0, None)
        clock.sleep(seconds)
    monkeypatch.setattr(module.time, "sleep", sleep)
    def popen(command, **kwargs):
        commands.append(command)
        process = Mock(pid=len(processes) + 1)
        process.poll.side_effect = lambda: (
            0 if ending == "normal" and active_at else None)
        processes.append(process)
        if len(processes) == 1:
            assert "--require-control-lease" in command
            lease_id = command[command.index("--control-lease-id") + 1]
            assert uuid.UUID(lease_id).version == 4
            controller._lease_id = lease_id  # Fake child receives launch argument.
        return process
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module, "PATROL_CONTROL_SOCKET", Mock(exists=lambda: True))
    def request(body, timeout=5):
        calls.append(body)
        if body["operation"] == "heartbeat":
            if ending == "heartbeat_error" and active_at:
                raise RuntimeError("heartbeat transport failed")
            status = controller.heartbeat(body["lease_id"])
        elif body["operation"] == "resume":
            assert controller.control_status()["lease_active"]
            controller.resume()
            status = controller.control_status()
        else:
            raise AssertionError(body)
        return {"ok": True, **status}
    monkeypatch.setattr(module, "patrol_request", request)
    cleanup = []
    def stop(process, name):
        cleanup.append(name)
        if name == "PATROL":
            controller.stop()
    monkeypatch.setattr(module, "stop_group", stop)
    if ending == "heartbeat_error":
        with pytest.raises(RuntimeError, match="heartbeat transport failed"):
            module.main(["--real-patrol", "--operator-approved-real-patrol"])
    else:
        result = module.main(["--real-patrol", "--operator-approved-real-patrol"])
        assert result == (130 if ending.startswith("sig") else 0)
    assert cleanup == ["PATROL", "REACTION", "MOTIONDECODE_DRY_RUN"]
    assert restored == [signal.SIGINT, signal.SIGTERM]
    assert calls[0]["operation"] == "heartbeat"
    assert sum(call["operation"] == "heartbeat" for call in calls) >= 20
    assert moves(loco)
    count = len(moves(loco))
    assert not controller._send_move(.3, 0)
    assert len(moves(loco)) == count
    assert controller.control_status()["stopped"]


def test_launcher_cannot_construct_unleased_patrol_command():
    module = load_supervisor()
    with pytest.raises(TypeError):
        module.patrol_command()
    with pytest.raises(ValueError):
        module.patrol_command("")


@pytest.mark.parametrize("status", [
    {}, {"lease_active": False}, {"lease_active": True, "error": "fault"},
    {"lease_active": True, "stopped": True},
])
def test_supervisor_rejects_inactive_or_faulted_heartbeat(monkeypatch, status):
    module = load_supervisor()
    monkeypatch.setattr(module, "patrol_request", Mock(return_value=status))
    with pytest.raises(RuntimeError, match="not active"):
        module.refresh_patrol_lease("generation-one", .1)
