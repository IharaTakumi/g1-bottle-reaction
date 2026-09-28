"""Calibration production runner with local UDP v4 relay + fake SDK/telemetry only."""
import base64
import json
import math
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from test_stop_transactions import rpc_relay
from scripts import run_stationary_calibration as harness
from scripts.analyze_stationary_calibration import summarize, validate


def args(tmp_path, mode="standing"):
    return harness.parser().parse_args([
        "--mode", mode, "--execute", "--output", str(tmp_path),
        "--session-id", "test", "--trial-id", "trial1",
        "--telemetry-bind", "127.0.0.1", "--telemetry-peer", "127.0.0.1",
        "--telemetry-port", "47623", "--pre-seconds", ".02", "--post-seconds", ".02",
        "--trial-timeout", "10", *([] if mode == "standing" else [
            "--relay-host", "127.0.0.1", "--relay-port", "47622",
            "--lidar-bind", "127.0.0.1", "--lidar-port", "47621",
            "--enable-real-robot", "--confirm-site-ready", "--operator-approved-calibration",
            "--confirm-external-recording", "--external-video", "fake-video.mp4"])])


class FakeTelemetry:
    def __init__(self, bind, port, peer, recorder, relay=None, mode="standing"):
        self.recorder, self.relay, self.mode = recorder, relay, mode
        self.x = self.yaw = 0.
        self.tick = 0
        self.sample()

    def sample(self):
        self.tick += 1
        owner = None if self.relay is None else self.relay.owner
        if owner is not None and owner.state == "MOVEMENT_ENABLED" and self.relay.sdk.moves:
            if self.mode == "forward-stop": self.x += .2
            else: self.yaw = math.atan2(math.sin(self.yaw+.2), math.cos(self.yaw+.2))
        message = dict(odom_x=self.x, odom_y=0., odom_yaw=self.yaw, yaw=self.yaw,
            odom_stamp_ns=10**18+self.tick, imu_tick=self.tick, imu_gyro=[0., 0., 0.],
            odom_ready=True, imu_ready=True, odom_age=0., imu_age=0., transport_age=0.,
            relay_epoch=None if owner is None else owner.epoch,
            owner_session=None if owner is None else owner.session,
            movement_generation=None if owner is None else owner.generation,
            stop_request_id=None if owner is None or not owner.stop_transaction else owner.stop_transaction["stop_request_id"],
            stop_rpc_status=None if owner is None else owner.stop_rpc_status)
        raw = {dest: message.get(source) for dest, source in harness.Telemetry.ingest.__globals__["RAW_FIELDS"].items()}
        self.recorder.emit(self.recorder.record("sample", identity=message, **raw,
            lowstate_gyro_raw=message["imu_gyro"], lowstate_tick_unwrapped=None,
            odom_stamp_unit_status="UNVERIFIED", odom_clock_mapping_status="UNVERIFIED", lowstate_tick_status="UNVERIFIED"))
        return message

    def close(self): pass


class ClearGuard:
    def __init__(self, *args): pass
    def state(self, direction): return harness.GuardState.CLEAR
    def close(self): pass


def read_records(tmp_path):
    return [json.loads(line) for line in (tmp_path/"test/trial1/canonical.jsonl").read_text().splitlines()]


def run_motion(tmp_path, monkeypatch, relay, mode="forward-stop", *, args_changes=None, **factories):
    monkeypatch.setenv("G1_ALLOW_REAL_ACTION", "1")  # Local fake SDK only, never real execution.
    a = args(tmp_path, mode); a.relay_port = relay.address[1]
    for key, value in (args_changes or {}).items(): setattr(a, key, value)
    factories.setdefault("telemetry_factory", lambda *a: FakeTelemetry(*a, relay=relay, mode=mode))
    factories.setdefault("guard_factory", ClearGuard)
    return harness.run(a, **factories)


def test_standing_never_constructs_command_adapter_or_controller(tmp_path):
    forbidden = Mock(side_effect=AssertionError("command dependency used"))
    assert harness.run(args(tmp_path), telemetry_factory=FakeTelemetry,
        adapter_factory=forbidden, guard_factory=forbidden, controller_factory=forbidden) == 0
    forbidden.assert_not_called()
    rows = read_records(tmp_path)
    assert any(r["record_type"] == "sample" for r in rows)
    assert not any(r.get("event", "").startswith("STOP_") for r in rows)
    assert summarize(rows)["production_authorization"] is False


