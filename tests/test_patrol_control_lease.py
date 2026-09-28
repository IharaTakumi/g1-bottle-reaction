"""Control-plane fault injection: fake locomotion, clocks and local sockets only."""
from pathlib import Path
import socket
import sys
import threading
import time
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "patrol"))
from control_ipc import PatrolControlServer, request
from control_lease import load_control_timing
from locomotion_adapter import DryRunLocomotionAdapter
from patrol_controller import PatrolConfig, PatrolController, PatrolState
from run_patrol import AlwaysClear, VirtualTime
import run_patrol


def build():
    clock = VirtualTime()
    loco = DryRunLocomotionAdapter()
    controller = PatrolController(
        loco, AlwaysClear(), clock=clock.clock, sleep=clock.sleep,
        emit=lambda _: None, lease_id="generation-one",
        lease_timeout_s=load_control_timing().lease_timeout_s,
    )
    return controller, loco, clock


def moves(loco):
    return [command for command in loco.commands if command[0] == "move"]


def arm(controller):
    assert controller.heartbeat("generation-one")["lease_active"]
    controller.resume()


def assert_faulted(controller, loco):
    count = len(moves(loco))
    assert controller.control_status()["control_fault"]
    assert any(command[0] == "stop" for command in loco.commands)
    for operation in (lambda: controller.heartbeat("generation-one"),
                      lambda: controller.heartbeat("generation-two"),
                      controller.resume,
                      lambda: controller._send_move(.3, 0),
                      lambda: controller._send_move(0, .5)):
        with pytest.raises((RuntimeError, ValueError)):
            operation()
    assert len(moves(loco)) == count


def test_initial_lease_is_paused_and_unarmed():
    controller, loco, _ = build()
    assert controller.state is PatrolState.PAUSED
    with pytest.raises(RuntimeError, match="no active"):
        controller.resume()
    assert not controller._send_move(.3, 0)
    assert not controller._send_move(0, .5)
    assert moves(loco) == []


def test_healthy_lease_preserves_forward_and_turn_commands():
    controller, loco, clock = build()
    arm(controller)
    for _ in range(10):
        clock.sleep(.1)
        controller.heartbeat("generation-one")
        assert controller._send_move(.3, 0)
        assert controller._send_move(0, .5)
    assert len(moves(loco)) == 20
    assert controller.control_status()["control_fault"] is None


def test_healthy_supervisor_allows_complete_patrol_cycle():
    controller, loco, clock = build()
    arm(controller)
    def sleep_with_heartbeat(seconds):
        remaining = seconds
        while remaining > 0:
            step = min(.1, remaining)
            clock.sleep(step)
            controller.heartbeat("generation-one")
            remaining -= step
    controller.sleep = sleep_with_heartbeat
    controller.run(cycles=1)
    assert any(vx > 0 for _, vx, _ in moves(loco))
    assert any(yaw > 0 for _, _, yaw in moves(loco))
    assert controller.control_status()["control_fault"] is None
    assert controller.control_status()["stopped"]


def test_standalone_dry_run_stays_hardware_free():
    assert run_patrol.main(["--mode", "dry-run", "--loops", "1"]) == 0


@pytest.mark.parametrize("path", ["forward", "turn", "timed", "paused"])
def test_abrupt_parent_loss_expires_inside_production_loop(path):
    # No supervisor cleanup or STOP request: only its heartbeats cease.
    controller, loco, clock = build()
    arm(controller)
    if path == "paused":
        controller.pause()
    before = clock.now
    with pytest.raises(RuntimeError, match="control lease expired"):
        if path == "forward":
            controller._run_forward(PatrolState.FORWARD_OUT, 2)
        elif path == "turn":
            controller._run_turn(PatrolState.TURN_BACK, 3.14)
        elif path == "timed":
            controller._run_timed_motion(PatrolState.FORWARD_OUT, 2, .3, 0, .3, "m")
        else:
            controller._checkpoint_reaction_pause()
    # First check after timeout, bounded by the unchanged command cadence.
    assert clock.now - before <= .30 + PatrolConfig().command_period_s
    if path != "paused":
        assert moves(loco)
    assert_faulted(controller, loco)


@pytest.mark.parametrize("first_after_expiry", ["heartbeat", "resume", "move"])
def test_expiry_cannot_be_revived_before_next_loop_tick(first_after_expiry):
    controller, loco, clock = build()
    arm(controller)
    assert controller._send_move(.3, 0)
    clock.sleep(.30)
    with pytest.raises(RuntimeError, match="expired"):
        if first_after_expiry == "heartbeat":
            controller.heartbeat("generation-one")
        elif first_after_expiry == "resume":
            controller.resume()
        else:
            controller._send_move(.3, 0)
    assert_faulted(controller, loco)


