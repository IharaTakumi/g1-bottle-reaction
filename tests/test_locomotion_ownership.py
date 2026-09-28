"""Linux flock, real localhost UDP, fake SDK only. No robot initialization."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "patrol"))
from locomotion_protocol import RelayOwnership, serve_datagram
from locomotion_session import RelaySession
from locomotion_adapter import UdpLocomotionAdapter
from patrol_controller import PatrolController
from run_patrol import AlwaysClear
import locomotion_adapter
import locomotion_protocol
import locomotion_relay
from robot_side.adapters.g1_robot import UnitreeSdkRuntime


class FakeSdk:
    def __init__(self):
        self.moves = []
        self.stops = 0

    def Move(self, *args, **kwargs):
        self.moves.append(args)

    def StopMove(self):
        self.stops += 1
        return 0


class LocalRelay:
    def __init__(self, clock=time.monotonic):
        self.sdk = FakeSdk()
        self.owner = RelayOwnership(self.sdk, clock=clock)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(.01)
        self.address = self.socket.getsockname()
        self.closed = threading.Event()
        self.errors = []
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self):
        try:
            while not self.closed.is_set():
                serve_datagram(self.socket, self.owner)
        except BaseException as exc:
            self.errors.append(exc)

    def close(self):
        self.closed.set()
        self.thread.join(2)
        self.socket.close()
        assert not self.thread.is_alive()
        assert not self.errors


@pytest.fixture
def relay():
    value = LocalRelay()
    try:
        yield value
    finally:
        value.close()


class Sender:
    def __init__(self, relay):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.connect(relay.address)
        self.socket.settimeout(1)
        self.epoch = relay.owner.epoch
        self.session = self.generation = None
        self.seq = -1

    def packet(self, operation, **kw):
        self.seq += 1
        return dict(protocol_version=4, request_id=str(uuid.uuid4()), operation=operation,
                    relay_epoch=self.epoch, owner_session=self.session,
                    movement_generation=self.generation, sequence=self.seq, **kw)

    def send(self, packet):
        self.socket.send(json.dumps(packet).encode())
        return json.loads(self.socket.recv(4096))

    def command(self, operation, **kw):
        reply = self.send(self.packet(operation, **kw))
        if reply["accepted"]:
            self.session = reply.get("owner_session", self.session)
            self.generation = reply.get("movement_generation", self.generation)
            if operation == "hold":
                return self.command("commit_stop", stop_request_id=reply["stop_request_id"])
        return reply

    def claim(self):
        return self.command("claim", client_nonce=str(uuid.uuid4()))


@pytest.fixture
def sender(relay):
    value = Sender(relay)
    try:
        assert value.claim()["accepted"]
        yield value
    finally:
        value.socket.close()


VELOCITY = {"vx": .1, "vy": 0, "vyaw": 0}


def test_python38_socket_timeout_still_checks_watchdog(monkeypatch):
    class LegacySocketTimeout(OSError):
        pass
    monkeypatch.setattr(locomotion_protocol.socket, "timeout", LegacySocketTimeout)
    owner = RelayOwnership(FakeSdk())
    sock = Mock(recvfrom=Mock(side_effect=LegacySocketTimeout()))
    serve_datagram(sock, owner)
    sock.sendto.assert_not_called()


def test_claim_is_held_second_sender_and_larger_sequence_rejected(relay, sender):
    assert relay.owner.state == "MOVEMENT_HELD"
    assert not sender.command("move", velocity=VELOCITY)["accepted"]
    b = Sender(relay)
    try:
        assert not b.claim()["accepted"]
        assert sender.command("hold")["accepted"]
        assert sender.command("enable")["accepted"]
        packet = sender.packet("move", velocity=VELOCITY)
        packet["sequence"] = 999999
        assert not b.send(packet)["accepted"]
        assert relay.sdk.moves == []
    finally:
        b.socket.close()


@pytest.mark.parametrize("field,value", [
    ("owner_session", str(uuid.uuid4())), ("owner_session", None),
    ("owner_session", []), ("relay_epoch", str(uuid.uuid4())),
    ("protocol_version", 1), ("protocol_version", True),
    ("sequence", -1), ("sequence", True),
    ("movement_generation", str(uuid.uuid4())),
])
def test_reject_does_not_refresh_or_advance(relay, sender, field, value):
    assert sender.command("hold")["accepted"]
    assert sender.command("enable")["accepted"]
    before = (relay.owner.sequence, relay.owner.last_move, relay.owner.generation)
    packet = sender.packet("move", velocity=VELOCITY)
    packet[field] = value
    assert not sender.send(packet)["accepted"]
    assert (relay.owner.sequence, relay.owner.last_move, relay.owner.generation) == before
    assert relay.sdk.moves == []


def test_replay_and_legacy_have_zero_additional_moves(relay, sender):
    assert sender.command("hold")["accepted"]
    assert sender.command("enable")["accepted"]
    packet = sender.packet("move", velocity=VELOCITY)
    assert sender.send(packet)["accepted"]
    assert not sender.send(packet)["accepted"]
    packet["sequence"] -= 1
    assert not sender.send(packet)["accepted"]
    assert not sender.send({"seq": 99999, **VELOCITY})["accepted"]
    assert len(relay.sdk.moves) == 1


def test_hold_blocks_delayed_move_before_and_after_explicit_resume(relay, sender):
    assert sender.command("hold")["accepted"]
    assert sender.command("enable")["accepted"]
    assert sender.command("move", velocity=VELOCITY)["accepted"]
    old = sender.packet("move", velocity=VELOCITY)
    old["sequence"] = 1000  # Reject by generation, not just ordering.
    session = sender.session
    assert sender.command("hold")["accepted"]
    assert relay.sdk.stops == 2
    assert not sender.send(old)["accepted"]
    assert sender.session == session
    assert sender.command("hold")["accepted"]
    assert sender.command("enable")["accepted"]
    assert not sender.send(old)["accepted"]
    assert sender.command("move", velocity=VELOCITY)["accepted"]
    assert len(relay.sdk.moves) == 2


def test_watchdog_fault_cannot_be_renewed_or_taken_over(relay, sender):
    assert sender.command("hold")["accepted"]
    assert sender.command("enable")["accepted"]
    assert sender.command("move", velocity=VELOCITY)["accepted"]
    until = time.monotonic() + 2
    while relay.owner.state != "FAULT" and time.monotonic() < until:
        # Continuous rejected traffic cannot suppress watchdog evaluation.
        assert not sender.send({"seq": 999, **VELOCITY})["accepted"]
        time.sleep(.01)
    assert relay.owner.state == "FAULT"
    assert relay.sdk.stops >= 1
    assert not sender.command("move", velocity=VELOCITY)["accepted"]
    assert not sender.command("enable")["accepted"]
    assert not sender.claim()["accepted"]
    assert len(relay.sdk.moves) == 1


def test_relay_restart_rejects_old_session_and_new_claim_has_fresh_sequence(relay, sender):
    assert sender.command("hold")["accepted"]
    assert sender.command("enable")["accepted"]
    old = sender.packet("move", velocity=VELOCITY)
    restarted = LocalRelay()
    b = Sender(restarted)
    try:
        assert restarted.owner.epoch != relay.owner.epoch
        assert not b.send(old)["accepted"]
        assert restarted.sdk.moves == []
        assert b.claim()["accepted"]
        assert b.session != sender.session
        assert b.command("hold")["accepted"]
        assert b.command("enable")["accepted"]
        assert b.command("move", velocity=VELOCITY)["accepted"]
    finally:
        b.socket.close()
        restarted.close()


def test_new_packets_cannot_decode_as_legacy_move(relay, sender):
    for operation in ("discover", "claim", "enable", "hold", "move"):
        packet = sender.packet(operation, velocity=VELOCITY)
        with pytest.raises((ValueError, KeyError)):
            locomotion_relay.decode(json.dumps(packet).encode(), False)


def test_production_client_envelopes_are_downgrade_safe(relay):
    seen = []
    handle = relay.owner.handle
    def record(packet, peer):
        seen.append(packet)
        return handle(packet, peer)
    relay.owner.handle = record
    client = RelaySession(*relay.address)
    client.hold()
    try:
        client.enable()
        client.move(.1, 0)
        client.hold()
    finally:
        client.close()
    assert [p["operation"] for p in seen] == [
        "discover", "claim", "hold", "commit_stop", "enable", "move", "hold", "commit_stop"]
    for packet in seen:
        with pytest.raises((ValueError, KeyError)):
            locomotion_relay.decode(json.dumps(packet).encode(), False)


@pytest.mark.parametrize("mode", ["missing_generation", "wrong_epoch", "malformed", "timeout"])
def test_enable_response_failure_latches_client_without_any_move(relay, mode):
    client = RelaySession(*relay.address)
    client.hold()
    handle = relay.owner.handle
    def broken_reply(packet, peer):
        reply = handle(packet, peer)
        if packet.get("operation") == "enable":
            if mode == "missing_generation":
                reply.pop("movement_generation")
            elif mode == "wrong_epoch":
                reply["relay_epoch"] = str(uuid.uuid4())
            elif mode == "malformed":
                return []
            else:
                reply["request_id"] = str(uuid.uuid4())  # No matching response.
        return reply
    relay.owner.handle = broken_reply
    try:
        with pytest.raises((RuntimeError, TimeoutError, ValueError)):
            client.enable()
        with pytest.raises(RuntimeError, match="latched"):
            client.enable()
        with pytest.raises(RuntimeError):
            client.move(.1, 0)
        assert relay.sdk.moves == []
    finally:
        client.close()


def test_production_patrol_adapter_resume_pause_and_f03_fault(relay):
    adapter = UdpLocomotionAdapter(*relay.address, telemetry_bind="127.0.0.1", telemetry_port=0)
    adapter.imu_sample = lambda: dict(imu_ready=True, yaw=0, imu_age=0, transport_age=0)
    adapter.odom_sample = lambda: dict(odom_ready=True, odom_x=0, odom_y=0, odom_yaw=0,
                                      odom_age=0, transport_age=0)
    controller = PatrolController(adapter, AlwaysClear(), emit=lambda _: None,
                                  lease_id="test", lease_timeout_s=.3)
    try:
        with pytest.raises(RuntimeError):
            adapter.move(.1)
        with pytest.raises(RuntimeError, match="no active"):
            controller.resume()
        controller.heartbeat("test")
        controller.resume()
        assert controller._send_move(.1, 0)
        controller.pause()
        assert not controller._send_move(.1, 0)
        assert relay.owner.state == "MOVEMENT_HELD"
        controller.resume()
        assert controller._send_move(.1, 0)
        controller.control_fault("test loss")
        count = len(relay.sdk.moves)
        with pytest.raises(RuntimeError):
            controller._send_move(.1, 0)
        assert len(relay.sdk.moves) == count
    finally:
        adapter.close()


def test_unleased_patrol_cannot_enable_real_protocol(relay):
    adapter = UdpLocomotionAdapter(*relay.address, telemetry_bind="127.0.0.1", telemetry_port=0)
    controller = PatrolController(adapter, AlwaysClear(), emit=lambda _: None)
    try:
        with pytest.raises(RuntimeError, match="requires a supervisor"):
            controller._send_move(.1, 0)
        assert relay.owner.state == "MOVEMENT_HELD"
        assert relay.sdk.moves == []
    finally:
        adapter.close()


@pytest.mark.parametrize("mode", ["silence", "legacy", "wrong_epoch", "reject"])
def test_client_handshake_failure_never_sends_legacy_or_move(mode):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(.4)
    seen = []
    def server():
        try:
            while True:
                data, peer = sock.recvfrom(4096)
                packet = json.loads(data)
                seen.append(packet)
                if mode == "silence":
                    continue
                reply = dict(protocol_version=4, accepted=True, request_id=packet["request_id"], operation=packet["operation"],
                             relay_epoch=str(uuid.uuid4()), state="MOVEMENT_HELD")
                if mode == "legacy":
                    reply["protocol_version"] = 1
                if mode == "reject" and packet["operation"] == "claim":
                    reply["accepted"] = False
                sock.sendto(json.dumps(reply).encode(), peer)
        except TimeoutError:
            pass
    thread = threading.Thread(target=server)
    thread.start()
    try:
        with pytest.raises((RuntimeError, TimeoutError)):
            RelaySession(*sock.getsockname())
    finally:
        thread.join(2)
        sock.close()
    assert seen
    assert all(p["operation"] in {"discover", "claim"} for p in seen)
    assert all("seq" not in p and "vx" not in p for p in seen)


def test_sdk_exception_latches_before_further_move(relay, sender):
    assert sender.command("hold")["accepted"]
    assert sender.command("enable")["accepted"]
    relay.sdk.StopMove = Mock(side_effect=ValueError("fake STOP failure"))
    assert not sender.command("hold")["accepted"]
    assert relay.owner.state == "FAULT"
    assert not sender.command("move", velocity=VELOCITY)["accepted"]
    assert relay.sdk.moves == []


@pytest.mark.skipif(sys.platform != "linux", reason="Linux flock/process boundary")
def test_cross_directory_lock_contention_and_hard_exit_release(tmp_path):
    lock = tmp_path / "canonical.lock"
    a_dir, b_dir = tmp_path / "checkout-a", tmp_path / "checkout-b"
    a_dir.mkdir(); b_dir.mkdir()
    code = ("import sys,time; sys.path.insert(0,sys.argv[1]); "
            "from patrol.ownership_lock import acquire_process_lock; "
            "acquire_process_lock(sys.argv[2]); print('LOCKED',flush=True); time.sleep(30)")
    args = [sys.executable, "-B", "-c", code, str(ROOT), str(lock)]
    a = subprocess.Popen(args, cwd=a_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert a.stdout.readline().strip() == "LOCKED"
        inode = lock.stat().st_ino
        b = subprocess.run(args, cwd=b_dir, capture_output=True, text=True, timeout=3)
        assert b.returncode != 0 and "BlockingIOError" in b.stderr
        a.kill(); a.wait(timeout=3)
        b = subprocess.Popen(args, cwd=b_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert b.stdout.readline().strip() == "LOCKED"
            assert lock.stat().st_ino == inode
        finally:
            b.kill(); b.wait(timeout=3)
    finally:
        if a.poll() is None:
            a.kill()
        a.wait(timeout=3)


@pytest.mark.parametrize("entry", ["relay", "adapter", "one_shot_runtime"])
def test_lock_conflict_precedes_every_sdk_initialization(monkeypatch, entry):
    def conflict():
        raise BlockingIOError("test ownership conflict")
    import patrol.ownership_lock as lock_module
    constructor = Mock(side_effect=AssertionError("SDK must not be constructed"))
    # Stub the import boundary as well as asserting that the lock raises first.
    from types import ModuleType
    sdk = ModuleType("unitree_sdk2py.g1.loco.g1_loco_client")
    sdk.LocoClient = constructor
    channel = ModuleType("unitree_sdk2py.core.channel")
    channel.ChannelFactoryInitialize = constructor
    monkeypatch.setitem(sys.modules, "unitree_sdk2py.g1.loco.g1_loco_client", sdk)
    monkeypatch.setitem(sys.modules, "unitree_sdk2py.core.channel", channel)
    monkeypatch.setattr(locomotion_relay, "acquire_process_lock", conflict)
    monkeypatch.setattr(locomotion_adapter, "acquire_process_lock", conflict)
    monkeypatch.setattr(lock_module, "acquire_process_lock", conflict)
    with pytest.raises(BlockingIOError):
        if entry == "relay":
            locomotion_relay.real_runtime("unused", True)
        elif entry == "adapter":
            locomotion_adapter.LocomotionAdapter("unused", arm=True, allow_reverse=False)
        else:
            UnitreeSdkRuntime().create_loco_client("eth0", 2)
    constructor.assert_not_called()


@pytest.mark.parametrize("held", [False, True])
def test_actual_controller_death_never_allows_takeover(relay, held):
    code = """
import sys,time
sys.path.insert(0,sys.argv[1])
from patrol.locomotion_session import RelaySession
s=RelaySession('127.0.0.1',int(sys.argv[2]))
if sys.argv[3]=='moving':
    s.hold(); s.enable(); s.move(.1,0)
print('READY',flush=True)
while True:
    if sys.argv[3]=='moving': s.move(.1,0)
    time.sleep(.05)
"""
    child = subprocess.Popen([sys.executable, "-B", "-c", code, str(ROOT),
                              str(relay.address[1]), "held" if held else "moving"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "READY"
        child.kill(); child.wait(timeout=3)
        time.sleep(.55)
        assert relay.owner.state == ("MOVEMENT_HELD" if held else "FAULT")
        if not held:
            assert relay.sdk.moves and relay.sdk.stops >= 1
        with pytest.raises(RuntimeError):
            RelaySession(*relay.address)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)
