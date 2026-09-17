from __future__ import annotations

import importlib.util
from pathlib import Path

from robot_side.wander_reactive_mvp import (
    ReactiveMvpPlan, choose_action, run_reactive_mvp, validate_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "g1_wander_reactive_mvp", ROOT / "scripts/g1-wander-reactive-mvp.py")
SCRIPT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SCRIPT)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeClient:
    def __init__(self, result=0):
        self.calls = []
        self.result = result

    def SetVelocity(self, *args):
        self.calls.append(("SetVelocity",) + args)
        return self.result

    def StopMove(self):
        self.calls.append(("StopMove",))
        return None


class FakeTelemetry:
    def __init__(self, snapshot=None, age=0.0, valid=True):
        self.snapshot = snapshot or {name: 2.0 for name in (
            "left", "front_left", "front", "front_right", "right")}
        self.age = age
        self.valid = valid

    def wait_ready(self, timeout):
        del timeout
        return self.latest()

    def latest(self):
        return {"cloud_age_s": self.age, "cloud_valid": self.valid,
                "cloud_invalid_reason": None,
                "obstacle_snapshot": dict(self.snapshot), "error": None}


class ObstacleAppearsTelemetry(FakeTelemetry):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def latest(self):
        self.reads += 1
        sample = super().latest()
        if self.reads >= 3:
            sample["obstacle_snapshot"]["front"] = 0.5
        return sample


def test_cli_defaults_to_no_command_dry_run(capsys):
    assert SCRIPT.main([]) == 0
    assert "NO G1 COMMAND SENT" in capsys.readouterr().out


def test_duration_cli_selects_bounded_runtime_without_command(capsys):
    assert SCRIPT.main(["--duration", "12"]) == 0
    output = capsys.readouterr().out
    assert "run=12.0" in output
    assert "NO G1 COMMAND SENT" in output


def test_open_front_sends_one_bounded_forward_and_stop():
    clock = FakeClock()
    client = FakeClient()
    result = run_reactive_mvp(
        ReactiveMvpPlan(run_seconds=2, max_pulses=1), FakeTelemetry(), client,
        lambda: [], clock=clock, sleep=clock.sleep)
    assert result["status"] == "pass"
    assert client.calls == [
        ("SetVelocity", 0.20, 0.0, 0.0, 0.50), ("StopMove",)]


def test_blocked_front_never_sends_forward_and_turns_to_clear_side():
    clock = FakeClock()
    client = FakeClient()
    snapshot = {"left": 2.0, "front_left": 1.5, "front": 0.4,
                "front_right": 0.6, "right": 0.7}
    run_reactive_mvp(
        ReactiveMvpPlan(run_seconds=2, max_pulses=1), FakeTelemetry(snapshot),
        client, lambda: [], clock=clock, sleep=clock.sleep)
    assert client.calls[0] == ("SetVelocity", 0.0, 0.0, 0.25, 1.0)
    assert not any(call[0] == "SetVelocity" and call[1] > 0 for call in client.calls)


def test_right_side_selection_has_negative_turn_sign():
    snapshot = {"left": 0.5, "front_left": 0.5, "front": 0.4,
                "front_right": 1.2, "right": 2.0}
    action, _, reason = choose_action(
        snapshot, ReactiveMvpPlan(), __import__("random").Random(1))
    assert (action, reason) == ("TURN_RIGHT_PULSE", "right-clearer")


def test_stale_lidar_fails_closed_without_any_command():
    clock = FakeClock()
    client = FakeClient()
    result = run_reactive_mvp(
        ReactiveMvpPlan(run_seconds=2), FakeTelemetry(age=0.51), client,
        lambda: [], clock=clock, sleep=clock.sleep)
    assert result["status"] == "fail"
    assert "stale" in result["reason"]
    assert client.calls == []


def test_writer_conflict_fails_closed_without_any_command():
    clock = FakeClock()
    client = FakeClient()
    result = run_reactive_mvp(
        ReactiveMvpPlan(run_seconds=2), FakeTelemetry(), client,
        lambda: ["writer"], clock=clock, sleep=clock.sleep)
    assert result["status"] == "fail"
    assert client.calls == []


def test_rpc_error_is_not_retried_and_is_explicitly_stopped():
    clock = FakeClock()
    client = FakeClient(result=3104)
    result = run_reactive_mvp(
        ReactiveMvpPlan(run_seconds=2), FakeTelemetry(), client,
        lambda: [], clock=clock, sleep=clock.sleep)
    assert result["status"] == "fail"
    assert [call[0] for call in client.calls] == ["SetVelocity", "StopMove"]
    assert "3104" in result["reason"]


def test_new_obstacle_interrupts_forward_then_returns_to_observe():
    clock = FakeClock()
    client = FakeClient()
    result = run_reactive_mvp(
        ReactiveMvpPlan(run_seconds=2, max_pulses=1),
        ObstacleAppearsTelemetry(), client, lambda: [],
        clock=clock, sleep=clock.sleep)
    assert result["status"] == "pass"
    assert result["actions"][0]["interrupted_reason"].startswith("obstacle")
    assert client.calls[-1] == ("StopMove",)
