#!/usr/bin/env python3
"""Single calibration trial: existing Patrol control, passive recording, no Reaction."""
import argparse
import base64
from dataclasses import asdict
import ipaddress
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "patrol"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from calibration_recording import Recorder, Telemetry
from control_lease import load_control_timing
from locomotion_adapter import UdpLocomotionAdapter
from locomotion_session import RelaySession
from patrol_controller import PatrolConfig, PatrolController
from udp_relay_guard import UdpRelayGuard
from lidar_guard import GuardState


class SocketTap:
    """Observe exact v4 datagrams; never implement or change the protocol."""
    def __init__(self, sock, recorder):
        self.sock, self.recorder = sock, recorder

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def send(self, data):
        packet = json.loads(data)
        operation = packet.get("operation")
        if operation in {"move", "enable"}:
            self.recorder.check()
        if operation == "hold":
            identity = dict(packet, stop_request_id=packet["request_id"], stop_rpc_status="STOP_REQUESTED")
            self.recorder.event("STOP_REQUESTED", phase="STOP_REQUESTED", identity=identity)
        self.recorder.event("PROTOCOL_SEND", protocol_direction="send", raw_payload_b64=base64.b64encode(data).decode())
        return self.sock.send(data)

    def recv(self, size):
        data = self.sock.recv(size)
        self.recorder.event("PROTOCOL_RECEIVE", protocol_direction="receive", raw_payload_b64=base64.b64encode(data).decode())
        return data


class RecordedSession(RelaySession):
    def __init__(self, host, port, recorder):
        self.recorder = recorder
        self.tapped = False
        super().__init__(host, port)

    def _exchange(self, operation, **fields):
        if not self.tapped:
            self.sock = SocketTap(self.sock, self.recorder)
            self.tapped = True
        try:
            reply = super()._exchange(operation, **fields)
        except BaseException as exc:
            if operation in {"hold", "commit_stop"}:
                self.recorder.event("STOP_UNCONFIRMED", identity=dict(
                    relay_epoch=self.epoch, owner_session=self.session,
                    movement_generation=self.generation,
                    stop_request_id=(self.stop_transaction or {}).get("stop_request_id",
                        (self.stop_transaction or {}).get("request_id")), stop_rpc_status="STOP_UNCONFIRMED"),
                    error=str(exc))
            raise
        names = {"hold": "STOP_RPC_PREPARED", "commit_stop": "STOP_RPC_CONFIRMED", "enable": "MOVEMENT_ENABLE"}
        if operation == "enable":
            with self.recorder.lock:
                self.recorder.scope = "trial"
        if operation in names:
            name = names[operation]
            self.recorder.event(name, phase=name, identity=reply, protocol_reply=reply,
                                timestamp_semantics="PC validated response; not relay processing time")
        return reply


class CalibrationAdapter(UdpLocomotionAdapter):
    """Use existing adapter commands with a separately owned passive receiver."""
    def __init__(self, host, port, telemetry, recorder):
        self._closed = False
        self.telemetry, self.recorder = telemetry, recorder
        self.cancelled = threading.Event()
        self.moving_marked = False
        self._session = RecordedSession(host, port, recorder)

    def imu_sample(self):
        return self.telemetry.sample()

    odom_sample = imu_sample

    def _allowed(self):
        self.recorder.check()
        if self.cancelled.is_set():
            raise RuntimeError("calibration trial cancelled")

    def enable_movement(self):
        self._allowed()
        return super().enable_movement()

    def move(self, vx, vyaw=0.):
        self._allowed()
        if vx < 0:
            raise RuntimeError("calibration reverse prohibited")
        if not self.moving_marked:
            self.recorder.event("MOVING", phase="MOVING", interpretation="first command attempt, not physical onset")
            self.moving_marked = True
        return super().move(vx, vyaw)

    def close(self):
        # Trial/controller owns STOP. Do not create an extra transaction during
        # file teardown or retry a failed HOLD/commit.
        self.cancelled.set()
        if not self._closed:
            self._closed = True
            self._session.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("standing", "forward-stop", "turn-stop"), required=True)
    p.add_argument("--output", type=Path, required=True, help="external data root; never inside repository")
    p.add_argument("--session-id", required=True)
    p.add_argument("--trial-id", required=True)
    p.add_argument("--telemetry-bind", required=True)
    p.add_argument("--telemetry-port", type=int, required=True)
    p.add_argument("--telemetry-peer", required=True)
    p.add_argument("--relay-host")
    p.add_argument("--relay-port", type=int)
    p.add_argument("--lidar-bind")
    p.add_argument("--lidar-port", type=int)
    p.add_argument("--pre-seconds", type=float, required=True)
    p.add_argument("--post-seconds", type=float, required=True)
    p.add_argument("--trial-timeout", type=float, required=True, help="operator-specified finite acquisition/trial deadline")
    p.add_argument("--loops", type=int, default=1)
    p.add_argument("--reverse", action="store_true")
    p.add_argument("--execute", action="store_true", help="explicitly enable passive sockets / approved trial")
    p.add_argument("--enable-real-robot", action="store_true")
    p.add_argument("--confirm-site-ready", action="store_true")
    p.add_argument("--operator-approved-calibration", action="store_true")
    p.add_argument("--confirm-external-recording", action="store_true")
    p.add_argument("--external-video")
    p.add_argument("--stdin-markers", action="store_true", help="type m + Enter for observation marker; never sends robot commands")
    return p


