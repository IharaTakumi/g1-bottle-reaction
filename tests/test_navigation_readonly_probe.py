"""Offline checks for telemetry formatting and the diagnostic safety boundary."""
import ast
from dataclasses import dataclass
from pathlib import Path
import runpy
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/read-g1-navigation-state.py'
PROBE = runpy.run_path(str(SCRIPT))


def test_string_status_does_not_infer_navigation_readiness():
    summarize = PROBE['summarize']
    sample = SimpleNamespace(data='{"info":"not init","data":{"currentPose":{"x":0}}}')
    assert summarize('string', sample)['json']['info'] == 'not init'
    assert summarize('string', SimpleNamespace(data='broken')) == {'text': 'broken'}
    assert summarize('string', SimpleNamespace(data='a' * 17000))['truncated']


def test_cloud_output_omits_bulk_data_and_checks_layout():
    @dataclass
    class Header:
        frame_id: str = 'livox_frame'

    cloud = SimpleNamespace(header=Header(), height=1, width=2, point_step=22,
                            row_step=44, data=bytes(44), fields=[],
                            is_dense=True, is_bigendian=False)
    result = PROBE['summarize']('cloud', cloud)
    assert result['layout_consistent']
    assert result['data_bytes'] == 44
    assert 'data' not in result
    cloud.data = bytes(43)
    assert not PROBE['summarize']('cloud', cloud)['layout_consistent']


def test_source_stamp_preserves_zero_and_rejects_missing_stamp():
    stamp = PROBE['source_stamp']
    assert stamp({'stamp': {'sec': 0, 'nanosec': 0}}) == (0, 0)
    assert stamp({'json': {'sec': 3, 'nanosec': 4}}) == (3, 4)
    assert stamp({'header': {'stamp': {'sec': 5, 'nanosec': 6}}}) == (5, 6)
    assert stamp({'json': []}) is None
    assert stamp({'header': None}) is None


def test_probe_has_no_application_writer_or_rpc_entrypoint():
    tree = ast.parse(SCRIPT.read_text())
    forbidden = {'DataWriter', 'Publisher', 'ChannelPublisher', 'Client',
                 'NavigationCoordinator', 'RemoteNavigationAdapter', 'G1RobotAdapter',
                 'ChannelFactoryInitialize', 'create_arm_sdk_transport',
                 'initialize_channel', 'write', 'Write', '_Call', 'publish'}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not forbidden & used
    assert not any('/api/' in name or 'command' in name or 'lowcmd' in name
                   for name in PROBE['TOPICS'])
    # Importing the script above must not initialize DDS; all DDS imports stay lazy.
    assert not any(isinstance(n, (ast.Import, ast.ImportFrom))
                   and 'cyclonedds' in ast.unparse(n) for n in tree.body)
