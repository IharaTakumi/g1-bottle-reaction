#!/usr/bin/env python3
"""Send one explicit test event through ReactionEngine to MotionDecode."""
from __future__ import annotations

import argparse
from pathlib import Path

from g1_bottle_reaction.adapters.motiondecode_reaction import MotionDecodeReactionAdapter
from g1_bottle_reaction.adapters.speech import MuteSpeechBackend
from g1_bottle_reaction.config.loader import ReactionConfig
from g1_bottle_reaction.reactions.engine import ReactionEngine, ReactionLifecycleState
from g1_bottle_reaction.reactions.models import Reaction
from g1_bottle_reaction.state.events import ReactionEvent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--real", action="store_true")
    parser.add_argument("--enable-real-robot", action="store_true")
    parser.add_argument("--motiondecode-repository", type=Path, default=Path("/home/ubuntu/dev/motiondecode-test"))
    parser.add_argument("--transport", choices=("local", "ssh"), default="ssh")
    parser.add_argument("--ssh-target", default="unitree@10.42.0.76")
    parser.add_argument("--ssh-control")
    parser.add_argument("--timeout", type=float, default=420.0)
    parser.add_argument("--attended-gate", action="store_true")
    parser.add_argument(
        "--reaction", choices=("frustration", "surprise", "found"), default="frustration"
    )
    args = parser.parse_args()

    adapter = MotionDecodeReactionAdapter(
        args.motiondecode_repository,
        real=args.real,
        enabled=args.enable_real_robot,
        transport=args.transport,
        ssh_target=args.ssh_target,
        ssh_control=args.ssh_control,
        timeout_seconds=args.timeout,
        attended_real=args.attended_gate,
    )
    reaction = Reaction(
        name=f"{args.reaction}-test-event",
        motion=f"motiondecode:{args.reaction}",
        speech="",
        speech_delay_seconds=0.0,
        priority=100,
        bypass_cooldown=True,
    )
    config = ReactionConfig(
        cooldown_seconds=0.0,
        items={ReactionEvent.FOUND.value: reaction},
    )
    engine = ReactionEngine(
        config,
        adapter,
        MuteSpeechBackend(),
        start_worker=False,
        motion_completion_timeout_s=args.timeout,
    )
    try:
        decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=0.0)
        if decision.job is None or decision.job.state is not ReactionLifecycleState.COMPLETED:
            error = None if decision.job is None else decision.job.error
            raise RuntimeError(f"Reaction job did not complete: {error}")
        print("REACTION_ENGINE_RESULT=PASS", flush=True)
        print(f"MOTIONDECODE_RESULT={adapter.last_result}", flush=True)
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