def validate_args(args):
    if args.loops != 1 or args.reverse:
        raise ValueError("exactly one trial; reverse prohibited")
    if not args.execute:
        raise ValueError("--execute required; nothing opened")
    for value in (args.pre_seconds, args.post_seconds, args.trial_timeout):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("explicit finite positive recording durations required")
    if args.trial_timeout <= args.pre_seconds + args.post_seconds:
        raise ValueError("trial timeout must exceed pre + post recording duration")
    for value in (args.session_id, args.trial_id):
        if not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in value):
            raise ValueError("session/trial IDs use ASCII letters, digits, hyphen, underscore only")
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError("output must be outside repository")
    for host in (args.telemetry_bind, args.telemetry_peer):
        if ipaddress.ip_address(host).version != 4:
            raise ValueError("explicit IPv4 required")
    for port in (args.telemetry_port,):
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
    if args.mode != "standing":
        if not (args.enable_real_robot and args.confirm_site_ready and args.operator_approved_calibration
                and args.confirm_external_recording and args.external_video and os.environ.get("G1_ALLOW_REAL_ACTION") == "1"):
            raise ValueError("motion requires all explicit robot/site/operator/video/environment approvals")
        for host in (args.relay_host, args.lidar_bind):
            if host is None or ipaddress.ip_address(host).version != 4:
                raise ValueError("explicit relay and LiDAR IPv4 required")
        for port in (args.relay_port, args.lidar_port):
            if port is None or not 1 <= port <= 65535:
                raise ValueError("explicit relay and LiDAR ports required")
    elif args.enable_real_robot:
        raise ValueError("standing never enables robot commands")


def read_operator_markers(recorder, stop, stream):
    """Optional annotation input. Losing the terminal never faults locomotion."""
    recorder.event("OPERATOR_MARKER_STATUS", operator_marker_status="available")
    try:
        while not stop.is_set():
            line = stream.readline()
            if not line:
                raise EOFError("marker input EOF")
            if line.strip() == "m" and not stop.is_set():
                recorder.operator_marker("local stdin m")
    except Exception as exc:
        if not stop.is_set():
            # Actual output/queue failure remains recorder-fatal through emit().
            # Only loss of the optional INPUT is downgraded to an annotation.
            recorder.event("OPERATOR_MARKER_STATUS", operator_marker_status="unavailable",
                           operator_marker_error=f"{type(exc).__name__}: {exc}")


