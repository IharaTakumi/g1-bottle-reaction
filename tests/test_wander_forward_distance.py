from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from robot_side.wander_forward_distance import ForwardDistancePlan, run_forward_distance


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "g1_wander_forward_distance", ROOT / "scripts/g1-wander-forward-distance.py"
)
SCRIPT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SCRIPT)


class FakeClient:
    def __init__(self, results=None):
        self.results = list(results or [0] * 100)
        self.calls = []

    def SetVelocity(self, vx, vy, omega, duration):
        self.calls.append(("SetVelocity", vx, vy, omega, duration))
        return self.results.pop(0)

    def StopMove(self):
        self.calls.append(("StopMove",))
        return 0


class FakeTelemetry:
    def __init__(self, poses):
        self.poses = list(poses)
        self.index = 0

    def _sample(self):
        x, y, yaw = self.poses[min(self.index, len(self.poses) - 1)]
        return {"odom": {"x": x, "y": y, "yaw": yaw}, "odom_age_s": 0.0,
                "cloud_age_s": 0.0, "cloud_valid": True,
                "cloud_invalid_reason": None, "forward_clearance_m": 3.0}

    def wait_ready(self, timeout):
        del timeout
        return self._sample()

    def latest(self):
        sample = self._sample()
        self.index += 1
        return sample


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def test_cli_defaults_to_dry_run_without_runtime(capsys):
    assert SCRIPT.main([]) == 0
    assert "NO G1 COMMAND SENT" in capsys.readouterr().out


def test_reaches_target_with_bounded_pulses_and_one_stop():
    clock = FakeClock()
    telemetry = FakeTelemetry([(0.0, 0.0, 0.0)] * 3 + [(0.5, 0.0, 0.0)] * 30
                              + [(1.0, 0.0, 0.0)])
    client = FakeClient()
    result = run_forward_distance(
        ForwardDistancePlan(), telemetry, client, lambda: [],
        clock=clock, sleep=clock.sleep)
    assert result["status"] == "pass"
    assert client.calls[-1] == ("StopMove",)
    assert client.calls.count(("StopMove",)) == 1
    assert all(call[4] <= 0.5 for call in client.calls if call[0] == "SetVelocity")


def test_rpc_failure_stops_without_retry():
    clock = FakeClock()
    client = FakeClient([3104])
    result = run_forward_distance(
        ForwardDistancePlan(), FakeTelemetry([(0.0, 0.0, 0.0)] * 20),
        client, lambda: [], clock=clock, sleep=clock.sleep)
    assert result["status"] == "fail"
    assert [call[0] for call in client.calls] == ["SetVelocity", "StopMove"]
    assert "3104" in result["reason"]


@pytest.mark.parametrize("field,value", [
    ("speed_m_s", 0.101), ("pulse_duration_s", 0.501),
    ("target_distance_m", 1.01), ("hard_cap_m", 1.21),
    ("wall_clock_cap_s", 15.1), ("minimum_start_clearance_m", 1.99),
])
def test_hard_limits_reject_out_of_range(field, value):
    values = ForwardDistancePlan().__dict__.copy()
    values[field] = value
    with pytest.raises(ValueError):
        ForwardDistancePlan(**values).validate()


def test_stale_or_conflicting_state_sends_no_velocity():
    clock = FakeClock()
    telemetry = FakeTelemetry([(0.0, 0.0, 0.0)] * 20)
    client = FakeClient()
    result = run_forward_distance(
        ForwardDistancePlan(), telemetry, client, lambda: ["writer"],
        clock=clock, sleep=clock.sleep)
    assert result["status"] == "fail"
    assert client.calls == []


def test_two_meter_start_clearance_is_required():
    telemetry = FakeTelemetry([(0.0, 0.0, 0.0)])
    sample = telemetry._sample()
    sample["forward_clearance_m"] = 1.99
    telemetry.wait_ready = lambda timeout: sample
    client = FakeClient()
    with pytest.raises(RuntimeError, match="preflight forward clearance"):
        run_forward_distance(ForwardDistancePlan(), telemetry, client, lambda: [])
    assert client.calls == []