@pytest.mark.parametrize("mode", ["forward-stop", "turn-stop"])
def test_real_controller_single_leg_v4_stop_and_records(tmp_path, monkeypatch, rpc_relay, mode):
    relay, sdk = rpc_relay
    # The telemetry fake reads the SDK used by the production protocol dispatcher.
    relay.sdk = sdk
    assert run_motion(tmp_path, monkeypatch, relay, mode) == 0
    assert sdk.moves and all(m[0] >= 0 for m in sdk.moves)
    assert len(sdk.calls) == 3  # Initial, primary, then controlled cleanup AFTER observation.
    assert relay.owner.state == "MOVEMENT_HELD"
    assert relay.owner.stop_rpc_status == "STOP_RPC_CONFIRMED"
    rows = read_records(tmp_path)
    for row in rows: validate(row)
    events = [r.get("event") for r in rows]
    assert events.index("MOVEMENT_ENABLE") < events.index("MOVING")
    assert events.index("POST_STOP_OBSERVATION") < events.index("TRIAL_END")
    confirmed = [r for r in rows if r.get("event") == "STOP_RPC_CONFIRMED"]
    assert len(confirmed) == 3
    assert confirmed[-1]["transport"]["command_scope"] == "cleanup"
    assert confirmed[-1]["stop_request_id"] == confirmed[-1]["movement_generation"]
    report = summarize(rows)
    assert report["trials"][0]["stop_confirm_marker_count"] == 1
    assert all(r["odom_clock_mapping_status"] == "UNVERIFIED" for r in rows if r["record_type"] == "sample")
    count = len(sdk.moves); time.sleep(.05)
    assert len(sdk.moves) == count


def test_open_failure_precedes_every_resource(tmp_path):
    opened = Mock(side_effect=AssertionError("resource opened"))
    def fail(*a): raise OSError("disk unavailable")
    with pytest.raises(OSError):
        harness.run(args(tmp_path), recorder_factory=fail, telemetry_factory=opened, adapter_factory=opened)
    opened.assert_not_called()


def test_recorder_runtime_failure_blocks_future_moves_and_attempts_stop(tmp_path, monkeypatch, rpc_relay):
    relay, sdk = rpc_relay; relay.sdk = sdk
    class FailingRecorder(harness.Recorder):
        def _write(self, record):
            if record.get("event") == "MOVING": raise OSError("injected disk loss")
            super()._write(record)
    with pytest.raises(RuntimeError):
        run_motion(tmp_path, monkeypatch, relay, recorder_factory=FailingRecorder)
    assert relay.owner.state == "MOVEMENT_HELD"
    assert len(sdk.calls) >= 2
    count = len(sdk.moves); time.sleep(.15)
    assert len(sdk.moves) == count


def test_stop_unconfirmed_is_terminal_no_retry(tmp_path, monkeypatch, rpc_relay):
    relay, sdk = rpc_relay; relay.sdk = sdk
    sdk.result = 3104
    with pytest.raises(RuntimeError): run_motion(tmp_path, monkeypatch, relay)
    assert sdk.moves == [] and len(sdk.calls) == 1
    assert any(r.get("event") == "STOP_UNCONFIRMED" for r in read_records(tmp_path))


def test_ownership_unavailable_no_sdk_or_move(tmp_path, monkeypatch, rpc_relay):
    relay, sdk = rpc_relay; relay.sdk = sdk
    other = harness.RelaySession(*relay.address)
    try:
        with pytest.raises(RuntimeError): run_motion(tmp_path, monkeypatch, relay)
        assert sdk.moves == sdk.calls == []
    finally:
        other.close()


def test_lease_unavailable_no_movement(tmp_path, monkeypatch, rpc_relay):
    relay, sdk = rpc_relay; relay.sdk = sdk
    class NoLease(harness.PatrolController):
        def heartbeat(self, lease_id): raise RuntimeError("lease unavailable")
    with pytest.raises(RuntimeError):
        run_motion(tmp_path, monkeypatch, relay, controller_factory=NoLease)
    assert sdk.moves == []


def test_existing_lease_expires_when_supervisor_stops_refreshing(tmp_path, rpc_relay):
    relay, sdk = rpc_relay; relay.sdk = sdk
    recorder = harness.Recorder(tmp_path/"lease", "test", "lease", {"pc_clock_id": "test"})
    telemetry = FakeTelemetry(None, None, None, recorder, relay=relay, mode="forward-stop")
    adapter = harness.CalibrationAdapter(*relay.address, telemetry, recorder)
    controller = harness.PatrolController(adapter, ClearGuard(), lease_id="lease-test",
        lease_timeout_s=harness.load_control_timing().lease_timeout_s, emit=lambda _: None)
    errors = []
    def run():
        try: controller.run_calibration_leg("forward-stop")
        except BaseException as exc: errors.append(exc)
    try:
        controller.heartbeat("lease-test")
        worker = threading.Thread(target=run)
        worker.start(); worker.join(3)
        assert not worker.is_alive()
        assert errors and "lease" in str(errors[0])
        assert sdk.moves and relay.owner.state == "MOVEMENT_HELD"
        count = len(sdk.moves); time.sleep(.1)
        assert len(sdk.moves) == count
        with pytest.raises(RuntimeError): controller.resume()
    finally:
        adapter.close(); recorder.close()


def test_terminal_stop_failure_after_movement_is_not_retried(tmp_path, monkeypatch, rpc_relay):
    relay, sdk = rpc_relay; relay.sdk = sdk
    class TerminalFailure(harness.CalibrationAdapter):
        def move(self, vx, vyaw=0):
            result = super().move(vx, vyaw)
            sdk.result = 3104
            return result
    with pytest.raises(RuntimeError):
        run_motion(tmp_path, monkeypatch, relay, adapter_factory=TerminalFailure)
    assert sdk.moves and len(sdk.calls) == 2
    events = [r.get("event") for r in read_records(tmp_path)]
    assert events.count("STOP_RPC_CONFIRMED") == 1  # Only the initialization STOP.
    assert "STOP_UNCONFIRMED" in events and "TRIAL_END" not in events


