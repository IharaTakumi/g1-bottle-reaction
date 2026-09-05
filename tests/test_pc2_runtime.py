import ast
import importlib.util
import json
from pathlib import Path
import types

import pytest

from robot_side.adapters import g1_robot as runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('machine,address,prefix,allowed', [
    ('aarch64', '192.168.123.164', 24, True),
    ('x86_64', '192.168.123.164', 24, False),
    ('aarch64', '192.168.123.99', 24, False),
    ('aarch64', '192.168.123.164', 16, False),
])
def test_pc2_guard_reads_only_network_state(monkeypatch, machine, address, prefix, allowed):
    monkeypatch.setattr(runtime.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(runtime.platform, 'machine', lambda: machine)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return types.SimpleNamespace(stdout=json.dumps([{'addr_info': [{'local': address, 'prefixlen': prefix}]}]))
    monkeypatch.setattr(runtime.subprocess, 'run', run)
    if allowed:
        runtime.require_pc2()
    else:
        with pytest.raises(RuntimeError):
            runtime.require_pc2()
    assert all(c == ['ip', '-j', '-4', 'address', 'show', 'dev', 'eth0'] for c in calls)


def test_runtime_requires_venv(monkeypatch):
    monkeypatch.setattr(runtime, 'require_pc2', lambda: None)
    monkeypatch.setattr(runtime.sys, 'prefix', runtime.sys.base_prefix)
    with pytest.raises(RuntimeError, match='venv'):
        runtime.require_runtime()


def test_python38_syntax_and_no_desktop_imports():
    paths = [ROOT / 'scripts/g1-slam-session.py', ROOT / 'scripts/g1-pc2-doctor.py',
             ROOT / 'robot_side/adapters/g1_robot.py']
    for path in paths:
        source = path.read_text()
        ast.parse(source, feature_version=(3, 8))
        assert 'from g1_bottle_reaction' not in source
        assert 'enp129s0' not in source
    assert 'name="eth0"' in runtime.DDS_XML
    assert '<Verbosity>none</Verbosity>' in runtime.DDS_XML


def test_stdin_probe_is_standalone_and_has_no_write_or_rpc_execution():
    spec = importlib.util.spec_from_file_location('build_probe', ROOT / 'scripts/build-pc2-probe.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    source = mod.source()
    ast.parse(source, feature_version=(3, 8))
    assert 'from robot_side' not in source
    assert not any(isinstance(n, ast.Name) and n.id == '__file__' for n in ast.walk(ast.parse(source)))
    # The callable RPC boundary is included, but doctor only invokes schema loaders.
    main = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    attrs = {n.func.attr for n in ast.walk(main) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert 'create_slam_single_call_client' not in attrs
    assert '_Call' not in attrs


@pytest.mark.parametrize('api_id', [1801, 1802, 1804])
def test_sdk_factory_registers_only_requested_api_and_restores_memory_config(monkeypatch, api_id):
    import sys
    events = []
    channel = types.ModuleType('unitree_sdk2py.core.channel')
    channel.ChannelConfigHasInterface = 'original'
    def initialize(domain, interface):
        events.append(('initialize', domain, interface, channel.ChannelConfigHasInterface))
    channel.ChannelFactoryInitialize = initialize
    core = types.ModuleType('unitree_sdk2py.core')
    core.channel = channel
    client_module = types.ModuleType('unitree_sdk2py.rpc.client')
    class Client:
        def __init__(self, name, lease):
            events.append(('client', name, lease))
        def SetTimeout(self, value):
            events.append(('timeout', value))
        def _SetApiVerson(self, value):
            events.append(('version', value))
        def _RegistApi(self, value, priority):
            events.append(('register', value, priority))
    client_module.Client = Client
    monkeypatch.setitem(sys.modules, 'unitree_sdk2py.core', core)
    monkeypatch.setitem(sys.modules, 'unitree_sdk2py.rpc.client', client_module)
    monkeypatch.setattr(runtime, 'require_runtime', lambda: None)
    monkeypatch.setattr(runtime, 'configure_sdk_path', lambda: None)
    runtime.UnitreeSdkRuntime().create_slam_single_call_client('eth0', 10, api_id)
    assert events == [('initialize', 0, 'eth0', runtime.DDS_XML), ('client', 'slam_operate', False),
                      ('timeout', 10), ('version', '1.0.0.1'), ('register', api_id, 0)]
    assert channel.ChannelConfigHasInterface == 'original'
