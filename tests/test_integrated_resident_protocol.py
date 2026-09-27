"""Exercise only the resident handshake with process/socket/clock mocks."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def startup(monkeypatch):
    spec = importlib.util.spec_from_file_location("integrated_protocol_test",
        Path(__file__).resolve().parents[1] / "scripts/run_integrated_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    process = SimpleNamespace(pid=123, poll=lambda: None)
    exists = iter([False, True])
    clock = iter([0., .1, 6.])
    monkeypatch.setattr(module, "DRY_RUN_SOCKET", SimpleNamespace(exists=lambda: next(exists)))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None))
    monkeypatch.setattr(module, "subprocess", SimpleNamespace(Popen=Mock(return_value=process)))
    monkeypatch.setattr(module, "stop_group", Mock())
    return module, process


@pytest.mark.parametrize("accepted", [False, True])
def test_startup_uses_bound_preflight_without_retry(startup, monkeypatch, accepted):
    module, process = startup
    status = {"accepted": True, "state": "READY", "mode": "dry-run",
              "protocol_version": 2, "session_id": "session-a"}
    request = Mock(side_effect=[status, {"accepted": accepted, "passed": accepted,
                                       "reason": "unknown operation"}])
    monkeypatch.setattr(module, "resident_request", request)
    if accepted:
        assert module.start_dry_run_resident() is process
        module.stop_group.assert_not_called()
    else:
        with pytest.raises(RuntimeError, match="bound preflight rejected"):
            module.start_dry_run_resident()
        module.stop_group.assert_called_once_with(process, "MOTIONDECODE_DRY_RUN")
    assert [c.args[0] for c in request.call_args_list] == [
        {"operation": "status"},
        {"operation": "preflight_bound", "expected_mode": "dry-run",
         "expected_session_id": "session-a"},
    ]


@pytest.mark.parametrize("field,value", [("protocol_version", None),
    ("protocol_version", 1), ("protocol_version", 2.0), ("mode", "real"),
    ("session_id", None), ("session_id", ""), ("accepted", False)])
def test_startup_does_not_preflight_unverified_status(startup, monkeypatch, field, value):
    module, process = startup
    status = {"accepted": True, "state": "READY", "mode": "dry-run",
              "protocol_version": 2, "session_id": "session-a"}
    status[field] = value
    request = Mock(return_value=status)
    monkeypatch.setattr(module, "resident_request", request)
    with pytest.raises(RuntimeError, match="did not become READY"):
        module.start_dry_run_resident()
    request.assert_called_once_with({"operation": "status"})
    module.stop_group.assert_called_once_with(process, "MOTIONDECODE_DRY_RUN")
