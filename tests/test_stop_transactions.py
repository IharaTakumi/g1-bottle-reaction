"""Actual local UDP/IPC and production controllers; SDK/audio/telemetry are fake."""
import ast
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import types
import uuid
from unittest.mock import Mock

import pytest

from test_locomotion_ownership import LocalRelay, Sender
from test_patrol_reaction_interlock import build, wait_idle
from locomotion_session import RelaySession
import locomotion_session
from locomotion_adapter import UdpLocomotionAdapter
from locomotion_protocol import RelayOwnership
from patrol_controller import PatrolController
from control_ipc import PatrolControlServer
from run_patrol import AlwaysClear
from stop_rpc import ResultPreservingClient
from g1_bottle_reaction.game_vision.patrol_interlock import LocalPatrolController

ROOT = Path(__file__).resolve().parents[1]


class FakeRpc:
    def __init__(self):
        self.result = 0
        self.calls = []
        self.moves = []
        self.release = None
        self.entered = threading.Event()

    def SetVelocity(self, *args):
        self.calls.append(args)
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(3)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def Move(self, *args, **kwargs):
        self.moves.append(args)


@pytest.fixture
def rpc_relay():
    relay = LocalRelay()
    sdk = FakeRpc()
    relay.owner.client = ResultPreservingClient(sdk, 0)
    try:
        yield relay, sdk
    finally:
        if sdk.release is not None:
            sdk.release.set()
        relay.close()


class LossySocket:
    """Drop actual UDP datagrams at the server boundary, without changing dispatch."""
    def __init__(self, sock, mode):
        self.sock, self.mode = sock, mode

    def __getattr__(self, key):
        return getattr(self.sock, key)

    def recvfrom(self, size):
        data, peer = self.sock.recvfrom(size)
        if self.mode == "request_lost" and json.loads(data).get("operation") == "hold":
            raise socket.timeout()
        return data, peer

    def sendto(self, data, peer):
        if json.loads(data).get("operation") == "hold":
            if self.mode == "response_lost":
                return len(data)
            if self.mode == "response_delayed":
                time.sleep(.2)
        return self.sock.sendto(data, peer)


def replace_loop_state(relay, **changes):
    # Finish any in-flight recv step before replacing its socket/owner reference.
    # Otherwise the first fault-injection packet can reach the old local fixture.
    relay.closed.set()
    relay.thread.join(2)
    assert not relay.thread.is_alive() and not relay.errors
    for key, value in changes.items():
        setattr(relay, key, value)
    relay.closed.clear()
    relay.thread = threading.Thread(target=relay.run)
    relay.thread.start()


def configure_failure(relay, sdk, mode):
    if mode in {"request_lost", "response_lost", "response_delayed"}:
        replace_loop_state(relay, socket=LossySocket(relay.socket, mode))
    elif mode == "rpc_error":
        sdk.result = 3104
    elif mode == "rpc_exception":
        sdk.result = RuntimeError("fake RPC exception")
    elif mode == "rpc_block":
        sdk.release = threading.Event()


def assert_latched(session):
    assert session.stop_rpc_status == "STOP_UNCONFIRMED"
    assert session.failed and not session.enabled
    for call in (session.enable, lambda: session.move(.1, 0), session.hold):
        with pytest.raises(RuntimeError):
            call()


def test_public_stop_boundary_exact_request_and_raw_result():
    sdk = Mock()
    sdk.SetVelocity.return_value = 3104
    assert ResultPreservingClient(sdk, 0).StopMove() == 3104
    sdk.SetVelocity.assert_called_once_with(0., 0., 0.)
    sdk.StopMove.assert_not_called()
    with pytest.raises(RuntimeError):
        ResultPreservingClient(sdk, None)


