from __future__ import annotations

import argparse
from pathlib import Path

from g1_bottle_reaction.adapters.mujoco_robot import (
    MujocoRobotAdapter,
    default_g1_model_path,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Headless virtual G1 smoke test")
    parser.add_argument("--model", type=Path, default=default_g1_model_path())
    args = parser.parse_args()
    adapter = MujocoRobotAdapter(model_path=args.model, launch_viewer=False)
    try:
        adapter.initialize()
        assert adapter.model is not None
        print(
            f"MUJOCO_MODEL_OK joints={adapter.model.njnt} qpos={adapter.model.nq}",
            flush=True,
        )
        adapter.play_motion("stand")
        if not adapter.wait_for_idle(2.0):
            raise RuntimeError("stand preview timed out")
        adapter.play_motion("little_dance")
        duration = adapter.library.animation("little_dance").duration_seconds
        if not adapter.wait_for_idle(duration + 3.0):
            raise RuntimeError("little_dance preview timed out")
        print("MUJOCO_ANIMATION_OK stand -> little_dance -> stand", flush=True)
        return 0
    finally:
        adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
