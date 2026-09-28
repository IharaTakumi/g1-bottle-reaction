"""Calibration-only bounded file recorder and passive UDP receiver; no SDK."""
import base64
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import socket
import threading
import time

from scripts.analyze_stationary_calibration import validate

IDENTITY = ("relay_epoch", "owner_session", "movement_generation", "stop_request_id")
RAW_FIELDS = {"odom_x_raw": "odom_x", "odom_y_raw": "odom_y",
              "odom_yaw_raw": "odom_yaw", "odom_stamp_raw": "odom_stamp_ns",
              "lowstate_yaw_raw": "yaw", "lowstate_tick_raw": "imu_tick"}


class Recorder:
    def __init__(self, directory, session_id, trial_id, manifest, *, capacity=128):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.error = None
        self.failed = threading.Event()
        self.closed = False
        self.lock = threading.Lock()
        self.phase = "PRE"
        self.scope = "initialization"
        self.identity = {k: None for k in IDENTITY}
        self.stop_state = "UNOBSERVED"
        self.base = dict(schema_version=1, session_id=session_id, trial_id=trial_id,
                         pc_clock_id=manifest["pc_clock_id"])
        self.queue = queue.Queue(maxsize=capacity)
        self.stream = None
        try:
            # Initialization, schema validation and initial flush precede sockets.
            (self.directory / "manifest.json").write_text(json.dumps(dict(
                manifest, session_id=session_id, trial_id=trial_id,
                date_utc=datetime.now(timezone.utc).isoformat(), recording_schema_version=1,
                deployment_evidence={"sdk_sha": None, "hashes": {}, "process_inventory": None}),
                allow_nan=False, indent=2) + "\n", encoding="utf-8")
            (self.directory / "operator_notes.txt").write_text("", encoding="utf-8")
            self.stream = (self.directory / "canonical.jsonl").open("x", encoding="utf-8")
            record = self.record("marker", "TRIAL_START")
            self._write(record)
        except BaseException:
            if self.stream is not None:
                self.stream.close()
            raise
        self.thread = threading.Thread(target=self._writer, name="calibration-recorder", daemon=True)
        self.thread.start()

    def fail(self, error):
        self.error = self.error or str(error)
        self.failed.set()

    def check(self):
        if self.failed.is_set() or self.closed:
            raise RuntimeError("calibration recorder unavailable: " + str(self.error))

    def record(self, kind, event=None, *, now=None, identity=None, **extra):
        with self.lock:
            record = dict(self.base, record_type=kind, phase=self.phase,
                          pc_receive_monotonic_s=time.monotonic() if now is None else now,
                          **self.identity, stop_transaction_state=self.stop_state,
                          transport={"command_scope": self.scope}, operator_marker={}, external_reference={})
        if identity:
            record.update({k: identity.get(k) for k in IDENTITY})
            record["stop_transaction_state"] = identity.get("stop_rpc_status") or "UNOBSERVED"
        if event:
            record["event"] = event
        record.update(extra)
        return record

    def emit(self, record):
        # Recording failure must NEVER prevent the existing STOP path running.
        try:
            self.check()
            self.queue.put_nowait(record)
        except Exception as exc:
            self.fail(exc)

    def event(self, name, *, phase=None, identity=None, **extra):
        with self.lock:
            if phase is not None:
                self.phase = phase
            if identity:
                self.identity.update({k: identity.get(k) for k in IDENTITY})
                self.stop_state = identity.get("stop_rpc_status") or "UNOBSERVED"
        self.emit(self.record("marker", name, **extra))

    def operator_marker(self, note):
        self.event("OPERATOR_EXTERNAL_STATIONARY_MARK", operator_marker={
            "note": note, "claim": "operator observation; not physical proof"})

    def _write(self, record):
        validate(record)
        self.stream.write(json.dumps(record, allow_nan=False) + "\n")
        self.stream.flush()

    def _writer(self):
        try:
            while True:
                try:
                    item = self.queue.get(timeout=.1)
                except queue.Empty:
                    if self.closed:
                        break
                    continue
                if item is None:
                    break
                if isinstance(item, threading.Event):
                    self.stream.flush()
                    item.set()
                else:
                    self._write(item)
        except BaseException as exc:
            self.fail(exc)
        finally:
            try:
                self.stream.close()
            except BaseException as exc:
                self.fail(exc)

    def barrier(self, timeout):
        self.check()
        ack = threading.Event()
        try:
            self.queue.put_nowait(ack)
        except queue.Full as exc:
            self.fail("recorder queue full")
            raise RuntimeError(self.error) from exc
        if not ack.wait(timeout):
            self.fail("recorder flush deadline exceeded")
        self.check()

    def close(self):
        if self.closed:
            return
        try:
            self.barrier(1.0)
        finally:
            self.closed = True
            try:
                self.queue.put_nowait(None)
            except queue.Full:
                self.fail("recorder shutdown queue full")
            self.thread.join(timeout=1.0)
            if self.thread.is_alive():
                self.fail("recorder shutdown incomplete")


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


class Telemetry:
    """Listen only. Preserve every datagram verbatim, including invalid/foreign data."""
    def __init__(self, bind, port, peer, recorder):
        self.recorder, self.peer = recorder, peer
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.latest = None
        self.count = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((bind, port))
            self.sock.settimeout(.1)
        except BaseException:
            self.sock.close()
            raise
        self.thread = threading.Thread(target=self._receive, name="calibration-telemetry", daemon=True)
        self.thread.start()

    def ingest(self, payload, peer, now):
        self.count += 1
        transport = dict(peer=list(peer), record_index=self.count, raw_payload_b64=base64.b64encode(payload).decode(),
                         command_scope=self.recorder.scope, provenance="relay snapshot; not DDS generation time")
        try:
            message = json.loads(payload)
            if peer[0] != self.peer or not isinstance(message, dict):
                raise ValueError("foreign peer or non-object packet")
            raw = {dest: message.get(source) for dest, source in RAW_FIELDS.items()}
            raw.update(lowstate_gyro_raw=message.get("imu_gyro"), lowstate_tick_unwrapped=None,
                       odom_stamp_unit_status="UNVERIFIED", odom_clock_mapping_status="UNVERIFIED",
                       lowstate_tick_status="UNVERIFIED")
            record = self.recorder.record("sample", now=now, identity=message, transport=transport, **raw)
            validate(record)
            self.recorder.emit(record)
            # Missing source fields are recordable, but cannot arm calibration.
            valid = all(finite(raw[k]) for k in RAW_FIELDS) and isinstance(raw["lowstate_gyro_raw"], list)
            if valid:
                with self.lock:
                    self.latest = (now, message)
        except (ValueError, TypeError, UnicodeError) as exc:
            self.recorder.emit(self.recorder.record("marker", "TELEMETRY_INVALID", now=now,
                                                   transport=transport, error=str(exc)))

    def _receive(self):
        try:
            while not self.closed.is_set():
                try:
                    payload, peer = self.sock.recvfrom(65535)
                except socket.timeout:
                    continue
                self.ingest(payload, peer, time.monotonic())
        except BaseException as exc:
            if not self.closed.is_set():
                self.recorder.fail("telemetry receiver failed: " + str(exc))

    def sample(self):
        with self.lock:
            latest = self.latest
        if latest is None:
            return None
        now, message = latest
        return dict(message, transport_age=max(0., time.monotonic()-now))

    def close(self):
        self.closed.set()
        self.thread.join(timeout=.5)
        self.sock.close()