def test_verified_sdk_methods_make_identical_zero_velocity_rpc():
    root = Path(os.environ.get("G1_SDK_SOURCE", ROOT.parent / "unitree_sdk2_python"))
    if not root.is_dir():
        pytest.skip("set G1_SDK_SOURCE for read-only local SDK source verification")
    source = ast.parse((root / "unitree_sdk2py/g1/loco/g1_loco_client.py").read_text())
    methods = [node for node in ast.walk(source) if isinstance(node, ast.FunctionDef)
               and node.name in {"StopMove", "SetVelocity"}]
    def constant(path, name):
        tree = ast.parse((root / path).read_text())
        return next(ast.literal_eval(node.value) for node in tree.body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == name for t in node.targets))
    api = constant("unitree_sdk2py/g1/loco/g1_loco_api.py", "ROBOT_API_ID_LOCO_SET_VELOCITY")
    ok = constant("unitree_sdk2py/rpc/internal.py", "RPC_OK")
    namespace = {"json": json, "ROBOT_API_ID_LOCO_SET_VELOCITY": api}
    # Execute only these pure method bodies with a fake _Call; never SDK imports/init.
    exec(compile(ast.Module(body=methods, type_ignores=[]), "verified_methods", "exec"), namespace)
    sdk = types.SimpleNamespace(_Call=Mock(return_value=(ok, "unused")))
    sdk.SetVelocity = types.MethodType(namespace["SetVelocity"], sdk)
    assert namespace["StopMove"](sdk) is None
    old_request = sdk._Call.call_args
    assert ResultPreservingClient(sdk, ok).StopMove() == ok
    assert sdk._Call.call_args == old_request
    assert json.loads(old_request.args[1]) == {"velocity": [0., 0., 0.], "duration": 1.0}


def test_response_processed_after_absolute_deadline_is_unconfirmed(rpc_relay, monkeypatch):
    relay, sdk = rpc_relay
    session = RelaySession(*relay.address)
    loads = json.loads
    def delayed_decode(data):
        value = loads(data)
        if isinstance(value, dict) and value.get("operation") == "hold" and "accepted" in value:
            time.sleep(.15)
        return value
    monkeypatch.setattr(locomotion_session.json, "loads", delayed_decode)
    try:
        with pytest.raises(TimeoutError):
            session.hold()
        assert_latched(session)
    finally:
        session.close()


def test_confirmed_hold_is_correlated_and_required_before_enable(rpc_relay):
    relay, sdk = rpc_relay
    session = RelaySession(*relay.address)
    try:
        with pytest.raises(RuntimeError, match="confirmed STOP"):
            session.enable()
        session.hold()
        response = session.stop_transaction
        assert response["protocol_version"] == 4
        assert response["stop_request_id"] == response["movement_generation"]
        assert response["relay_epoch"] == session.epoch
        assert response["owner_session"] == session.session
        assert response["sequence"] == session.sequence
        assert response["relay_state"] == "MOVEMENT_HELD"
        assert response["raw_rpc_code"] == 0
        assert session.stop_rpc_status == response["stop_rpc_status"] == "STOP_RPC_CONFIRMED"
        assert sdk.calls == [(0., 0., 0.)]
        session.enable()
        session.move(.1, 0)
        assert len(sdk.moves) == 1
    finally:
        session.close()


@pytest.mark.parametrize("result", [3104, None, False, "0", 0.0, {}, RuntimeError("fake")])
def test_non_success_latches_fault_without_stop_retry(rpc_relay, result):
    relay, sdk = rpc_relay
    sdk.result = result
    session = RelaySession(*relay.address)
    try:
        with pytest.raises(RuntimeError):
            session.hold()
        assert_latched(session)
        assert relay.owner.state == "FAULT"
        assert relay.owner.stop_rpc_status == "STOP_UNCONFIRMED"
        relay.owner.fault("cleanup must not retry")
        assert sdk.calls == [(0., 0., 0.)]
        assert sdk.moves == []
    finally:
        session.close()


