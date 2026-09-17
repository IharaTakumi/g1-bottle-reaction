from __future__ import annotations

from pathlib import Path

from .core import MaplessWanderCore
from .models import LocalObstacleSnapshot, OdomSample, WanderAction, WanderConfig
from .replay import read_replay


def run_synthetic_shadow(config: WanderConfig, *, seed: int | None) -> tuple[int, int]:
    open_m = config.point_cloud.max_range_m
    blocked = config.policy.blocked_distance_m * 0.75
    scenarios = (
        ("open", (open_m,) * 5, (0.0, 0.0, 0.0), {WanderAction.FORWARD}),
        ("front_wall", (open_m, open_m, blocked, open_m, open_m), (0.0, 0.0, 0.0), {WanderAction.TURN_LEFT, WanderAction.TURN_RIGHT}),
        ("left_block", (blocked, blocked, open_m, open_m, open_m), (0.0, 0.0, 0.0), {WanderAction.FORWARD}),
        ("right_block", (open_m, open_m, open_m, blocked, blocked), (0.0, 0.0, 0.0), {WanderAction.FORWARD}),
        ("narrow_corridor", (blocked, blocked, open_m, blocked, blocked), (0.0, 0.0, 0.0), {WanderAction.FORWARD}),
        ("dead_end", (open_m, blocked, blocked, blocked, open_m), (0.0, 0.0, 0.0), {WanderAction.TURN_LEFT, WanderAction.TURN_RIGHT}),
        ("sensor_stale", (open_m,) * 5, (0.0, 0.0, 0.0), {WanderAction.STOP}),
        ("soft_leash", (open_m,) * 5, (config.leash.soft_limit_m, 0.0, 0.0), {WanderAction.TURN_LEFT, WanderAction.TURN_RIGHT}),
        ("recent_trail", (open_m,) * 5, (0.0, 0.0, 0.0), {WanderAction.TURN_LEFT, WanderAction.TURN_RIGHT}),
    )
    passed = 0
    names = ("left", "front_left", "front", "front_right", "right")
    for index, (name, values, pose_values, expected) in enumerate(scenarios):
        core = MaplessWanderCore(config, seed=None if seed is None else seed + index)
        now = 100.0
        if name == "recent_trail":
            core.trail.add(OdomSample(config.policy.candidate_step_m, 0.0, 0.0, now - 0.1))
        timestamp = now - config.safety.stale_after_s - 0.1 if name == "sensor_stale" else now
        snapshot = LocalObstacleSnapshot.from_clearances(
            timestamp,
            dict(zip(names, values)),
            blocked_distance_m=config.policy.blocked_distance_m,
        )
        pose = OdomSample(*pose_values, timestamp=now)
        decision = core.decide(snapshot, pose, now=now)
        ok = decision.action in expected
        passed += int(ok)
        clear = " ".join(f"{sector}={snapshot.sector(sector).clearance_m:.2f}m" for sector in names)
        print(
            f"SCENARIO={name} DECISION={decision.action.value} SAFETY={decision.safety.value} "
            f"{clear} reason={decision.reason} {'PASS' if ok else 'FAIL'}"
        )
    print(f"SYNTHETIC={passed}/{len(scenarios)}")
    return passed, len(scenarios)


def run_replay_shadow(config: WanderConfig, path: Path, *, seed: int | None) -> int:
    core = MaplessWanderCore(config, seed=seed)
    count = 0
    for timestamp, snapshot, pose in read_replay(path, config):
        decision = core.decide(snapshot, pose, now=timestamp)
        clear = " ".join(
            f"{name}={snapshot.sector(name).clearance_m:.2f}m"
            for name in snapshot.sectors
        ) if snapshot.valid else "snapshot=INVALID"
        print(
            f"t={timestamp:.3f} DECISION={decision.action.value} SAFETY={decision.safety.value} "
            f"{clear} reason={decision.reason}"
        )
        count += 1
    return count
