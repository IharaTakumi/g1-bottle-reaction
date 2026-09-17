from __future__ import annotations

import json

from g1_bottle_reaction.main import main


def test_synthetic_shadow_runs_without_robot_or_camera(capsys) -> None:
    assert main(["--wander-shadow", "--wander-seed", "11"]) == 0
    output = capsys.readouterr().out
    assert "SYNTHETIC=9/9" in output
    assert "DECISION=" in output
    assert "reason=" in output


def test_snapshot_replay_runs_without_robot(tmp_path, capsys) -> None:
    replay = tmp_path / "wander.jsonl"
    replay.write_text(
        json.dumps(
            {
                "timestamp": 1.0,
                "odom": {"x": 0.0, "y": 0.0, "yaw": 0.0},
                "obstacle_snapshot": {
                    "left": 4.0,
                    "front_left": 4.0,
                    "front": 4.0,
                    "front_right": 4.0,
                    "right": 4.0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert main(["--wander-replay", str(replay), "--wander-seed", "2"]) == 0
    output = capsys.readouterr().out
    assert "REPLAY=1 samples" in output
    assert "DECISION=FORWARD" in output
