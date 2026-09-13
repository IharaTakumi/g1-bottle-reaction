"""Offline safety and statistics checks for the LowState benchmark."""
import ast
import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/g1-lowstate-readonly-benchmark.py'
MODULE = runpy.run_path(str(SCRIPT))


def test_benchmark_has_no_application_writer_or_rpc_entrypoint():
    tree = ast.parse(SCRIPT.read_text())
    forbidden = {'DataWriter', 'Publisher', 'ChannelPublisher', 'Client',
                 'ChannelFactoryInitialize', 'write', 'Write', '_Call', 'publish'}
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    used |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not forbidden & used
    assert 'rt/lowstate' in SCRIPT.read_text()
    assert 'rt/lowcmd' not in SCRIPT.read_text().lower()


def test_interface_override_does_not_change_base_xml():
    base = (ROOT / 'config/g1-readonly-dds.xml').read_text()
    configured = MODULE['config_for_interface'](
        base, 'wifi-test', ['192.0.2.1', '192.0.2.2'])
    assert 'name="wifi-test"' in configured
    assert 'Address="192.0.2.1"' in configured
    assert 'Address="192.0.2.2"' in configured
    assert 'name="enp129s0"' in base


def test_nearest_rank_percentile():
    assert MODULE['percentile']([4, 1, 3, 2], 0.50) == 2
    assert MODULE['percentile']([4, 1, 3, 2], 0.99) == 4
    assert MODULE['percentile']([], 0.95) is None
