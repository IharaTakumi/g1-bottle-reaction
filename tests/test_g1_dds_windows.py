from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g1_bottle_reaction.adapters.g1_robot import (
    WINDOWS_CHANNEL_CONFIG,
    UnitreeSdkRuntime,
    resolve_windows_interface_ipv4,
)
from g1_bottle_reaction.main import build_parser


class UnusedClient:
    pass


def _symbols(initializer):
    return initializer, UnusedClient


def test_windows_interface_alias_resolves_to_ipv4() -> None:
    payload = [
        {
            "InterfaceAlias": "Wi-Fi",
            "IPAddress": "192.0.2.9",
            "AddressState": "Preferred",
            "SkipAsSource": False,
        },
        {
            "InterfaceAlias": "イーサネット 3",
            "IPAddress": "192.168.123.222",
            "AddressState": "Preferred",
            "SkipAsSource": False,
        },
    ]

    def runner(command, **kwargs):
        assert command[0] == "powershell.exe"
        assert kwargs["timeout"] == 10
        return SimpleNamespace(stdout=json.dumps(payload, ensure_ascii=False))

    assert (
        resolve_windows_interface_ipv4("イーサネット 3", runner=runner)
        == "192.168.123.222"
    )


def test_explicit_network_address_bypasses_alias_resolution() -> None:
    calls: list[tuple[int, str]] = []
    channel_module = SimpleNamespace(ChannelConfigHasInterface="ORIGINAL")
    runtime = UnitreeSdkRuntime(
        lambda: _symbols(lambda domain, value: calls.append((domain, value))),
        channel_module_loader=lambda: channel_module,
        interface_resolver=lambda alias: (_ for _ in ()).throw(
            AssertionError(f"resolver called for {alias}")
        ),
        platform_name="win32",
    )
    runtime.initialize_channel(None, "192.168.123.222")
    assert calls == [(0, "192.168.123.222")]


def test_windows_channel_config_uses_address_without_linux_trace_path() -> None:
    assert 'address="$__IF_NAME__$"' in WINDOWS_CHANNEL_CONFIG
    assert "name=\"$__IF_NAME__$\"" not in WINDOWS_CHANNEL_CONFIG
    assert "/tmp/cdds.LOG" not in WINDOWS_CHANNEL_CONFIG
    assert "<Tracing>" not in WINDOWS_CHANNEL_CONFIG


def test_windows_config_is_temporary_and_initializer_receives_address() -> None:
    original = (
        '<NetworkInterface name="$__IF_NAME__$"/>'
        "<Tracing><OutputFile>/tmp/cdds.LOG</OutputFile></Tracing>"
    )
    channel_module = SimpleNamespace(ChannelConfigHasInterface=original)
    observed: list[tuple[int, str, str]] = []

    def initializer(domain: int, value: str) -> None:
        observed.append((domain, value, channel_module.ChannelConfigHasInterface))

    runtime = UnitreeSdkRuntime(
        lambda: _symbols(initializer),
        channel_module_loader=lambda: channel_module,
        interface_resolver=lambda alias: "192.168.123.222",
        platform_name="win32",
    )
    runtime.initialize_channel("イーサネット 3")
    assert observed == [(0, "192.168.123.222", WINDOWS_CHANNEL_CONFIG)]
    assert channel_module.ChannelConfigHasInterface == original


def test_windows_config_is_restored_when_initializer_fails() -> None:
    channel_module = SimpleNamespace(ChannelConfigHasInterface="ORIGINAL")

    def fail(domain: int, value: str) -> None:
        raise Exception("channel factory init error")

    runtime = UnitreeSdkRuntime(
        lambda: _symbols(fail),
        channel_module_loader=lambda: channel_module,
        platform_name="win32",
    )
    with pytest.raises(RuntimeError, match="Windows IPv4 192.168.123.222"):
        runtime.initialize_channel(None, "192.168.123.222")
    assert channel_module.ChannelConfigHasInterface == "ORIGINAL"


def test_linux_keeps_official_name_based_initializer_path() -> None:
    calls: list[tuple[int, str]] = []
    runtime = UnitreeSdkRuntime(
        lambda: _symbols(lambda domain, value: calls.append((domain, value))),
        channel_module_loader=lambda: (_ for _ in ()).throw(
            AssertionError("Linux must not patch ChannelConfigHasInterface")
        ),
        platform_name="linux",
    )
    runtime.initialize_channel("eth0")
    assert calls == [(0, "eth0")]


def test_network_address_cli_and_validation() -> None:
    args = build_parser().parse_args(
        ["--g1-test", "connection", "--network-address", "192.168.123.222"]
    )
    assert args.network_address == "192.168.123.222"
    runtime = UnitreeSdkRuntime(
        lambda: _symbols(lambda domain, value: None),
        channel_module_loader=lambda: SimpleNamespace(
            ChannelConfigHasInterface="ORIGINAL"
        ),
        platform_name="win32",
    )
    with pytest.raises(ValueError, match="Invalid IPv4"):
        runtime.initialize_channel(None, "not-an-address")


def test_video_client_uses_windows_address_compatibility_path() -> None:
    initialized: list[tuple[int, str]] = []
    channel_module = SimpleNamespace(ChannelConfigHasInterface="ORIGINAL")

    class FakeVideoClient:
        def __init__(self) -> None:
            self.timeout = None
            self.inited = False

        def SetTimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def Init(self) -> None:
            self.inited = True

    runtime = UnitreeSdkRuntime(
        lambda: _symbols(
            lambda domain, value: initialized.append((domain, value))
        ),
        channel_module_loader=lambda: channel_module,
        video_client_loader=lambda: FakeVideoClient,
        platform_name="win32",
    )
    client = runtime.create_video_client(None, 2.5, "192.168.123.222")
    assert initialized == [(0, "192.168.123.222")]
    assert client.timeout == 2.5
    assert client.inited
    assert channel_module.ChannelConfigHasInterface == "ORIGINAL"


def test_arm_action_client_uses_windows_address_compatibility_path() -> None:
    initialized: list[tuple[int, str]] = []
    channel_module = SimpleNamespace(ChannelConfigHasInterface="ORIGINAL")

    class FakeArmActionClient:
        def __init__(self) -> None:
            self.timeout = None
            self.inited = False

        def SetTimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def Init(self) -> None:
            self.inited = True

    runtime = UnitreeSdkRuntime(
        lambda: _symbols(
            lambda domain, value: initialized.append((domain, value))
        ),
        channel_module_loader=lambda: channel_module,
        arm_action_client_loader=lambda: FakeArmActionClient,
        platform_name="win32",
    )
    client = runtime.create_arm_action_client(
        None, 10.0, "192.168.123.222"
    )
    assert initialized == [(0, "192.168.123.222")]
    assert client.timeout == 10.0
    assert client.inited
    assert channel_module.ChannelConfigHasInterface == "ORIGINAL"


def test_arm_sdk_transport_uses_windows_address_compatibility_path() -> None:
    initialized: list[tuple[int, str]] = []
    channel_module = SimpleNamespace(ChannelConfigHasInterface="ORIGINAL")
    transport = object()
    runtime = UnitreeSdkRuntime(
        lambda: _symbols(
            lambda domain, value: initialized.append((domain, value))
        ),
        channel_module_loader=lambda: channel_module,
        arm_sdk_transport_loader=lambda: transport,
        platform_name="win32",
    )
    assert (
        runtime.create_arm_sdk_transport(None, "192.168.123.222")
        is transport
    )
    assert initialized == [(0, "192.168.123.222")]
    assert channel_module.ChannelConfigHasInterface == "ORIGINAL"