def test_recorder_bounded_queue_and_flush_stall_fail_closed(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class StalledRecorder(harness.Recorder):
        def _write(self, record):
            if record.get("event") == "STALL":
                entered.set(); release.wait(2)
            super()._write(record)
    recorder = StalledRecorder(tmp_path/"bounded", "test", "bounded", {"pc_clock_id": "test"}, capacity=2)
    try:
        recorder.event("STALL"); assert entered.wait(1)
        recorder.event("FILL1"); recorder.event("FILL2"); recorder.event("OVERFLOW")
        with pytest.raises(RuntimeError): recorder.check()
        assert recorder.queue.qsize() <= 2
    finally:
        release.set()
        try: recorder.close()
        except RuntimeError: pass
    assert not recorder.thread.is_alive()


@pytest.mark.parametrize("change", [dict(loops=0), dict(loops=2), dict(reverse=True),
    dict(execute=False), dict(operator_approved_calibration=False), dict(enable_real_robot=False),
    dict(confirm_external_recording=False)])
def test_rejected_arguments_before_io(tmp_path, monkeypatch, change):
    monkeypatch.setenv("G1_ALLOW_REAL_ACTION", "1")
    a = args(tmp_path, "forward-stop")
    for k, v in change.items(): setattr(a, k, v)
    factory = Mock(side_effect=AssertionError("opened"))
    with pytest.raises(ValueError): harness.run(a, recorder_factory=factory)
    factory.assert_not_called()


def test_missing_environment_gate_before_io(tmp_path, monkeypatch):
    monkeypatch.delenv("G1_ALLOW_REAL_ACTION", raising=False)
    with pytest.raises(ValueError): harness.run(args(tmp_path, "forward-stop"))
    assert not (tmp_path/"test").exists()


def test_passive_receiver_exact_raw_bad_packets_marker_and_queue_failure(tmp_path):
    recorder = harness.Recorder(tmp_path/"raw", "test", "raw", {"pc_clock_id": "test"})
    telemetry = harness.Telemetry("127.0.0.1", 0, "127.0.0.1", recorder)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = b'{"odom_stamp_ns":1000000000000000001,"imu_tick":4294967295,"imu_gyro":[1,2,3]}'
    try:
        sender.sendto(payload, telemetry.sock.getsockname())
        sender.sendto(b'not JSON', telemetry.sock.getsockname())
        deadline = time.monotonic()+1
        while telemetry.count < 2 and time.monotonic() < deadline: time.sleep(.01)
        assert telemetry.count == 2
        recorder.operator_marker("video frame check")
        recorder.barrier(1)
    finally:
        sender.close(); telemetry.close(); recorder.close()
    rows = [json.loads(l) for l in (tmp_path/"raw/canonical.jsonl").read_text().splitlines()]
    sample = next(r for r in rows if r["record_type"] == "sample")
    assert sample["odom_stamp_raw"] == 1000000000000000001
    assert sample["lowstate_tick_raw"] == 4294967295 and sample["lowstate_tick_unwrapped"] is None
    assert base64.b64decode(sample["transport"]["raw_payload_b64"]) == payload
    assert any(r.get("event") == "TELEMETRY_INVALID" for r in rows)
    assert any(r.get("event") == "OPERATOR_EXTERNAL_STATIONARY_MARK" for r in rows)


def test_cli_subprocess_standing_no_robot_dependencies(tmp_path):
    a = args(tmp_path)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]; probe.close()
    argv = ["--mode", "standing", "--execute", "--output", str(tmp_path), "--session-id", "test",
            "--trial-id", "trial1", "--telemetry-bind", "127.0.0.1", "--telemetry-peer", "127.0.0.1",
            "--telemetry-port", str(port), "--pre-seconds", ".3", "--post-seconds", ".1", "--trial-timeout", "5"]
    process = subprocess.Popen([sys.executable, "-B", str(Path(harness.__file__)), *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    payload = json.dumps(dict(odom_x=0, odom_y=0, odom_yaw=0, odom_stamp_ns=1,
                             yaw=0, imu_tick=1, imu_gyro=[0,0,0])).encode()
    try:
        deadline = time.monotonic()+5
        while process.poll() is None and time.monotonic() < deadline:
            sender.sendto(payload, ("127.0.0.1", port)); time.sleep(.02)
        stdout, stderr = process.communicate(timeout=2)
        assert process.returncode == 0, stderr.decode()
        assert b"REACTION=OFF" in stdout
    finally:
        sender.close()
        if process.poll() is None: process.kill(); process.wait()
    assert summarize(read_records(tmp_path))["measurement_only"]


def test_runner_has_no_reaction_or_sdk_imports():
    import ast
    tree = ast.parse(Path(harness.__file__).read_text())
    imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any(any(word in name.lower() for word in ("reaction", "vision", "motiondecode", "unitree", "wander", "audio")) for name in imports)
