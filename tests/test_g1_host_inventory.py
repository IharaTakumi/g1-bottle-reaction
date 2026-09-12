from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOSTS = runpy.run_path(str(ROOT / 'scripts/read-g1-dds-hosts.py'))
PCD = runpy.run_path(str(ROOT / 'scripts/inspect-pcd-header.py'))


def test_invalid_local_ip_exits_before_socket(monkeypatch):
    monkeypatch.setattr('sys.argv', ['read-g1-dds-hosts.py', '--local-ip', 'not-an-ip'])
    with pytest.raises(SystemExit) as exc:
        HOSTS['main']()
    assert exc.value.code == 2


def test_rtps_identity_does_not_decode_guid_as_ip():
    prefix = bytes.fromhex('01101c9b7042ed0f85113fc3')
    result = HOSTS['rtps_identity'](b'RTPS\x02\x01\x01\x10' + prefix)
    assert result['guid_prefix'] == prefix.hex()
    assert 'source_ip' not in result
    assert HOSTS['rtps_identity'](b'RTPS') is None
    assert HOSTS['rtps_identity'](b'NOTP' + bytes(16)) is None


def test_pcd_header_stops_before_binary_payload(tmp_path):
    path = tmp_path / 'map.pcd'
    header = b'# .PCD v0.7\nVERSION 0.7\nFIELDS x y z\nDATA binary\n'
    path.write_bytes(header + b'\xff\x00SECRET_POINT_DATA')
    result = PCD['inspect'](path)
    assert result['pcd_header'] == header.decode()
    assert result['size_bytes'] == path.stat().st_size
    assert set(result) == {'filename', 'absolute_path', 'size_bytes', 'modified_time_utc', 'pcd_header'}


def test_pcd_inspection_requires_absolute_path_and_bounded_header(tmp_path):
    with pytest.raises(ValueError, match='absolute'):
        PCD['inspect']('map.pcd')
    path = tmp_path / 'bad.pcd'
    path.write_bytes(b'x' * 70000)
    with pytest.raises(ValueError, match='64 KiB'):
        PCD['inspect'](path)
