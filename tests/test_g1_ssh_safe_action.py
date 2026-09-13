from __future__ import annotations

import json
import subprocess
import threading

import pytest

from g1_bottle_reaction.adapters.g1_robot import g1_local_safe_action_main
from g1_bottle_reaction.adapters.g1_ssh_safe_action import (
    RESULT_PREFIX,
    SshG1SafeActionAdapter,
    parse_safe_action_result,
)


CHANNEL_XML = """<CycloneDDS><Domain><Tracing><Verbosity>config</Verbosity></Tracing></Domain></CycloneDDS>"""


class FakeChannel:
    ChannelConfigHasInterface = CHANNEL_XML

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def ChannelFactoryInitialize(self, domain: int, interface: str) -> None:
        assert "Tracing" not in self.ChannelConfigHasInterface
        self.calls.append((domain, interface))


class FakeActionClient:
    def __init__(
        self,
        *,
        actions: tuple[tuple[int, str], ...] = (
            (23, "right_hand_up"),
            (99, "release_arm"),
        ),
        results: dict[int, object] | None = None,
        on_execute=None,
    ) -> None:
        self.actions = actions
        self.results = results or {}
        self.on_execute = on_execute
        self.timeout = None
        self.init_calls = 0
        self.list_calls = 0
        self.execute_calls: list[int] = []

    def SetTimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def Init(self) -> None:
        self.init_calls += 1

    def GetActionList(self):
        self.list_calls += 1
        return 0, [[{"id": action_id, "name": name} for action_id, name in self.actions]]

    def ExecuteAction(self, action_id: int):
        self.execute_calls.append(action_id)
        if self.on_execute is not None:
            self.on_execute(action_id)
        value = self.results.get(action_id, 0)
        if isinstance(value, Exception):
            raise value
        return value


def run_helper(argv, client, **kwargs):
    channel = FakeChannel()
    lines: list[str] = []
    result = g1_local_safe_action_main(
        argv,
        dependencies=(channel, lambda: client),
        environ=kwargs.pop("environ", {}),
        output=lines.append,
        install_signal_handlers=False,
        **kwargs,
    )
    assert FakeChannel.ChannelConfigHasInterface == CHANNEL_XML
    assert len(lines) == 1 and lines[0].startswith(RESULT_PREFIX)
    assert json.loads(lines[0][len(RESULT_PREFIX) :]) == result
    return result, channel


def test_k_helper_probe_never_executes_action() -> None:
    client = FakeActionClient()
    result, channel = run_helper(["probe"], client)
    assert result["ok"]
    assert result["action_list_ok"]
    assert channel.calls == [(0, "eth0")]
    assert client.timeout == 10.0
    assert client.init_calls == 1
    assert client.list_calls == 1
    assert client.execute_calls == []


@pytest.mark.parametrize(
    ("argv", "environ"),
    [
        (["notice"], {"G1_ALLOW_REAL_ACTION": "1"}),
        (["notice", "--execute-real-action"], {}),
    ],
)
def test_l_notice_requires_both_gates(argv, environ) -> None:
    client = FakeActionClient()
    result, channel = run_helper(argv, client, environ=environ)
    assert not result["ok"]
    assert "requires both" in result["error"]
    assert channel.calls == []
    assert client.execute_calls == []


def test_m_helper_has_no_external_action_id_argument() -> None:
    client = FakeActionClient()
    with pytest.raises(SystemExit):
        run_helper(
            ["notice", "--execute-real-action", "--action", "23"],
            client,
            environ={"G1_ALLOW_REAL_ACTION": "1"},
        )
    assert client.execute_calls == []


@pytest.mark.parametrize(
    "actions",
    [((99, "release_arm"),), ((23, "right_hand_up"),)],
)
def test_n_missing_verified_action_is_safe_no_op(actions) -> None:
    client = FakeActionClient(actions=actions)
    result, _ = run_helper(
        ["notice", "--execute-real-action"],
        client,
        environ={"G1_ALLOW_REAL_ACTION": "1"},
    )
    assert not result["ok"]
    assert not result["action_list_ok"]
    assert client.list_calls == 1
    assert client.execute_calls == []


def test_o_normal_mocked_notice_is_23_hold_99_once() -> None:
    client = FakeActionClient()
    waits: list[float] = []
    result, _ = run_helper(
        ["notice", "--execute-real-action"],
        client,
        environ={"G1_ALLOW_REAL_ACTION": "1"},
        hold_wait=lambda seconds: waits.append(seconds) or False,
    )
    assert result["ok"]
    assert client.list_calls == 1
    assert client.execute_calls == [23, 99]
    assert waits == [2.0]
    assert not result["motion_uncertain"]


