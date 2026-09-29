from pathlib import Path
import sys

import pytest


PATROL = Path(__file__).resolve().parents[1] / "patrol"
sys.path.insert(0, str(PATROL))

from lidar_guard import GuardConfig, GuardState, LidarGuard
from run_patrol import parser


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def points(distance):
    return [(distance, 0.0, 0.0)] * 5 + [(1.5, 0.0, 0.0)] * 95


def settle(guard, clock, sample):
    guard.update_points(sample)
    clock.now += 0.41
    guard.update_points(sample)


def test_lidar_stop_distance_default_and_hackathon_cli():
    assert GuardConfig().stop_distance_m == 0.80
    cli = parser()
    assert cli.parse_args([]).lidar_stop_distance == 0.80
    assert cli.parse_args([
        "--lidar-stop-distance", "0.50"
    ]).lidar_stop_distance == 0.50
    with pytest.raises(SystemExit):
        cli.parse_args(["--lidar-stop-distance", "0.49"])


def test_hackathon_lidar_blocks_at_half_meter_and_clears_beyond_it():
    clock = Clock()
    guard = LidarGuard(GuardConfig(stop_distance_m=0.50), clock=clock)
    guard.update_points(points(0.50))
    assert guard.state("front") is GuardState.BLOCKED

    settle(guard, clock, points(0.51))
    assert guard.state("front") is GuardState.CLEAR


def test_hackathon_lidar_stale_remains_fail_closed():
    clock = Clock()
    guard = LidarGuard(GuardConfig(stop_distance_m=0.50), clock=clock)
    settle(guard, clock, points(0.51))
    assert guard.state("front") is GuardState.CLEAR
    clock.now += 0.51
    assert guard.state("front") is GuardState.STALE


def test_block_requires_continuous_observation_but_confirmation_is_not_clear():
    clock = Clock()
    guard = LidarGuard(
        GuardConfig(stop_distance_m=0.50, clear_hold_s=0.0, block_confirm_s=0.10),
        clock=clock,
    )
    guard.update_points(points(0.51))
    guard.update_points(points(0.51))
    assert guard.state("front") is GuardState.CLEAR
    guard.update_points(points(0.50))
    assert guard.state("front") is GuardState.CONFIRMING
    clock.now += 0.11
    guard.update_points(points(0.50))
    assert guard.state("front") is GuardState.BLOCKED


def test_turn_guard_is_radial_and_does_not_reuse_front_corridor():
    clock = Clock()
    guard = LidarGuard(
        GuardConfig(stop_distance_m=0.50, clear_hold_s=0.0, block_confirm_s=0.0),
        clock=clock,
    )
    sample = points(0.51) + [(0.0, -0.40, 0.0)] * 5
    guard.update_points(sample)
    guard.update_points(sample)
    assert guard.state("front") is GuardState.CLEAR
    assert guard.state("turn") is GuardState.BLOCKED
