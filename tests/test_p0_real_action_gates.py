"""Offline gate contracts: no SSH, SDK, camera, or real resident is used."""
from __future__ import annotations

import subprocess
import socket
from unittest.mock import Mock

import pytest

from g1_bottle_reaction.adapters.motiondecode_reaction import MotionDecodeReactionAdapter
from g1_bottle_reaction.game_vision.app import build_parser
from g1_bottle_reaction.game_vision import app, dual
from g1_bottle_reaction.game_vision.wander_interlock import RemoteWanderController


class Resident:
    def __init__(self, mode):
        self.status = {"accepted": True, "state": "READY", "mode": mode,
                       "protocol_version": 2, "session_id": "test-session"}
        self.calls = []
        self.closed = False

    def request(self, payload):
        self.calls.append(payload)
        if payload["operation"] == "status":
            return dict(self.status)
        if payload["operation"] == "preflight_bound":
            return {"accepted": True, "passed": True}
        assert payload["operation"] == "execute_bound"
        return {
            "accepted": True, "reaction": payload["reaction"], "status": "pass",
            "executed": self.status["mode"] == "real", "released": True,
            "motion_completed": True, "weight_zero": True,
            "returned_to_q0": True, "hard_fault": None,
        }

    @property
    def executes(self):
        return [call for call in self.calls if call["operation"] == "execute_bound"]

    def close(self):
        self.closed = True


@pytest.mark.parametrize("bad_session", [None, "", " ", 1, True, [], {}])
def test_resident_missing_or_invalid_session_refuses_old_server(tmp_path, bad_session):
    worker = Resident("dry-run")
    worker.status["session_id"] = bad_session
    if bad_session is None:
        worker.status.pop("session_id")
    with pytest.raises(RuntimeError, match="session_id"):
        MotionDecodeReactionAdapter(tmp_path, channel_factory=lambda: worker)
    assert worker.calls == [{"operation": "status"}]
    assert worker.executes == [] and worker.closed


@pytest.mark.parametrize("real", [False, True])
@pytest.mark.parametrize("version", [None, 1, 3, "2", 2.0, True, [], {}])
def test_protocol_version_must_be_exactly_two(tmp_path, real, version):
    worker = Resident("real" if real else "dry-run")
    worker.status["protocol_version"] = version
    if version is None:
        worker.status.pop("protocol_version")
    with pytest.raises(RuntimeError, match="protocol_version 2"):
        MotionDecodeReactionAdapter(tmp_path, real=real, enabled=real,
                                    channel_factory=lambda: worker)
    assert worker.calls == [{"operation": "status"}]
    assert worker.executes == [] and worker.closed


@pytest.mark.parametrize("real", [False, True])
def test_resident_binding_is_sent_to_preflight_and_execute(tmp_path, real):
    worker = Resident("real" if real else "dry-run")
    adapter = MotionDecodeReactionAdapter(
        tmp_path, real=real, enabled=real, channel_factory=lambda: worker)
    try:
        assert adapter.preflight_motion()
        adapter.play_motion("motiondecode:found")
        bound = [c for c in worker.calls if c["operation"] != "status"]
        assert [c["operation"] for c in bound] == ["preflight_bound", "execute_bound"]
        assert all(c["expected_mode"] == worker.status["mode"] and
                   c["expected_session_id"] == "test-session" for c in bound)
    finally:
        adapter.close()


def test_session_change_at_status_latches_without_execute(tmp_path):
    worker = Resident("dry-run")
    adapter = MotionDecodeReactionAdapter(tmp_path, channel_factory=lambda: worker)
    try:
        worker.status["session_id"] = "replacement"
        with pytest.raises(RuntimeError, match="session mismatch"):
            adapter.play_motion("motiondecode:found")
        calls = list(worker.calls)
        worker.status["session_id"] = "test-session"
        assert not adapter.preflight_motion()
        with pytest.raises(RuntimeError, match="session mismatch"):
            adapter.play_motion("motiondecode:found")
        assert worker.calls == calls
        assert worker.executes == []
        assert adapter._expected_resident_session == "test-session"
    finally:
        adapter.close()


