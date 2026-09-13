#!/usr/bin/env python3
"""Run one fixed G1-local read-only probe or explicitly gated notice over SSH."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from g1_bottle_reaction.adapters.g1_ssh_safe_action import (  # noqa: E402
    RESULT_PREFIX,
    SshG1SafeActionAdapter,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("probe", "notice"))
    parser.add_argument("--ssh-target", required=True)
    parser.add_argument("--ssh-control")
    parser.add_argument("--execute-real-action", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    adapter = SshG1SafeActionAdapter(
        args.ssh_target,
        args.ssh_control,
        enabled=args.operation == "notice",
        motion_mode="safe-actions" if args.operation == "notice" else "disabled",
        execute_real_action=args.execute_real_action,
    )
    try:
        if args.operation == "probe":
            result = adapter.probe()
        else:
            adapter.initialize()
            adapter.play_motion("notice")
            result = adapter.last_result or {
                "ok": False,
                "operation": "notice",
                "error": "missing SSH helper result",
            }
        print(RESULT_PREFIX + json.dumps(result, sort_keys=True), flush=True)
        return 0 if result.get("ok") else 2
    except Exception as exc:
        print(
            RESULT_PREFIX
            + json.dumps(
                {"ok": False, "operation": args.operation, "error": str(exc)},
                sort_keys=True,
            ),
            flush=True,
        )
        return 2
    finally:
        adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
