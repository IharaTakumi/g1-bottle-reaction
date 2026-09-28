"""Optional-input and post-STOP lifecycle tests: local relay/fake SDK only."""
import io
import json
import threading
import time

import pytest

from test_stationary_harness import harness, FakeTelemetry, ClearGuard, args, read_records, run_motion
from test_stop_transactions import rpc_relay
from scripts.analyze_stationary_calibration import summarize


class BrokenInput:
    def readline(self):
        raise OSError("terminal unavailable")


class BrokenParserInput:
    def readline(self):
        return object()  # Cannot parse .strip(); not a recorder/output failure.


@pytest.mark.parametrize("stream", [io.StringIO(""), None, BrokenInput(), BrokenParserInput()])
def test_repeated_optional_input_failures_only_annotate(tmp_path, stream):
    recorder = harness.Recorder(tmp_path/"marker", "test", "marker", {"pc_clock_id": "test"})
    try:
        for _ in range(3):
            harness.read_operator_markers(recorder, threading.Event(), stream)
            recorder.check()
        recorder.barrier(1)
    finally:
        recorder.close()
    rows = [json.loads(line) for line in (tmp_path/"marker/canonical.jsonl").read_text().splitlines()]
    assert sum(r.get("operator_marker_status") == "unavailable" for r in rows) == 3
    assert not recorder.failed.is_set()
    assert not any(r.get("event", "").startswith(("STOP", "MOVE", "TRIAL_FAILURE")) for r in rows)


def test_normal_marker_then_eof_standing_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.sys, "stdin", io.StringIO("m\n"))
    a = args(tmp_path); a.stdin_markers = True
    def forbidden(*a, **kw): raise AssertionError("standing constructed control path")
    assert harness.run(a, telemetry_factory=FakeTelemetry, adapter_factory=forbidden,
                       controller_factory=forbidden, guard_factory=forbidden) == 0
    rows = read_records(tmp_path)
    assert any(r.get("event") == "OPERATOR_EXTERNAL_STATIONARY_MARK" for r in rows)
    assert any(r.get("operator_marker_status") == "unavailable" for r in rows)
    assert rows[-1]["event"] == "TRIAL_END"


@pytest.mark.parametrize("when", ["before-motion", "during-motion"])
def test_marker_failure_does_not_mutate_finite_control(tmp_path, monkeypatch, rpc_relay, when):
    relay, sdk = rpc_relay; relay.sdk = sdk
    moving = threading.Event()
    class NotifyAdapter(harness.CalibrationAdapter):
        def move(self, vx, vyaw=0):
            result = super().move(vx, vyaw)
            moving.set()
            return result
    class Input:
        def readline(self):
            if when == "before-motion": return ""
            assert moving.wait(3)
            raise OSError("input thread error during motion")
    monkeypatch.setattr(harness.sys, "stdin", Input())
    assert run_motion(tmp_path, monkeypatch, relay, adapter_factory=NotifyAdapter,
                      args_changes={"stdin_markers": True}) == 0
    rows = read_records(tmp_path)
    assert any(r.get("operator_marker_status") == "unavailable" for r in rows)
    assert len(sdk.calls) == 3
    assert not any(r.get("event") == "TRIAL_FAILURE" for r in rows)