@pytest.mark.parametrize(
    "failure",
    [3104, RuntimeError("simulated Action 23 timeout")],
)
def test_p_uncertain_action_23_always_attempts_one_release_without_retry(failure) -> None:
    client = FakeActionClient(results={23: failure})
    result, _ = run_helper(
        ["notice", "--execute-real-action"],
        client,
        environ={"G1_ALLOW_REAL_ACTION": "1"},
    )
    assert not result["ok"]
    assert result["motion_uncertain"]
    assert result["action23_invoked"]
    assert client.execute_calls == [23, 99]


def test_q_shutdown_before_action_23_sends_no_action() -> None:
    stopped = threading.Event()
    stopped.set()
    client = FakeActionClient()
    result, _ = run_helper(
        ["notice", "--execute-real-action"],
        client,
        environ={"G1_ALLOW_REAL_ACTION": "1"},
        shutdown_event=stopped,
    )
    assert not result["ok"]
    assert client.execute_calls == []


def test_r_shutdown_after_action_23_allows_only_one_release() -> None:
    stopped = threading.Event()

    def stop_after_23(action_id):
        if action_id == 23:
            stopped.set()

    client = FakeActionClient(on_execute=stop_after_23)
    result, _ = run_helper(
        ["notice", "--execute-real-action"],
        client,
        environ={"G1_ALLOW_REAL_ACTION": "1"},
        shutdown_event=stopped,
    )
    assert not result["ok"]
    assert "shutdown requested" in result["error"]
    assert client.execute_calls == [23, 99]


class FakeProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def communicate(self, timeout=None):
        del timeout
        return self.stdout, self.stderr

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        del timeout
        return self.returncode


def helper_line(**updates) -> str:
    result = {
        "ok": True,
        "operation": "notice",
        "dds_initialized": True,
        "action_list_ok": True,
        "action23_available": True,
        "action99_available": True,
        "action23_invoked": True,
        "action23_code": 0,
        "action99_invoked": True,
        "action99_code": 0,
        "motion_uncertain": False,
        "error": "",
    }
    result.update(updates)
    return RESULT_PREFIX + json.dumps(result) + "\n"


def test_s_ssh_failure_latches_motion_for_process_lifetime() -> None:
    processes = []

    def popen(command, **kwargs):
        del command, kwargs
        process = FakeProcess(
            helper_line(ok=False, error="3102", action23_invoked=False),
            returncode=2,
        )
        processes.append(process)
        return process

    adapter = SshG1SafeActionAdapter(
        "g1",
        enabled=True,
        motion_mode="safe-actions",
        execute_real_action=True,
        environ={"G1_ALLOW_REAL_ACTION": "1"},
        popen_factory=popen,
    )
    with pytest.raises(RuntimeError, match="3102"):
        adapter.play_motion("notice")
    adapter.play_motion("notice")
    assert len(processes) == 1
    assert "3102" in adapter.motion_disabled_reason


def test_t_machine_result_parser_ignores_sdk_logs() -> None:
    expected = {"ok": True, "operation": "probe"}
    output = (
        "CycloneDDS config message\n"
        + RESULT_PREFIX
        + json.dumps(expected)
        + "\nSDK trailing log\n"
    )
    assert parse_safe_action_result(output) == expected
    with pytest.raises(RuntimeError, match="exactly one"):
        parse_safe_action_result("SDK log only")


def test_pc_shutdown_never_kills_an_in_flight_helper_that_finishes_bounded() -> None:
    entered = threading.Event()
    finish = threading.Event()
    processes = []

    class BlockingProcess(FakeProcess):
        def communicate(self, timeout=None):
            del timeout
            entered.set()
            assert finish.wait(timeout=2)
            return self.stdout, self.stderr

        def poll(self):
            return None if not finish.is_set() else self.returncode

    def popen(command, **kwargs):
        del command, kwargs
        process = BlockingProcess(helper_line())
        processes.append(process)
        return process

    adapter = SshG1SafeActionAdapter(
        "g1",
        enabled=True,
        motion_mode="safe-actions",
        execute_real_action=True,
        environ={"G1_ALLOW_REAL_ACTION": "1"},
        popen_factory=popen,
    )
    worker = threading.Thread(target=adapter.play_motion, args=("notice",))
    worker.start()
    assert entered.wait(timeout=1)
    adapter.request_shutdown()
    finish.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert not processes[0].terminated
    assert not processes[0].killed
    adapter.play_motion("notice")
    assert len(processes) == 1