def test_wrong_generation_never_refreshes_lease():
    controller, loco, clock = build()
    arm(controller)
    clock.sleep(.20)
    for invalid in ("generation-two", None, "", 1, {}, []):
        with pytest.raises(ValueError, match="wrong"):
            controller.heartbeat(invalid)
    clock.sleep(.10)
    with pytest.raises(RuntimeError, match="expired"):
        controller._send_move(.3, 0)
    assert_faulted(controller, loco)


def test_delayed_heartbeat_handler_cannot_refresh_from_completion_time():
    controller, loco, clock = build()
    arm(controller)
    clock.sleep(.2)
    check = controller.check_control_lease
    def delayed_check():
        result = check()
        clock.sleep(.4)  # Simulate descheduling after the validation read.
        return result
    controller.check_control_lease = delayed_check
    assert not controller.heartbeat("generation-one")["lease_active"]
    controller.check_control_lease = check
    with pytest.raises(RuntimeError, match="expired"):
        controller._send_move(.3, 0)
    assert_faulted(controller, loco)


def test_resume_rechecks_lease_after_telemetry_barrier():
    controller, loco, clock = build()
    controller.heartbeat("generation-one")
    controller._wait_for_fresh_telemetry = lambda _: clock.sleep(.31)
    with pytest.raises(RuntimeError, match="expired"):
        controller.resume()
    assert moves(loco) == []
    assert_faulted(controller, loco)


def test_stop_error_does_not_remove_fault_latch():
    controller, loco, clock = build()
    arm(controller)
    loco.stop = Mock(side_effect=OSError("STOP delivery failed"))
    clock.sleep(.31)
    with pytest.raises(OSError):
        controller.check_control_lease()
    with pytest.raises(RuntimeError, match="expired"):
        controller.heartbeat("generation-one")
    with pytest.raises(RuntimeError):
        controller._send_move(.3, 0)
    assert moves(loco) == []


def test_ipc_fault_serializes_with_inflight_move():
    controller, loco, _ = build()
    arm(controller)
    entered, release = threading.Event(), threading.Event()
    original_move = loco.move
    def move(vx, yaw):
        entered.set()
        assert release.wait(3)
        original_move(vx, yaw)
    loco.move = move
    sender = threading.Thread(target=lambda: controller._send_move(.3, 0))
    fault = threading.Thread(target=lambda: controller.control_fault("IPC lost"))
    sender.start()
    try:
        assert entered.wait(3)
        fault.start()
    finally:
        release.set()
        sender.join(3)
        fault.join(3)
    assert not sender.is_alive() and not fault.is_alive()
    assert [command[0] for command in loco.commands] == ["move", "stop"]
    assert_faulted(controller, loco)


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.005)
    assert predicate()


@pytest.mark.parametrize("failure", ["exception", "return"])
def test_ipc_listener_unexpected_death_latches_stop(tmp_path, failure):
    controller, loco, _ = build()
    arm(controller)
    controller._send_move(.3, 0)
    server = PatrolControlServer(tmp_path / "control.sock", controller)
    def die():
        if failure == "exception":
            raise OSError("accept failed")
    server._accept_clients = die
    server.start()
    try:
        server._thread.join(2)
        assert not server._thread.is_alive()
        assert_faulted(controller, loco)
    finally:
        server.close()


def test_partial_socket_deadline_and_independent_lease_expiry(tmp_path):
    controller, loco, clock = build()
    server = PatrolControlServer(tmp_path / "control.sock", controller)
    server.start()
    try:
        assert request(server.path, {"operation": "heartbeat", "lease_id": "generation-one"})["ok"]
        assert request(server.path, {"operation": "resume"})["ok"]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(server.path))
            client.sendall(b'{"operation":')  # No newline; remains open.
            with pytest.raises(RuntimeError, match="expired"):
                controller._run_forward(PatrolState.FORWARD_OUT, 2)
            assert moves(loco)
            assert_faulted(controller, loco)
            # Absolute read deadline rejects just this client and frees its slot.
            assert b'"ok":false' in client.recv(4096)
        assert request(server.path, {"operation": "status"})["ok"]
        assert server._thread.is_alive()
    finally:
        server.close()


