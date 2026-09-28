"""v4 barrier failures through real UDP and the production Reaction path."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid
from unittest.mock import Mock

import pytest

from test_locomotion_ownership import Sender
from test_stop_transactions import (
    rpc_relay, replace_loop_state, assert_latched, build, wait_idle,
    RelaySession, UdpLocomotionAdapter, PatrolController, AlwaysClear,
    PatrolControlServer, LocalPatrolController,
)


class CommitSocket:
    def __init__(self, sock, mode):
        self.sock, self.mode = sock, mode

    def __getattr__(self, key):
        return getattr(self.sock, key)

    def recvfrom(self, size):
        data, peer = self.sock.recvfrom(size)
        packet = json.loads(data)
        if packet.get("operation") == "commit_stop":
            if self.mode == "timeout":
                raise socket.timeout()
            if self.mode in {"stop_request_id", "relay_epoch", "owner_session", "movement_generation"}:
                packet[self.mode] = str(uuid.uuid4())
                data = json.dumps(packet).encode()
        return data, peer

    def sendto(self, data, peer):
        response = json.loads(data)
        if response.get("operation") == "commit_stop":
            if self.mode == "lost_ack":
                return len(data)
            if self.mode == "late_ack":
                time.sleep(.2)
            if self.mode == "malformed":
                data = b"[]"
            if self.mode == "wrong_response":
                response["stop_request_id"] = str(uuid.uuid4())
                data = json.dumps(response).encode()
        return self.sock.sendto(data, peer)


def break_commit(relay, mode):
    if mode in {"missing_transaction", "fault", "enabled"}:
        handle = relay.owner.handle
        def changed(packet, peer):
            if packet.get("operation") == "commit_stop":
                if mode == "missing_transaction":
                    relay.owner.stop_transaction = None
                elif mode == "fault":
                    relay.owner.state = "FAULT"
                else:
                    relay.owner.state = "MOVEMENT_ENABLED"
                    relay.owner.last_move = relay.owner.clock()
            return handle(packet, peer)
        relay.owner.handle = changed
    else:
        replace_loop_state(relay, socket=CommitSocket(relay.socket, mode))


FAILURES = ["timeout", "lost_ack", "late_ack", "stop_request_id", "relay_epoch",
            "owner_session", "movement_generation", "missing_transaction", "fault",
            "enabled", "malformed", "wrong_response"]


def test_prepared_cannot_enable_and_duplicate_commit_has_no_rpc(rpc_relay):
    relay, sdk = rpc_relay
    sender = Sender(relay)
    try:
        assert sender.claim()["accepted"]
        stop = sender.send(sender.packet("hold"))
        sender.generation = stop["movement_generation"]
        assert stop["stop_rpc_status"] == "STOP_RPC_PREPARED"
        assert relay.owner.stop_transaction["committed"] is False
        assert not sender.command("enable")["accepted"]
        commit = sender.packet("commit_stop", stop_request_id=stop["request_id"])
        response = sender.send(commit)
        assert response["stop_rpc_status"] == "STOP_RPC_CONFIRMED"
        assert relay.owner.stop_transaction["committed"] is True
        assert not sender.send(commit)["accepted"]
        assert relay.owner.state == "MOVEMENT_HELD"
        assert sdk.calls == [(0., 0., 0.)] and sdk.moves == []
    finally:
        sender.socket.close()


@pytest.mark.parametrize("mode", FAILURES)
def test_commit_failure_latches_without_rpc_retry(rpc_relay, mode):
    relay, sdk = rpc_relay
    session = RelaySession(*relay.address)
    break_commit(relay, mode)
    try:
        with pytest.raises((RuntimeError, TimeoutError)):
            session.hold()
        assert_latched(session)
        if mode == "lost_ack":
            assert relay.owner.stop_transaction["committed"] is True
        time.sleep(.22)
        assert_latched(session)
        assert sdk.calls == [(0., 0., 0.)] and sdk.moves == []
    finally:
        session.close()


def reaction_setup(address, tmp_path):
    adapter = UdpLocomotionAdapter(*address, telemetry_bind="127.0.0.1", telemetry_port=0)
    adapter.imu_sample = lambda: dict(imu_ready=True, yaw=0, imu_age=0, transport_age=0)
    adapter.odom_sample = lambda: dict(odom_ready=True, odom_x=0, odom_y=0, odom_yaw=0,
                                      odom_age=0, transport_age=0)
    controller = PatrolController(adapter, AlwaysClear(), emit=lambda _: None,
                                  lease_id="test", lease_timeout_s=.3, clock=lambda: 0.)
    controller.heartbeat("test")
    controller.resume()
    assert controller._send_move(.1, 0)
    controller.resume = Mock(wraps=controller.resume)
    server = PatrolControlServer(tmp_path / "commit.sock", controller)
    server.start()
    events = []
    reaction, _ = build(events)
    reaction.patrol = reaction.interlock = LocalPatrolController(server.path)
    reaction.engine.handle = Mock(wraps=reaction.engine.handle)
    return adapter, controller, server, reaction, events


def check_no_reaction(parts):
    adapter, controller, _, reaction, events = parts
    wait_idle(reaction)
    assert_latched(adapter._session)
    assert reaction.engine.handle.call_count == 0
    assert controller.resume.call_count == 0
    assert events == []  # Neither fake audio nor MotionDecode called.
    with pytest.raises(RuntimeError):
        controller.resume()


def cleanup_reaction(parts):
    adapter, _, server, reaction, _ = parts
    reaction.close()
    for close in (server.close, adapter.close):
        try:
            close()
        except (RuntimeError, TimeoutError):
            pass


def test_prepared_client_does_not_start_job_until_commit_ack(rpc_relay, tmp_path):
    relay, sdk = rpc_relay
    parts = reaction_setup(relay.address, tmp_path)
    entered, release = threading.Event(), threading.Event()
    class BarrierSocket(CommitSocket):
        def sendto(self, data, peer):
            if json.loads(data).get("operation") == "commit_stop":
                entered.set()
                assert release.wait(2)
            return self.sock.sendto(data, peer)
    replace_loop_state(relay, socket=BarrierSocket(relay.socket, "barrier"))
    try:
        assert parts[3].trigger("person", time.monotonic())
        assert entered.wait(1)
        assert parts[0].stop_rpc_status == "STOP_RPC_PREPARED"
        assert parts[3].engine.handle.call_count == 0
        assert parts[1].resume.call_count == 0 and parts[4] == []
        release.set()
        wait_idle(parts[3])
        assert parts[3].engine.handle.call_count == 1
        assert parts[1].resume.call_count == 1
        assert "AUDIO" in parts[4] and "motiondecode:found" in parts[4]
    finally:
        release.set()
        cleanup_reaction(parts)


@pytest.mark.parametrize("mode", FAILURES)
def test_production_reaction_zero_on_commit_failure(rpc_relay, tmp_path, mode):
    relay, sdk = rpc_relay
    parts = reaction_setup(relay.address, tmp_path)
    sdk.calls.clear()
    break_commit(relay, mode)
    try:
        assert parts[3].trigger("person", time.monotonic())
        check_no_reaction(parts)
        assert sdk.calls == [(0., 0., 0.)]
    finally:
        cleanup_reaction(parts)


# Only fake SDK and localhost UDP. A really exits and closes its socket; B binds
# the same port. Replaying A's bytes from B's socket models a delayed datagram
# from that endpoint, not a response with an artificially changed epoch.
CHILD = r'''
import json, socket, sys
sys.path.insert(0, sys.argv[1])
from locomotion_protocol import RelayOwnership, serve_datagram
class FakeSdk:
    def __init__(self): self.stops = 0
    def StopMove(self): self.stops += 1; return 0
    def Move(self, *a, **k): pass
sdk = FakeSdk()
owner = RelayOwnership(sdk)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('127.0.0.1', int(sys.argv[2])))
sock.settimeout(.01)
class Transport:
    def recvfrom(self, n): return sock.recvfrom(n)
    def sendto(self, data, peer):
        response = json.loads(data)
        if sys.argv[3] == 'A' and response.get('operation') == 'hold' and sdk.stops == 2:
            print(json.dumps(dict(response=response, peer=peer)), flush=True)
            raise SystemExit(0)
        if response.get('operation') == 'commit_stop':
            print(json.dumps(dict(commit=response)), flush=True)
        return sock.sendto(data, peer)
print(json.dumps(dict(port=sock.getsockname()[1], epoch=owner.epoch)), flush=True)
if sys.argv[3] == 'B':
    delayed = json.loads(sys.argv[4])
    sock.sendto(json.dumps(delayed['response']).encode(), tuple(delayed['peer']))
try:
    while True: serve_datagram(Transport(), owner)
finally:
    sock.close()
'''


def test_actual_process_restart_delayed_prepared_blocks_reaction(tmp_path):
    root = str(Path(__file__).resolve().parents[1] / "patrol")
    children = []
    def start(role, port, delayed="{}"):
        process = subprocess.Popen([sys.executable, "-B", "-u", "-c", CHILD,
                                    root, str(port), role, delayed],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        children.append(process)
        ready = json.loads(process.stdout.readline())
        return process, ready
    a, ready_a = start("A", 0)
    evidence = {}
    errors = []
    def restart():
        try:
            # Initial resume also makes one HOLD/commit. Wait for reaction HOLD.
            while True:
                line = a.stdout.readline()
                if not line:
                    raise AssertionError("relay A exited before prepared STOP")
                delayed = json.loads(line)
                if "response" in delayed:
                    break
            assert delayed["response"]["stop_rpc_status"] == "STOP_RPC_PREPARED"
            assert delayed["response"]["raw_rpc_code"] == 0
            assert a.wait(timeout=3) == 0
            b, ready_b = start("B", ready_a["port"], json.dumps(delayed))
            assert ready_b["epoch"] != ready_a["epoch"]
            assert ready_b["port"] == ready_a["port"]
            evidence.update(prepared=delayed, b=ready_b,
                            commit=json.loads(b.stdout.readline())["commit"])
        except BaseException as exc:
            errors.append(exc)
    monitor = threading.Thread(target=restart, daemon=True)
    monitor.start()
    parts = None
    try:
        parts = reaction_setup(("127.0.0.1", ready_a["port"]), tmp_path)
        # Test-only scheduling allowance for interpreter startup. Production
        # request deadline remains 0.10 s; ordinary deadline tests use that value.
        parts[0]._session.timeout = 2.0
        assert parts[3].trigger("person", time.monotonic())
        check_no_reaction(parts)
        monitor.join(3)
        assert not errors and not monitor.is_alive()
        assert evidence["commit"]["accepted"] is False
        assert evidence["commit"]["error"] == "wrong relay epoch"
        assert evidence["commit"]["stop_request_id"] is None
    finally:
        if parts is not None:
            cleanup_reaction(parts)
        for process in children:
            if process.poll() is None:
                process.terminate()  # Only the local fake children created here.
            process.wait(timeout=3)
        monitor.join(3)