class PostTelemetry(FakeTelemetry):
    """Emit held telemetry independently of controller reads, like UDP reception."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.poll)
        self.thread.start()

    def poll(self):
        while not self.closed.wait(.02):
            if self.recorder.phase == "POST_STOP_OBSERVATION":
                self.sample()

    def close(self):
        self.closed.set(); self.thread.join(1)
        assert not self.thread.is_alive()


@pytest.mark.parametrize("mode", ["forward-stop", "turn-stop"])
def test_completion_race_and_observation_longer_than_lease(tmp_path, monkeypatch, rpc_relay, mode):
    relay, sdk = rpc_relay; relay.sdk = sdk
    at_return, heartbeat_seen = threading.Event(), threading.Event()
    observations, faults, heartbeat_threads = [], [], []
    class Controller(harness.PatrolController):
        def run_calibration_leg(self, kind):
            super().run_calibration_leg(kind)
            at_return.set()
            # Force the reviewed race window; no dependence on scheduler luck.
            assert heartbeat_seen.wait(2)

        def heartbeat(self, lease_id):
            try:
                result = super().heartbeat(lease_id)
                heartbeat_threads.append(threading.get_ident())
                if self.locomotion.recorder.phase == "POST_STOP_OBSERVATION":
                    observations.append((result["lease_active"], self._paused.is_set(),
                                         relay.owner.state, len(sdk.moves), len(sdk.calls), relay.owner.session))
                return result
            finally:
                if at_return.is_set(): heartbeat_seen.set()

        def control_fault(self, reason):
            faults.append(reason)
            return super().control_fault(reason)

    assert run_motion(tmp_path, monkeypatch, relay, mode,
        args_changes={"post_seconds": .7}, controller_factory=Controller,
        telemetry_factory=lambda *a: PostTelemetry(*a, relay=relay, mode=mode)) == 0
    assert observations and faults == []
    assert set(heartbeat_threads) == {threading.get_ident()}  # No renewing background thread.
    assert all(active and paused and state == "MOVEMENT_HELD" for active, paused, state, *_ in observations)
    assert len({row[3] for row in observations}) == 1  # No Move during observation.
    assert {row[4] for row in observations} == {2}  # Cleanup STOP happens afterward.
    assert len({row[5] for row in observations}) == 1 and observations[0][5] is not None
    rows = read_records(tmp_path)
    events = [r.get("event") for r in rows]
    ordered = ["POST_STOP_OBSERVATION", "POST_STOP_OBSERVATION_COMPLETE", "CONTROLLED_TEARDOWN", "TRIAL_END"]
    assert [events.index(e) for e in ordered] == sorted(events.index(e) for e in ordered)
    post = next(r for r in rows if r.get("event") == "POST_STOP_OBSERVATION")
    end = next(r for r in rows if r.get("event") == "POST_STOP_OBSERVATION_COMPLETE")
    assert end["pc_receive_monotonic_s"] - post["pc_receive_monotonic_s"] >= .7
    assert any(r["record_type"] == "sample" and r["phase"] == "POST_STOP_OBSERVATION" for r in rows)
    cleanup = next(r for r in rows if r.get("event") == "STOP_REQUESTED" and r["transport"]["command_scope"] == "cleanup")
    assert cleanup["pc_receive_monotonic_s"] > end["pc_receive_monotonic_s"]
    assert summarize(rows)["trials"][0]["stop_confirm_marker_count"] == 1


def test_main_hang_during_post_stop_expires_lease(tmp_path, monkeypatch, rpc_relay):
    relay, sdk = rpc_relay; relay.sdk = sdk
    expired = threading.Event()
    class Controller(harness.PatrolController):
        def control_fault(self, reason):
            try: return super().control_fault(reason)
            finally:
                if reason == "control lease expired": expired.set()
    class MainHangRecorder(harness.Recorder):
        def barrier(self, timeout):
            if self.phase == "POST_STOP_OBSERVATION":
                # Block main progress, not writer/lease checker. Only the existing
                # lease expiry can release this deterministic injected hang.
                assert expired.wait(3)
            return super().barrier(timeout)
    with pytest.raises(RuntimeError, match="lease expired"):
        run_motion(tmp_path, monkeypatch, relay, recorder_factory=MainHangRecorder,
                   controller_factory=Controller, args_changes={"post_seconds": .7})
    assert expired.is_set() and relay.owner.state == "MOVEMENT_HELD"
    events = [r.get("event") for r in read_records(tmp_path)]
    assert "POST_STOP_OBSERVATION_COMPLETE" not in events and "TRIAL_END" not in events


@pytest.mark.parametrize("at", ["POST_STOP_OBSERVATION", "POST_STOP_OBSERVATION_COMPLETE"])
def test_required_recording_failure_still_aborts(tmp_path, monkeypatch, rpc_relay, at):
    relay, sdk = rpc_relay; relay.sdk = sdk
    class FailedRecorder(harness.Recorder):
        def _write(self, record):
            if record.get("event") == at: raise OSError("fatal observation write failure")
            return super()._write(record)
    with pytest.raises(RuntimeError):
        run_motion(tmp_path, monkeypatch, relay, recorder_factory=FailedRecorder,
                   args_changes={"post_seconds": .7})
    assert relay.owner.state == "MOVEMENT_HELD" and len(sdk.calls) >= 3
    assert "TRIAL_END" not in [r.get("event") for r in read_records(tmp_path)]


@pytest.mark.parametrize("failure", ["patrol", "adapter", "telemetry", "recorder"])
def test_teardown_exceptions_do_not_skip_other_cleanup(tmp_path, monkeypatch, rpc_relay, failure):
    relay, sdk = rpc_relay; relay.sdk = sdk
    closed = []
    class Controller(harness.PatrolController):
        def stop(self):
            if self.locomotion.recorder.scope == "cleanup" and failure == "patrol":
                raise OSError("Patrol teardown failed")
            return super().stop()
    class Adapter(harness.CalibrationAdapter):
        def close(self):
            super().close(); closed.append("adapter")
            if failure == "adapter": raise OSError("adapter close failed")
    class Telemetry(FakeTelemetry):
        def close(self):
            closed.append("telemetry")
            if failure == "telemetry": raise OSError("receiver close failed")
    class Recorder(harness.Recorder):
        def close(self):
            try: super().close()
            finally: closed.append("recorder")
            if failure == "recorder": raise OSError("recorder close failed")
    with pytest.raises(OSError):
        run_motion(tmp_path, monkeypatch, relay, controller_factory=Controller,
            adapter_factory=Adapter, recorder_factory=Recorder,
            telemetry_factory=lambda *a: Telemetry(*a, relay=relay, mode="forward-stop"))
    assert closed == ["adapter", "telemetry", "recorder"]
    assert relay.owner.state == "MOVEMENT_HELD"
    if failure != "recorder":
        assert "TRIAL_END" not in [r.get("event") for r in read_records(tmp_path)]