@pytest.mark.parametrize("operation", ["preflight_bound", "execute_bound"])
@pytest.mark.parametrize("reason", ["resident mode mismatch", "resident session mismatch"])
def test_binding_rejection_after_fresh_status_never_refreshes_or_retries(tmp_path, operation, reason):
    class ReplacedResident(Resident):
        def request(self, payload):
            if payload["operation"] == operation:
                self.calls.append(payload)
                assert payload["expected_session_id"] == "test-session"
                self.status["session_id"] = "replacement"
                return {"accepted": False, "state": "READY", "reason": reason}
            return super().request(payload)

    worker = ReplacedResident("dry-run")
    adapter = MotionDecodeReactionAdapter(tmp_path, channel_factory=lambda: worker)
    try:
        if operation == "preflight_bound":
            assert not adapter.preflight_motion()
        else:
            with pytest.raises(RuntimeError, match="mismatch"):
                adapter.play_motion("motiondecode:found")
        calls = list(worker.calls)
        assert [c["operation"] for c in calls] == ["status", "status", operation]
        assert not adapter.preflight_motion()
        with pytest.raises(RuntimeError, match="mismatch"):
            adapter.play_motion("motiondecode:found")
        assert worker.calls == calls
        assert adapter._expected_resident_session == "test-session"
    finally:
        adapter.close()


@pytest.mark.parametrize("real,mode", [(False, "dry-run"), (True, "real")])
def test_matching_resident_mode_allows_each_execute(tmp_path, real, mode):
    worker = Resident(mode)
    adapter = MotionDecodeReactionAdapter(
        tmp_path, real=real, enabled=real, channel_factory=lambda: worker,
    )
    try:
        for reaction in ("found", "surprise"):
            adapter.play_motion("motiondecode:" + reaction)
            assert adapter.wait_for_motion_complete("motiondecode:" + reaction)
        assert [call["operation"] for call in worker.calls] == [
            "status", "status", "execute_bound", "status", "execute_bound",
        ]
        assert len(worker.executes) == 2
    finally:
        adapter.close()


@pytest.mark.parametrize("real", [False, True])
@pytest.mark.parametrize("bad_mode", [None, "unknown", "dry", "REAL", "", True, 1])
def test_missing_or_invalid_mode_never_executes(tmp_path, real, bad_mode):
    worker = Resident(bad_mode)
    if bad_mode is None:
        worker.status.pop("mode")
    with pytest.raises(RuntimeError, match="mode mismatch"):
        MotionDecodeReactionAdapter(
            tmp_path, real=real, enabled=real, channel_factory=lambda: worker,
        )
    assert worker.executes == []
    assert worker.closed


@pytest.mark.parametrize("real,mode", [(False, "real"), (True, "dry-run")])
def test_mismatched_startup_mode_never_executes(tmp_path, real, mode):
    worker = Resident(mode)
    with pytest.raises(RuntimeError, match="mode mismatch"):
        MotionDecodeReactionAdapter(
            tmp_path, real=real, enabled=real, channel_factory=lambda: worker,
        )
    assert worker.executes == []
    assert worker.closed


@pytest.mark.parametrize("real", [False, True])
@pytest.mark.parametrize("after_success", [False, True])
def test_restart_mode_change_is_checked_before_execute_and_latched(
    tmp_path, real, after_success,
):
    mode = "real" if real else "dry-run"
    worker = Resident(mode)
    adapter = MotionDecodeReactionAdapter(
        tmp_path, real=real, enabled=real, channel_factory=lambda: worker,
    )
    try:
        if after_success:
            adapter.play_motion("motiondecode:found")
        assert adapter.preflight_motion()
        worker.calls.clear()
        worker.status["mode"] = "dry-run" if real else "real"
        with pytest.raises(RuntimeError, match="mode mismatch"):
            adapter.play_motion("motiondecode:found")
        assert worker.executes == []
        worker.status["mode"] = mode
        assert not adapter.preflight_motion()
        with pytest.raises(RuntimeError, match="mode mismatch"):
            adapter.play_motion("motiondecode:surprise")
        assert worker.executes == []
    finally:
        adapter.close()


