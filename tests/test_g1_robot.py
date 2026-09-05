from __future__ import annotations

import importlib
import logging

import pytest

from g1_bottle_reaction.adapters.g1_robot import (
    ARM_ACTION_RPC_TIMEOUT_CODE,
    G1RobotAdapter,
    extract_action_ids,
)
from g1_bottle_reaction.adapters.robot import TrackingCommand
from g1_bottle_reaction.custom_motion import load_custom_motion_config
from g1_bottle_reaction.main import build_parser


class FakeArmActionClient:
    def __init__(
        self,
        *,
        action_ids=(23, 26, 99),
        results: dict[int, list[int]] | None = None,
        list_code: int = 0,
    ) -> None:
        self.action_ids = action_ids
        self.results = results or {}
        self.list_code = list_code
        self.list_calls = 0
        self.execute_calls: list[int] = []

    def GetActionList(self):
        self.list_calls += 1
        return self.list_code, {
            "actions": [{"id": item} for item in self.action_ids]
        }

    def ExecuteAction(self, action_id: int) -> int:
        self.execute_calls.append(action_id)
        action_results = self.results.get(action_id, [0])
        return action_results.pop(0)


class FakeRuntime:
    def __init__(self, client: FakeArmActionClient | None = None) -> None:
        self.client = client or FakeArmActionClient()
        self.create_calls: list[tuple] = []

    def create_arm_action_client(self, *args):
        self.create_calls.append(args)
        return self.client


def _enabled_adapter(runtime: FakeRuntime, **kwargs) -> G1RobotAdapter:
    return G1RobotAdapter(
        "eth-test",
        enabled=True,
        motion_mode="safe-actions",
        runtime=runtime,
        sleeper=lambda seconds: None,
        **kwargs,
    )


def test_real_motion_requires_explicit_gate() -> None:
    with pytest.raises(RuntimeError, match="both --robot g1"):
        G1RobotAdapter("eth0", enabled=False, motion_mode="safe-actions")


def test_arm_client_default_timeout_and_action_list_check() -> None:
    runtime = FakeRuntime()
    adapter = _enabled_adapter(runtime)
    adapter.initialize()
    assert runtime.create_calls == [("eth-test", 10.0)]
    assert runtime.client.list_calls == 1


def test_verified_motion_mapping_and_required_release() -> None:
    runtime = FakeRuntime()
    sleeps: list[float] = []
    adapter = G1RobotAdapter(
        "eth-test",
        enabled=True,
        motion_mode="safe-actions",
        timeout_seconds=10.0,
        release_delay_seconds=2.0,
        runtime=runtime,
        sleeper=sleeps.append,
    )
    adapter.initialize()
    adapter.play_motion("notice")
    assert adapter.wait_for_motion_complete("notice", timeout=0.1)
    adapter.play_motion("spot_target")
    assert not adapter.wait_for_motion_complete("spot_target", timeout=0.1)
    adapter.play_motion("guard")
    adapter.play_motion("little_dance")
    assert runtime.client.execute_calls == [23, 99, 26]
    assert sleeps == [2.0]


def test_explicit_network_address_is_forwarded() -> None:
    runtime = FakeRuntime()
    adapter = G1RobotAdapter(
        "",
        network_address="192.168.123.222",
        enabled=True,
        motion_mode="safe-actions",
        runtime=runtime,
    )
    adapter.initialize()
    assert runtime.create_calls == [("", 10.0, "192.168.123.222")]


def test_missing_action_or_release_id_is_safe_no_op(caplog) -> None:
    runtime = FakeRuntime(FakeArmActionClient(action_ids=(23, 26)))
    adapter = _enabled_adapter(runtime)
    with caplog.at_level(logging.WARNING):
        adapter.initialize()
        adapter.play_motion("notice")
        adapter.play_motion("spot_target")
    assert runtime.client.execute_calls == [26]
    assert "Action IDs 99 are unavailable" in caplog.text


def test_execute_action_3104_warns_without_retry(caplog) -> None:
    runtime = FakeRuntime(
        FakeArmActionClient(results={26: [ARM_ACTION_RPC_TIMEOUT_CODE]})
    )
    adapter = _enabled_adapter(runtime)
    adapter.initialize()
    with caplog.at_level(logging.WARNING):
        adapter.play_motion("spot_target")
    assert runtime.client.execute_calls == [26]
    assert "may have started" in caplog.text
    assert "will not be retried" in caplog.text


def test_timed_out_releasable_action_is_still_released_once(caplog) -> None:
    runtime = FakeRuntime(
        FakeArmActionClient(results={23: [ARM_ACTION_RPC_TIMEOUT_CODE]})
    )
    adapter = _enabled_adapter(runtime)
    adapter.initialize()
    with caplog.at_level(logging.WARNING):
        adapter.play_motion("notice")
    assert runtime.client.execute_calls == [23, 99]
    assert not adapter.wait_for_motion_complete("notice", timeout=0.1)