def test_broken_pipe_is_contained_and_next_client_works(tmp_path):
    controller, loco, _ = build()
    server = PatrolControlServer(tmp_path / "control.sock", controller)
    entered, release = threading.Event(), threading.Event()
    finished = threading.Event()
    original_client = server._client
    def client(connection):
        try:
            original_client(connection)
        finally:
            finished.set()
    server._client = client
    original = server._dispatch
    def dispatch(payload):
        if payload.get("operation") == "disconnect-test":
            entered.set()
            assert release.wait(3)
            return {"ok": True}
        return original(payload)
    server._dispatch = dispatch
    server.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(server.path))
            client.sendall(b'{"operation":"disconnect-test"}\n')
            assert entered.wait(3)
        release.set()  # sendall to a closed actual Unix socket -> BrokenPipe.
        assert finished.wait(3)
        assert request(server.path, {"operation": "status"})["ok"]
        assert server._thread.is_alive()
        assert controller.control_status()["control_fault"] is None
        assert moves(loco) == []
    finally:
        release.set()
        server.close()


def test_waiting_reaction_request_does_not_starve_heartbeat(tmp_path):
    controller, _, clock = build()
    arm(controller)
    controller._recovering_telemetry.set()  # Existing pause waits for recovery.
    server = PatrolControlServer(tmp_path / "control.sock", controller)
    server.start()
    result = []
    worker = threading.Thread(target=lambda: result.append(
        request(server.path, {"operation": "pause", "timeout": 2})))
    try:
        worker.start()
        wait_until(controller._pause_requested.is_set)
        for _ in range(8):
            clock.sleep(.1)
            assert request(server.path, {"operation": "heartbeat", "lease_id": "generation-one"})["ok"]
        assert not result
        controller._recovering_telemetry.clear()
        controller._activate_pause("reaction")
        worker.join(3)
        assert result[0]["ok"]
        assert controller.control_status()["control_fault"] is None
    finally:
        server.close()
        worker.join(3)


def test_unexpected_client_failure_propagates_fault(tmp_path):
    controller, loco, _ = build()
    arm(controller)
    server = PatrolControlServer(tmp_path / "control.sock", controller)
    server._dispatch = Mock(side_effect=AssertionError("dispatch invariant broken"))
    server.start()
    try:
        with pytest.raises(ValueError):
            request(server.path, {"operation": "status"})
        wait_until(lambda: controller.control_status()["control_fault"] is not None)
        assert_faulted(controller, loco)
    finally:
        server.close()


@pytest.mark.parametrize("payload", [[], {}, {"operation": "unknown"},
    {"operation": "heartbeat"}, {"operation": "pause", "timeout": float("nan")},
    {"operation": "pause", "timeout": None}])
def test_bad_requests_are_rejected_without_server_death(tmp_path, payload):
    controller, loco, _ = build()
    server = PatrolControlServer(tmp_path / "control.sock", controller)
    server.start()
    try:
        assert request(server.path, payload)["ok"] is False
        assert request(server.path, {"operation": "status"})["ok"]
        assert controller.control_status()["control_fault"] is None
        assert moves(loco) == []
    finally:
        server.close()


@pytest.mark.parametrize("args", [
    ["--require-control-lease"],
    ["--mode", "real", "--require-control-lease", "--control-socket", "/unused"],
    ["--mode", "real", "--control-lease-id", "orphan"],
])
def test_bad_lease_cli_rejected_before_io(monkeypatch, args):
    observe = Mock(side_effect=AssertionError("hardware prohibited"))
    monkeypatch.setattr(run_patrol, "observe_lidar", observe)
    with pytest.raises(ValueError):
        run_patrol.main(args)
    observe.assert_not_called()


def test_required_lease_is_wired_by_real_entrypoint_with_fake_io(monkeypatch):
    loco = DryRunLocomotionAdapter()
    captured = []
    monkeypatch.setattr(run_patrol, "observe_lidar", lambda _: (
        AlwaysClear(), Mock(), {"front_ready": True}))
    monkeypatch.setattr(run_patrol, "writer_conflicts", lambda: [])
    monkeypatch.setattr(run_patrol, "UdpLocomotionAdapter", lambda *args: loco)
    monkeypatch.setattr(run_patrol, "PatrolControlServer", Mock())
    def run(controller, cycles):
        captured.append(controller)
        assert controller.control_status()["paused"]
        with pytest.raises(RuntimeError, match="no active"):
            controller.resume()
        assert not controller._send_move(.3, 0)
    monkeypatch.setattr(run_patrol.PatrolController, "run", run)
    assert run_patrol.main([
        "--mode", "real", "--arm", "--one-cycle", "--operator-approved-one-cycle",
        "--require-control-lease", "--control-lease-id", "generation-one",
        "--control-socket", "/unused",
    ]) == 0
    assert len(captured) == 1
    assert moves(loco) == []


def test_timing_keeps_existing_command_and_relay_budget():
    timing = load_control_timing()
    assert timing.heartbeat_interval_s == PatrolConfig().command_period_s == .10
    assert timing.lease_timeout_s == .30
    assert timing.lease_timeout_s + PatrolConfig().command_period_s <= .40
    assert timing.ipc_read_timeout_s < timing.lease_timeout_s
