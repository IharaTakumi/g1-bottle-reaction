from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from robot_side.wander_locomotion import (
    MAX_ANGULAR_SPEED_RAD_S,
    MAX_DURATION_S,
    MAX_LINEAR_SPEED_M_S,
    OneShotPlan,
    execute_once,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "g1_wander_loco_once", ROOT / "scripts/g1-wander-loco-once.py"
)
SCRIPT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SCRIPT)


class FakeClient:
    def __init__(self, result=0):
        self.calls = []
        self.result = result

    def SetVelocity(self, vx, vy, omega, duration):
        self.calls.append(("SetVelocity", vx, vy, omega, duration))
        return self.result

    def StopMove(self):
        self.calls.append(("StopMove",))
        return self.result


class FakeRuntime:
    def __init__(self, result=0):
        self.client = FakeClient(result)
        self.create_calls = []

    def create_loco_client(self, interface, timeout):
        self.create_calls.append((interface, timeout))
        return self.client


def test_script_defaults_to_dry_run_without_initializing_sdk(capsys) -> None:
    runtime = FakeRuntime()
    result = SCRIPT.main(["--action", "forward"], runtime=runtime)
    assert result == 0
    assert runtime.create_calls == []
    assert runtime.client.calls == []
    assert "NO G1 COMMAND SENT" in capsys.readouterr().out


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--enable-real-robot"],
        ["--enable-real-robot", "--execute-real-g1"],
        [
            "--enable-real-robot",
            "--execute-real-g1",
            "--i-understand-this-will-move-the-robot",
        ],
        ["--robot", "g1", "--enable-real-robot", "--execute-real-g1"],
    ],
)
def test_every_real_command_gate_is_required(flags, capsys) -> None:
    runtime = FakeRuntime()
    assert SCRIPT.main(["--action", "forward", *flags], runtime=runtime) == 0
    assert runtime.client.calls == []
    assert "DRY RUN" in capsys.readouterr().out


@pytest.mark.parametrize(
    "plan",
    [
        OneShotPlan("forward", MAX_LINEAR_SPEED_M_S + 0.001, 0.1),
        OneShotPlan("turn-left", MAX_ANGULAR_SPEED_RAD_S + 0.001, 0.1),
        OneShotPlan("turn-right", 0.1, MAX_DURATION_S + 0.001),
        OneShotPlan("backward", 0.01, 0.1),
    ],
)
def test_allowlist_and_hard_limits_reject_out_of_range(plan) -> None:
    with pytest.raises(ValueError):
        plan.validate()


@pytest.mark.parametrize(
    "action, expected",
    [
        ("forward", ("SetVelocity", 0.05, 0.0, 0.0, 0.3)),
        ("turn-left", ("SetVelocity", 0.0, 0.0, 0.05, 0.3)),
        ("turn-right", ("SetVelocity", 0.0, 0.0, -0.05, 0.3)),
        ("stop", ("StopMove",)),
    ],
)
def test_armed_path_sends_exactly_one_allowlisted_mutation(action, expected) -> None:
    runtime = FakeRuntime()
    result = execute_once(
        OneShotPlan(action),
        runtime,
        robot="g1",
        enable_real_robot=True,
        execute_real_g1=True,
        understand_motion=True,
    )
    assert result["executed"]
    assert runtime.client.calls == [expected]


def test_rpc_failure_is_returned_without_retry() -> None:
    runtime = FakeRuntime(result=3104)
    result = execute_once(
        OneShotPlan("forward"),
        runtime,
        robot="g1",
        enable_real_robot=True,
        execute_real_g1=True,
        understand_motion=True,
    )
    assert result["result"] == 3104
    assert len(runtime.client.calls) == 1
