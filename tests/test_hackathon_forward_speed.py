from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

PATROL = Path(__file__).resolve().parents[1] / "patrol"
sys.path.insert(0, str(PATROL))

from locomotion_relay import decode, parser as relay_parser
from run_patrol import config, parser as patrol_parser


def packet(vx: float) -> bytes:
    return json.dumps({"seq": 1, "vx": vx, "vy": 0.0, "vyaw": 0.0}).encode()


def test_normal_speed_limit_remains_point_three():
    args = patrol_parser().parse_args(["--forward-speed", "0.31"])
    with pytest.raises(ValueError, match="0.30"):
        config(args)
    with pytest.raises(ValueError, match="relay clamp"):
        decode(packet(0.31), False)


def test_explicit_half_meter_speed_is_accepted_end_to_end():
    args = patrol_parser().parse_args([
        "--forward-speed", "0.50", "--max-forward-speed", "0.50"
    ])
    assert config(args).forward_speed_m_s == pytest.approx(0.50)
    assert decode(packet(0.50), False, 0.50)[2][0] == pytest.approx(0.50)
    assert relay_parser().parse_args([
        "--max-forward-speed", "0.50"
    ]).max_forward_speed == pytest.approx(0.50)


def test_half_meter_opt_in_still_rejects_above_cap():
    with pytest.raises(ValueError, match="relay clamp"):
        decode(packet(0.5001), False, 0.50)
