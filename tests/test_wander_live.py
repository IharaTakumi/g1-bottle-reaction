from __future__ import annotations

import ast
from dataclasses import dataclass
import io
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest
import yaml

from g1_bottle_reaction.navigation.wander.live import LiveShadowSession, run_live_shadow
from g1_bottle_reaction.navigation.wander.replay import read_replay
from robot_side.wander_live import decode_xyz, odom_payload, reduce_points


ROOT = Path(__file__).resolve().parents[1]


def live_record(**overrides):
    value = {
        "timestamp": 100.0,
        "cloud_age_s": 0.01,
        "odom_age_s": 0.02,
        "odom": {"x": 0.0, "y": 0.0, "yaw": 0.0, "source_timestamp": 99.9},
        "obstacle_snapshot": {
            "left": 4.0,
            "front_left": 4.0,
            "front": 4.0,
            "front_right": 4.0,
            "right": 4.0,
        },
        "valid_points": {name: 10 for name in ("left", "front_left", "front", "front_right", "right")},
        "valid": True,
    }
    value.update(overrides)
    return value


def test_live_session_connects_snapshot_to_wander_core(app_config) -> None:
    result = LiveShadowSession(app_config.wander, seed=11).process(
        live_record(), now=10.0
    )
    assert "ACTION=FORWARD" in result
    assert "SAFETY=SAFE" in result
    assert "cloud_age=0.010" in result


@pytest.mark.parametrize(
    "value, reason",
    [
        ({"bad": "record"}, "required obstacle sectors missing"),
        (live_record(odom=None), "required odometry missing"),
        (live_record(cloud_age_s=2.0), "ACTION=STOP"),
    ],
)
def test_live_fail_closed_for_missing_or_stale_input(app_config, value, reason) -> None:
    session = LiveShadowSession(app_config.wander, seed=1)
    if reason == "ACTION=STOP":
        assert reason in session.process(value, now=10.0)
    else:
        with pytest.raises(ValueError, match=reason):
            session.process(value, now=10.0)


def test_live_stream_malformed_and_eof_display_stop_and_record_valid_only(
    app_config, tmp_path
) -> None:
    record_path = tmp_path / "record.jsonl"
    stream = io.StringIO("not-json\n" + json.dumps(live_record()) + "\n")
    output = []
    result = run_live_shadow(
        app_config.wander,
        stream=stream,
        record_path=record_path,
        seed=1,
        output=output.append,
    )
    assert result == 2
    assert "ACTION=STOP" in output[0]
    assert "ACTION=FORWARD" in output[1]
    assert "stream disconnected" in output[-1]
    assert len(record_path.read_text(encoding="utf-8").splitlines()) == 1

    timestamp, snapshot, pose = next(read_replay(record_path, app_config.wander))
    assert snapshot.timestamp == pytest.approx(timestamp - 0.01)
    assert pose.timestamp == pytest.approx(timestamp - 0.02)


@dataclass
class Field:
    name: str
    offset: int
    datatype: int = 7
    count: int = 1


def test_pc2_decoder_and_sector_reduction_include_axis_diagnostics() -> None:
    raw = b"".join(struct.pack("<fff", *point) for point in [(1.0, 0.0, 0.5)] * 5)
    sample = SimpleNamespace(
        fields=[Field("x", 0), Field("y", 4), Field("z", 8)],
        point_step=12,
        row_step=60,
        width=5,
        height=1,
        data=raw,
        is_bigendian=False,
    )
    config = json.loads((ROOT / "config/wander_live_pc2.json").read_text())
    result = reduce_points(decode_xyz(sample), config, debug=True)
    assert result["valid"]
    assert result["valid_points"]["front"] == 5
    assert result["obstacle_snapshot"]["front"] == pytest.approx(1.0)
    assert result["diagnostics"]["nearest_point_xyz"]["front"] == [1.0, 0.0, 0.5]


def test_pc2_and_desktop_perception_defaults_stay_in_sync() -> None:
    pc2 = json.loads((ROOT / "config/wander_live_pc2.json").read_text())
    desktop = yaml.safe_load(
        (ROOT / "config/default.yaml").read_text(encoding="utf-8")
    )["wander"]
    expected = dict(desktop["point_cloud"])
    expected["blocked_distance_m"] = desktop["policy"]["blocked_distance_m"]
    assert pc2 == expected


def test_odom_conversion_preserves_source_time_and_yaw() -> None:
    sample = SimpleNamespace(
        header=SimpleNamespace(stamp=SimpleNamespace(sec=12, nanosec=500_000_000)),
        pose=SimpleNamespace(
            pose=SimpleNamespace(
                position=SimpleNamespace(x=1.0, y=-2.0),
                orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
            )
        ),
    )
    assert odom_payload(sample) == {
        "x": 1.0,
        "y": -2.0,
        "yaw": 0.0,
        "source_timestamp": 12.5,
    }


def test_live_source_has_no_writer_rpc_or_loco_symbols() -> None:
    source = (ROOT / "scripts/g1-wander-live-source.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    forbidden = {"DataWriter", "Publisher", "ChannelPublisher", "Client", "LocoClient", "SetVelocity", "StopMove"}
    assert not forbidden & (names | attributes)
    assert "rt/utlidar/cloud_livox_mid360" in source
    assert "rt/dog_odom" in source