@pytest.mark.parametrize("status", [
    {"accepted": True, "state": "READY"},
    {"accepted": True, "state": "READY", "mode": "unknown"},
    {"accepted": True, "state": "FAULT", "mode": "dry-run"},
    {"accepted": False, "state": "READY", "mode": "dry-run"},
])
def test_invalid_fresh_status_never_executes(tmp_path, status):
    worker = Resident("dry-run")
    adapter = MotionDecodeReactionAdapter(tmp_path, channel_factory=lambda: worker)
    try:
        worker.status = status
        with pytest.raises(RuntimeError):
            adapter.play_motion("motiondecode:found")
        assert worker.executes == []
    finally:
        adapter.close()


def test_failed_fresh_status_never_executes(tmp_path):
    worker = Resident("dry-run")
    adapter = MotionDecodeReactionAdapter(tmp_path, channel_factory=lambda: worker)
    def disconnected(_payload):
        raise OSError("worker restarted")
    worker.request = disconnected
    try:
        with pytest.raises(RuntimeError, match="worker restarted"):
            adapter.play_motion("motiondecode:found")
        assert worker.executes == []
    finally:
        adapter.close()


def test_preflight_mode_change_latches_before_preflight_or_execute(tmp_path):
    worker = Resident("dry-run")
    adapter = MotionDecodeReactionAdapter(tmp_path, channel_factory=lambda: worker)
    try:
        worker.calls.clear()
        worker.status["mode"] = "real"
        assert not adapter.preflight_motion()
        assert "mode mismatch" in adapter.last_preflight_error
        assert worker.calls == [{"operation": "status"}]
        with pytest.raises(RuntimeError, match="mode mismatch"):
            adapter.play_motion("motiondecode:found")
        assert worker.executes == []
    finally:
        adapter.close()


@pytest.mark.parametrize("robot,enabled,approved", [
    ("mock", False, False), ("mock", True, True),
    ("motiondecode", False, False), ("motiondecode", False, True),
    ("motiondecode", True, False), ("unknown", True, True),
    ("g1", True, 1), ("g1-ssh", 1, True),
])
def test_wander_controller_rejects_without_invoking_runner(robot, enabled, approved):
    calls = []
    with pytest.raises(ValueError, match="operator-approved-wander"):
        RemoteWanderController(
            "unitree@example", robot=robot, enable_real_robot=enabled,
            operator_approved=approved, runner=lambda *a, **k: calls.append(a),
        )
    assert calls == []


def test_wander_controller_default_has_no_remote_action():
    calls = []
    with pytest.raises(ValueError):
        RemoteWanderController("unitree@example", runner=lambda *a, **k: calls.append(a))
    assert calls == []


@pytest.mark.parametrize("robot", ["g1", "g1-ssh", "motiondecode"])
def test_wander_controller_explicit_approval_allows_mock_runner(robot):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "STARTED\n", "")
    controller = RemoteWanderController(
        "unitree@example", robot=robot, enable_real_robot=True,
        operator_approved=True, runner=runner,
    )
    assert calls == []  # Construction never starts a remote process.
    controller.start()
    assert len(calls) == 1
    assert controller.running
    assert "--execute-real-g1" in calls[0][-1]
    assert "--i-understand-this-will-move-the-robot" in calls[0][-1]


WANDER_ARGS = [
    "--source", "dual", "--yolo", "--found-audio", "--with-wander",
    "--wander-ssh-target", "unitree@example",
]
REAL_APPROVAL = [
    "--robot", "motiondecode", "--enable-real-robot", "--confirm-site-ready",
    "--operator-approved-wander",
]