@pytest.mark.parametrize("mode", ["request_lost", "response_lost", "response_delayed", "rpc_block"])
def test_udp_loss_and_block_never_accept_late_success(rpc_relay, mode):
    relay, sdk = rpc_relay
    session = RelaySession(*relay.address)
    configure_failure(relay, sdk, mode)
    old_generation = relay.owner.generation
    try:
        with pytest.raises(TimeoutError):
            session.hold()
        assert_latched(session)
        if mode != "request_lost":
            assert relay.owner.generation != old_generation
            assert relay.owner.state == "MOVEMENT_HELD"
            assert sdk.calls == [(0., 0., 0.)]
        else:
            assert sdk.calls == []
        if mode == "rpc_block":
            assert relay.owner.stop_rpc_status == "STOP_RELAY_RECEIVED"
            sdk.release.set()
        time.sleep(.22)
        assert_latched(session)  # Late datagram cannot update state without a new exchange.
    finally:
        session.close()


@pytest.mark.parametrize("field,value", [
    ("request_id", "other"), ("relay_epoch", str(uuid.uuid4())),
    ("owner_session", str(uuid.uuid4())), ("sequence", 999), ("sequence", True),
    ("movement_generation", str(uuid.uuid4())), ("protocol_version", 2),
    ("operation", "move"), ("relay_state", "FAULT"), ("raw_rpc_code", None),
    ("stop_rpc_status", "UNKNOWN"), ("state", "FAULT"),
    ("stop_rpc_status", None), ("raw_rpc_code", "0"), ("payload", []),
])
def test_wrong_response_correlation_is_unconfirmed(rpc_relay, field, value):
    relay, sdk = rpc_relay
    session = RelaySession(*relay.address)
    handle = relay.owner.handle
    def corrupt(packet, peer):
        response = handle(packet, peer)
        if packet.get("operation") == "hold":
            if field == "payload":
                return value
            response[field] = value
        return response
    relay.owner.handle = corrupt
    try:
        with pytest.raises((RuntimeError, TimeoutError)):
            session.hold()
        assert_latched(session)
        assert sdk.calls == [(0., 0., 0.)]
    finally:
        session.close()


def test_old_success_response_cannot_confirm_new_stop(rpc_relay):
    relay, sdk = rpc_relay
    session = RelaySession(*relay.address)
    session.hold()
    old = dict(session.stop_transaction)
    handle = relay.owner.handle
    def old_reply(packet, peer):
        handle(packet, peer)
        return old
    relay.owner.handle = old_reply
    try:
        with pytest.raises(TimeoutError):
            session.hold()
        assert_latched(session)
        assert len(sdk.calls) == 2  # Two distinct requests, never a retry.
    finally:
        session.close()


def test_duplicate_hold_rejected_without_second_rpc(rpc_relay):
    relay, sdk = rpc_relay
    sender = Sender(relay)
    try:
        assert sender.claim()["accepted"]
        packet = sender.packet("hold")
        assert sender.send(packet)["accepted"]
        assert not sender.send(packet)["accepted"]
        assert sdk.calls == [(0., 0., 0.)]
    finally:
        sender.socket.close()


def test_relay_restart_during_stop_rejects_old_session(rpc_relay):
    relay, sdk = rpc_relay
    session = RelaySession(*relay.address)
    new_owner = RelayOwnership(ResultPreservingClient(sdk, 0))
    replace_loop_state(relay, owner=new_owner)  # Same endpoint, fresh state/epoch.
    try:
        with pytest.raises(RuntimeError):
            session.hold()
        assert_latched(session)
        assert sdk.calls == []
    finally:
        session.close()


@pytest.mark.parametrize("version", [2, 3])
def test_actual_old_dispatch_and_v4_client_reject_each_other(rpc_relay, version):
    relay, sdk = rpc_relay
    if version == 2:
        source = subprocess.check_output(["git", "show", "cf1e4d9:patrol/locomotion_protocol.py"],
                                         cwd=os.environ.get("G1_REVIEW_GIT_ROOT", ROOT), text=True)
    else:
        # Exact v3 source from a27367d; retain it because amendment makes that
        # commit unreachable from the new branch history on a fresh clone.
        source = (ROOT / "tests/fixtures/locomotion_protocol_v3.py").read_text()
    legacy = types.ModuleType("legacy_protocol")
    exec(compile(source, "legacy_protocol.py", "exec"), legacy.__dict__)
    sender = Sender(relay)
    packet = sender.packet("claim", client_nonce=str(uuid.uuid4()))
    packet["protocol_version"] = version
    try:
        assert not sender.send(packet)["accepted"]
        replace_loop_state(relay, owner=legacy.RelayOwnership(sdk))
        with pytest.raises(RuntimeError):
            RelaySession(*relay.address)
        assert sdk.calls == [] and sdk.moves == []
    finally:
        sender.socket.close()