def run(args, *, recorder_factory=Recorder, telemetry_factory=Telemetry,
        adapter_factory=CalibrationAdapter, guard_factory=UdpRelayGuard, controller_factory=PatrolController):
    validate_args(args)  # Also protect callers bypassing main().
    timing = load_control_timing()
    config = PatrolConfig()  # Exact existing validated profile; no new motion defaults/overrides.
    profile = asdict(config)
    print("CALIBRATION_PROFILE=" + json.dumps(profile, sort_keys=True), flush=True)
    print("REACTION=OFF VISION=OFF WANDER=OFF MOTIONDECODE=OFF", flush=True)
    manifest = dict(git_head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    branch=subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
                    python=sys.version, python_executable=sys.executable, f04_protocol_version=4,
                    ownership_contract_version=1, movement_parameters=profile, arguments={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                    pc_clock_id=str(uuid.uuid4()), clock_mapping_status="UNVERIFIED", external_video=args.external_video)
    # Must succeed before even a passive socket, claim or movement initialization.
    recorder = recorder_factory(args.output.resolve() / args.session_id / args.trial_id,
                                args.session_id, args.trial_id, manifest)
    telemetry = adapter = guard = controller = worker = None
    worker_errors = []
    leg_done = threading.Event()
    worker_done = threading.Event()
    teardown_requested = threading.Event()
    marker_stop = threading.Event()
    deadline = time.monotonic() + args.trial_timeout
    lease_id = str(uuid.uuid4())

    def checkpoint(*, renew_lease=False):
        if time.monotonic() >= deadline:
            raise TimeoutError("finite calibration trial deadline exceeded")
        recorder.barrier(timing.heartbeat_interval_s)
        if renew_lease:
            if worker_errors:
                raise worker_errors[0]
            if controller.heartbeat(lease_id).get("lease_active") is not True:
                raise RuntimeError("calibration lease inactive")

    def wait_recording(seconds, *, renew_lease=False):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            checkpoint(renew_lease=renew_lease)
            time.sleep(min(timing.heartbeat_interval_s, max(0., end-time.monotonic())))

    def movement():
        try:
            controller.run_calibration_leg(args.mode)
            leg_done.set()
            # This worker CHECKS the existing lease; it never renews it. Thus
            # a hung main during POST_STOP still expires the held controller.
            while not teardown_requested.wait(config.command_period_s):
                controller.check_control_lease()
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            worker_done.set()

    failure = None
    try:
        recorder.event("PRE", phase="PRE")
        telemetry = telemetry_factory(args.telemetry_bind, args.telemetry_port, args.telemetry_peer, recorder)
        if args.stdin_markers:
            threading.Thread(target=read_operator_markers,
                             args=(recorder, marker_stop, sys.stdin),
                             name="calibration-markers", daemon=True).start()
        wait_recording(args.pre_seconds)
        sample = telemetry.sample()
        if sample is None or sample.get("transport_age", float("inf")) > config.imu_stale_s:
            raise RuntimeError("fresh raw-source telemetry required before trial")
        if args.mode != "standing":
            guard = guard_factory(args.lidar_bind, args.lidar_port)
            # Existing guard owns its unchanged freshness/health interpretation.
            while guard.state("front") is not GuardState.CLEAR:
                checkpoint()
                time.sleep(timing.heartbeat_interval_s)
            checkpoint()
            adapter = adapter_factory(args.relay_host, args.relay_port, telemetry, recorder)
            controller = controller_factory(adapter, guard, config, lease_id=lease_id,
                lease_timeout_s=timing.lease_timeout_s, emit=lambda message: recorder.event("CONTROLLER_LOG", message=message))
            if controller.heartbeat(lease_id).get("lease_active") is not True:
                raise RuntimeError("calibration lease inactive before movement")
            checkpoint()
            worker = threading.Thread(target=movement, name="calibration-leg", daemon=True)
            recorder.event("TRIAL_ACTIVE")
            worker.start()
            while not leg_done.is_set():
                checkpoint(renew_lease=True)
                if worker_done.is_set() and not leg_done.is_set():
                    raise RuntimeError("calibration worker exited before leg completion")
                leg_done.wait(timing.heartbeat_interval_s)
            if worker_errors:
                raise worker_errors[0]
            if adapter.stop_rpc_status != "STOP_RPC_CONFIRMED":
                raise RuntimeError("calibration STOP_UNCONFIRMED")
            recorder.event("POST_STOP_OBSERVATION", phase="POST_STOP_OBSERVATION")
        wait_recording(args.post_seconds, renew_lease=controller is not None)
        checkpoint(renew_lease=controller is not None)
        if controller is not None:
            recorder.event("POST_STOP_OBSERVATION_COMPLETE", phase="POST_STOP_OBSERVATION_COMPLETE")
        checkpoint(renew_lease=controller is not None)  # Flush the complete observation before teardown.
        recorder.event("CONTROLLED_TEARDOWN", phase="CONTROLLED_TEARDOWN")
        teardown_requested.set()
        if controller is not None:
            while not worker_done.is_set():
                checkpoint(renew_lease=True)
                worker_done.wait(timing.heartbeat_interval_s)
            if worker_errors:
                raise worker_errors[0]
            # Expected shutdown, not lease expiry. Retain the ordinary cleanup
            # STOP, labeled separately from the observed primary transaction.
            with recorder.lock:
                recorder.scope = "cleanup"
            controller.stop()
    except BaseException as exc:
        failure = exc
        if adapter is not None:
            adapter.cancelled.set()  # Prevent every subsequent Move/enable before waiting on locks.
        recorder.event("TRIAL_FAILURE", error=str(exc))
        if controller is not None and adapter.stop_rpc_status != "STOP_UNCONFIRMED":
            try:
                controller.control_fault("calibration aborted: " + str(exc))
            except BaseException as stop_exc:
                recorder.event("STOP_FAILURE", error=str(stop_exc))
    finally:
        teardown_requested.set()
        marker_stop.set()
        if worker is not None:
            worker.join(timeout=2.)
            if worker.is_alive():
                failure = failure or RuntimeError("calibration worker did not exit; physical STOP unverified")
        # Independent cleanup attempts: one failure cannot skip remaining resources.
        for resource in (adapter, guard, telemetry):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as exc:
                    failure = failure or exc
        try:
            if failure is None:
                recorder.barrier(timing.heartbeat_interval_s)
                recorder.event("TRIAL_END", phase="END", outcome="CONTROL_CLOSED; physical stationary unverified")
                recorder.barrier(timing.heartbeat_interval_s)
            else:
                recorder.event("TRIAL_FAILURE", error=str(failure))
        except BaseException as exc:
            failure = failure or exc
        try:
            recorder.close()
        except BaseException as exc:
            failure = failure or exc
        if recorder.failed.is_set():
            failure = failure or RuntimeError(str(recorder.error))
    if failure is not None:
        raise failure
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    def interrupted(signum, frame):
        raise KeyboardInterrupt("calibration interrupted")
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        return run(args)
    except (ValueError, RuntimeError, OSError, KeyboardInterrupt) as exc:
        print("CALIBRATION FAILED: " + str(exc), file=sys.stderr)
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