@pytest.mark.parametrize("approval", [
    [], ["--operator-approved-wander"],
    ["--robot", "mock", "--enable-real-robot", "--operator-approved-wander"],
    [arg for arg in REAL_APPROVAL if arg != "--enable-real-robot"],
    [arg for arg in REAL_APPROVAL if arg != "--operator-approved-wander"],
    [arg for arg in REAL_APPROVAL if arg != "--confirm-site-ready"],
])
def test_wander_cli_rejects_before_any_subprocess(monkeypatch, approval):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(args)
        pytest.fail("rejected configuration must not reach a subprocess")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ValueError):
        dual.run(build_parser().parse_args(WANDER_ARGS + approval))
    assert calls == []


def test_complete_wander_cli_approval_is_valid():
    dual.validate_args(build_parser().parse_args(WANDER_ARGS + REAL_APPROVAL))


def test_non_wander_cli_defaults_remain_safe():
    args = build_parser().parse_args(["--source", "dual"])
    dual.validate_args(args)
    assert args.robot == "mock"
    assert not args.with_wander
    assert not args.enable_real_robot
    assert not args.operator_approved_wander


def test_non_wander_real_reaction_needs_no_wander_approval():
    args = build_parser().parse_args([
        "--source", "dual", "--yolo", "--found-audio", "--robot", "motiondecode",
        "--enable-real-robot", "--confirm-site-ready",
    ])
    dual.validate_args(args)
    assert not args.operator_approved_wander


@pytest.mark.parametrize("source_name", [
    "synthetic", "g1", "webcam", "video", "recording", "realsense", "g1-rgb",
    "usb-lan",
])
@pytest.mark.parametrize("approval", [["--robot", "mock"], REAL_APPROVAL])
def test_main_rejects_unsupported_wander_before_io(
    monkeypatch, capsys, source_name, approval,
):
    source = Mock()
    create_source = Mock(return_value=source)
    monkeypatch.setattr(app, "_create_source", create_source)
    boundaries = []
    for owner, name in [
        (app, "load_game_vision_config"), (app, "run_viewer"),
        (app, "OpenCVViewer"), (dual, "run"),
        (RemoteWanderController, "_ssh"),
        (subprocess, "run"), (subprocess, "Popen"),
        (socket, "socket"), (socket, "create_connection"),
    ]:
        boundary = Mock(side_effect=AssertionError(f"unexpected IO boundary: {name}"))
        monkeypatch.setattr(owner, name, boundary)
        boundaries.append(boundary)

    assert app.main([
        "--source", source_name, "--with-wander", *approval,
    ]) == 2
    assert "--with-wander requires --source dual" in capsys.readouterr().err
    create_source.assert_not_called()
    source.open.assert_not_called()
    for boundary in boundaries:
        boundary.assert_not_called()


@pytest.mark.parametrize("source_name", ["synthetic", "g1"])
def test_main_without_wander_keeps_existing_source_dispatch(monkeypatch, source_name):
    source = Mock()
    create_source = Mock(return_value=source)
    viewer = Mock(return_value=0)
    monkeypatch.setattr(app, "_create_source", create_source)
    monkeypatch.setattr(app, "run_viewer", viewer)
    assert app.main(["--source", source_name, "--robot", "mock"]) == 0
    create_source.assert_called_once()
    viewer.assert_called_once()
    assert create_source.call_args.args[0].source == source_name
    assert not create_source.call_args.args[0].with_wander
    assert viewer.call_args.args[0] is source


def test_main_keeps_supported_wander_approval_and_validation(monkeypatch):
    # Stop at the IO boundary, but use the production dual validator.
    def validate_only(args):
        dual.validate_args(args)
        return 0
    run = Mock(side_effect=validate_only)
    monkeypatch.setattr(dual, "run", run)
    assert app.main(WANDER_ARGS + REAL_APPROVAL) == 0
    run.assert_called_once()
    args = run.call_args.args[0]
    assert args.with_wander and args.enable_real_robot and args.operator_approved_wander
    assert args.confirm_site_ready and args.robot == "motiondecode"