@pytest.mark.parametrize("mode", ["success", "request_lost", "response_lost", "response_delayed",
                                  "rpc_error", "rpc_exception", "rpc_block"])
def test_production_reaction_job_requires_confirmed_rpc(rpc_relay, tmp_path, mode):
    relay, sdk = rpc_relay
    adapter = UdpLocomotionAdapter(*relay.address, telemetry_bind="127.0.0.1", telemetry_port=0)
    adapter.imu_sample = lambda: dict(imu_ready=True, yaw=0, imu_age=0, transport_age=0)
    adapter.odom_sample = lambda: dict(odom_ready=True, odom_x=0, odom_y=0, odom_yaw=0,
                                      odom_age=0, transport_age=0)
    # Deterministic valid F03 lease; lease-loss behavior is tested separately.
    controller = PatrolController(adapter, AlwaysClear(), emit=lambda _: None,
                                  lease_id="test", lease_timeout_s=.3, clock=lambda: 0.)
    controller.heartbeat("test")
    controller.resume()
    assert controller._send_move(.1, 0)
    server = PatrolControlServer(tmp_path / "patrol.sock", controller)
    server.start()
    events = []
    reaction, _ = build(events)
    local = LocalPatrolController(server.path)
    reaction.patrol = reaction.interlock = local
    reaction.engine.handle = Mock(wraps=reaction.engine.handle)
    controller.resume = Mock(wraps=controller.resume)
    sdk.calls.clear()
    sdk.entered.clear()
    configure_failure(relay, sdk, mode)
    try:
        assert reaction.trigger("person", time.monotonic())
        if mode == "rpc_block":
            assert sdk.entered.wait(1)
            assert reaction.engine.handle.call_count == 0
            assert events == []
            time.sleep(.15)
            sdk.release.set()
        wait_idle(reaction)
        if mode == "success":
            assert reaction.engine.handle.call_count == 1
            assert "AUDIO" in events and "motiondecode:found" in events
            assert controller.resume.call_count == 1
        else:
            assert reaction.engine.handle.call_count == 0
            assert "AUDIO" not in events and "motiondecode:found" not in events
            assert controller.resume.call_count == 0
            assert controller.control_status()["control_fault"]
            assert_latched(adapter._session)
            with pytest.raises(RuntimeError):
                controller._send_move(.1, 0)
            time.sleep(.22)
            assert reaction.engine.handle.call_count == 0
    finally:
        if sdk.release is not None:
            sdk.release.set()
        reaction.close()
        for cleanup in (server.close, adapter.close):
            try:
                cleanup()
            except (RuntimeError, TimeoutError):
                pass  # Expected latched cleanup rejection; never retries the STOP RPC.


@pytest.mark.parametrize("reason", ["control lease expired", "control IPC failed"])
def test_control_fault_remains_latched_when_stop_fails(rpc_relay, reason):
    relay, sdk = rpc_relay
    adapter = UdpLocomotionAdapter(*relay.address, telemetry_bind="127.0.0.1", telemetry_port=0)
    controller = PatrolController(adapter, AlwaysClear(), emit=lambda _: None,
                                  lease_id="test", lease_timeout_s=.3)
    controller.heartbeat("test")
    sdk.result = 3104
    try:
        with pytest.raises(RuntimeError):
            controller.control_fault(reason)
        assert controller.control_status()["control_fault"] == reason
        with pytest.raises(RuntimeError):
            controller._send_move(.1, 0)
        assert sdk.moves == []
    finally:
        try:
            adapter.close()
        except RuntimeError:
            pass