def test_other_execute_failure_is_reported() -> None:
    runtime = FakeRuntime(FakeArmActionClient(results={26: [1234]}))
    adapter = _enabled_adapter(runtime)
    adapter.initialize()
    with pytest.raises(
        RuntimeError, match=r"ExecuteAction\(26, high wave\).+1234"
    ):
        adapter.play_motion("spot_target")
    assert runtime.client.execute_calls == [26]


def test_action_list_failure_prevents_motion_startup() -> None:
    runtime = FakeRuntime(FakeArmActionClient(list_code=1001))
    adapter = _enabled_adapter(runtime)
    with pytest.raises(RuntimeError, match="GetActionList failed"):
        adapter.initialize()
    assert runtime.client.execute_calls == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([23, 26, 99], {23, 26, 99}),
        (
            {"actionList": [{"actionId": 23}, {"action_id": 26}]},
            {23, 26},
        ),
        ({"right hand up": 23, "high wave": 26}, {23, 26}),
    ],
)
def test_action_id_extraction(payload, expected) -> None:
    assert extract_action_ids(payload) == expected


def test_disabled_motion_and_tracking_are_no_ops() -> None:
    runtime = FakeRuntime()
    adapter = G1RobotAdapter("", motion_mode="disabled", runtime=runtime)
    adapter.initialize()
    adapter.play_motion("notice")
    adapter.apply_tracking(
        TrackingCommand(False, False, 0, 0, 0, "UNAWARE", "idle", 0, None)
    )
    assert runtime.create_calls == []
    assert runtime.client.execute_calls == []


def test_g1_cli_and_import_are_hardware_independent() -> None:
    args = build_parser().parse_args(
        [
            "--g1-test",
            "wave",
            "--robot",
            "g1",
            "--enable-real-robot",
            "--g1-motion",
            "safe-actions",
            "--network-interface",
            "eth0",
        ]
    )
    assert args.g1_test == "wave"
    assert args.enable_real_robot
    module = importlib.import_module("g1_bottle_reaction.adapters.g1_robot")
    assert module.G1RobotAdapter is not None


def test_custom_notice_requires_opt_in_and_dispatches_controller() -> None:
    class FakeCustomController:
        def __init__(self) -> None:
            self.preflight_calls = 0
            self.starts = []

        def preflight(self):
            self.preflight_calls += 1
            return {}

        def start(self, *args, **kwargs):
            self.starts.append((args, kwargs))
            return True

        def wait(self, timeout=None):
            del timeout
            return True

        def close(self):
            pass

    runtime = FakeRuntime()
    controller = FakeCustomController()
    adapter = G1RobotAdapter(
        "eth-test",
        enabled=True,
        motion_mode="safe-actions",
        custom_motion_enabled=True,
        custom_motion_amplitude="small",
        custom_motion_config=load_custom_motion_config(),
        custom_controller=controller,
        runtime=runtime,
    )
    adapter.initialize()
    assert controller.preflight_calls == 1
    assert adapter.play_motion_timed(
        "custom_notice", timeline_start=12.0, timing_debug=True
    )
    assert controller.starts == [
        (
            ("custom_notice", "small"),
            {"timeline_start": 12.0, "timing_debug": True},
        )
    ]
    assert adapter.wait_for_motion_complete("custom_notice", timeout=0.1)


def test_preset_action_is_rejected_while_custom_owns_arm() -> None:
    runtime = FakeRuntime()
    adapter = _enabled_adapter(runtime)
    adapter.initialize()
    assert adapter._ownership.acquire("custom")
    try:
        assert not adapter.play_motion_timed(
            "spot_target", timeline_start=0.0
        )
    finally:
        adapter._ownership.release("custom")
    assert runtime.client.execute_calls == []


def test_custom_notice_rejects_unconfirmed_preset_completion() -> None:
    class FakeCustomController:
        def __init__(self) -> None:
            self.starts = 0

        def preflight(self):
            return {}

        def start(self, *args, **kwargs):
            del args, kwargs
            self.starts += 1
            return True

        def wait(self, timeout=None):
            del timeout
            return True

        def close(self):
            pass

    runtime = FakeRuntime()
    controller = FakeCustomController()
    adapter = G1RobotAdapter(
        "eth-test",
        enabled=True,
        motion_mode="safe-actions",
        custom_motion_enabled=True,
        custom_motion_config=load_custom_motion_config(),
        custom_controller=controller,
        runtime=runtime,
        sleeper=lambda _: None,
    )
    adapter.initialize()
    assert adapter.play_motion_timed("spot_target", timeline_start=0.0)
    assert not adapter.play_motion_timed("custom_notice", timeline_start=1.0)
    assert controller.starts == 0
