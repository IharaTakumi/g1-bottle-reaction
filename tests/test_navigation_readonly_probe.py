"""Offline checks for telemetry formatting and the diagnostic safety boundary."""
import ast
from dataclasses import dataclass
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/read-g1-navigation-state.py'
PROBE = runpy.run_path(str(SCRIPT))
EXTENDED_SCRIPT = ROOT / 'scripts/g1-navigation-readonly-check.py'
EXTENDED = runpy.run_path(str(EXTENDED_SCRIPT))
PCAP = runpy.run_path(str(ROOT / 'scripts/analyze-g1-dds-pcap.py'))


@pytest.mark.parametrize('period', ['-1', 'nan', 'inf', '61'])
def test_invalid_sample_period_exits_before_dds(monkeypatch, period):
    monkeypatch.setattr('sys.argv', [str(SCRIPT), '--sample-period', period])
    with pytest.raises(SystemExit) as exc:
        PROBE['main']()
    assert exc.value.code == 2


@pytest.mark.parametrize('seconds', ['0', '-1', 'nan', 'inf', '61'])
def test_invalid_discovery_window_exits_before_dds(monkeypatch, seconds):
    monkeypatch.setattr('sys.argv', [str(SCRIPT), '--discovery-seconds', seconds])
    with pytest.raises(SystemExit) as exc:
        PROBE['main']()
    assert exc.value.code == 2


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
    assert result['points'] == 2
    assert result['data_bytes'] == 44
    assert 'data' not in result
    cloud.data = bytes(43)
    assert not PROBE['summarize']('cloud', cloud)['layout_consistent']


def test_main_probe_includes_mid360_imu_as_read_only_telemetry():
    assert PROBE['TOPICS']['rt/utlidar/imu_livox_mid360'] == 'imu'
    assert PROBE['TOPICS']['rt/unitree/slam_relocation/global_map'] == 'cloud'


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


def test_extended_probe_selects_live_types_and_excludes_switch_reader():
    targets = EXTENDED['TARGETS']
    assert targets['rt/sportmodestate'] == {
        EXTENDED['TYPE_GO_SPORT']: 'odomstate', EXTENDED['TYPE_HG_SPORT']: 'hg_sport'}
    assert targets['rt/utlidar/imu_livox_mid360'] == {EXTENDED['TYPE_IMU']: 'imu'}
    assert targets['rt/utlidar/range_info'] == {EXTENDED['TYPE_RANGE']: 'range'}
    assert EXTENDED['LIDAR_SWITCH'] not in targets


def test_extended_probe_has_no_application_writer_or_rpc_entrypoint():
    tree = ast.parse(EXTENDED_SCRIPT.read_text())
    forbidden = {'DataWriter', 'Publisher', 'ChannelPublisher', 'Client',
                 'NavigationCoordinator', 'RemoteNavigationAdapter', 'G1RobotAdapter',
                 'ChannelFactoryInitialize', 'create_arm_sdk_transport',
                 'initialize_channel', 'write', 'Write', '_Call', 'publish'}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not forbidden & used
    assert not any('/api/' in name or 'command' in name or 'switch' in name
                   for name in EXTENDED['TARGETS'])


def test_packet_capture_config_keeps_text_trace_disabled(tmp_path):
    xml = EXTENDED['trace_config'](
        (ROOT / 'config/g1-readonly-dds.xml').read_text(), tmp_path)
    assert '<Verbosity>none</Verbosity>' in xml
    assert '<PacketCaptureFile>' in xml
    assert '<Category>' not in xml
    assert '<OutputFile>' not in xml


def test_rtps_pcap_parser_correlates_data_writer_guid():
    prefix = bytes.fromhex('011005ade62f953c344ce908')
    writer = bytes.fromhex('000003c2')
    body = bytes(8) + writer + bytes(8)
    payload = b'RTPS' + bytes([2, 1, 1, 16]) + prefix
    payload += bytes([0x15, 1]) + len(body).to_bytes(2, 'little') + body
    assert PCAP['rtps_user_data'](payload) == [('DATA', (prefix + writer).hex())]
