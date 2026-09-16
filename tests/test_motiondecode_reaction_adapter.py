from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading

import pytest

from g1_bottle_reaction.adapters.motiondecode_reaction import (
    MotionDecodeReactionAdapter,
    parse_named_result,
)


def completed(result: dict, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, json.dumps(result) + "\n", "")


def test_dry_run_resolves_frustration_to_named_cli(tmp_path: Path) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return completed({
            "reaction": "frustration", "status": "pass", "executed": False,
            "released": False,
        })

    adapter = MotionDecodeReactionAdapter(tmp_path, run_factory=run)
    adapter.play_motion("motiondecode:frustration")

    command, kwargs = calls[0]
    assert "run_named_reaction.py" in command[1]
    assert "frustration" in command
    assert "--dry-run" in command
    assert kwargs["capture_output"] is True
    assert adapter.wait_for_motion_complete("motiondecode:frustration") is True


def test_wrong_reaction_is_rejected_before_subprocess(tmp_path: Path) -> None:
    adapter = MotionDecodeReactionAdapter(
        tmp_path, run_factory=lambda *a, **kw: pytest.fail("must not run")
    )
    with pytest.raises(ValueError, match="not allowlisted"):
        adapter.play_motion("motiondecode:unknown")


def test_nonzero_exit_is_reported(tmp_path: Path) -> None:
    adapter = MotionDecodeReactionAdapter(
        tmp_path,
        run_factory=lambda *a, **kw: subprocess.CompletedProcess([], 2, "", "failed"),
    )
    with pytest.raises(RuntimeError, match="exited with code 2"):
        adapter.play_motion("motiondecode:frustration")


def test_timeout_is_not_swallowed(tmp_path: Path) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    adapter = MotionDecodeReactionAdapter(tmp_path, run_factory=timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        adapter.play_motion("motiondecode:frustration")


def test_concurrent_execution_is_rejected(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def run(*args, **kwargs):
        started.set()
        assert release.wait(1)
        return completed({
            "reaction": "frustration", "status": "pass", "executed": False,
            "released": False,
        })

    adapter = MotionDecodeReactionAdapter(tmp_path, run_factory=run)
    worker = threading.Thread(
        target=adapter.play_motion, args=("motiondecode:frustration",)
    )
    worker.start()
    assert started.wait(1)
    with pytest.raises(RuntimeError, match="already executing"):
        adapter.play_motion("motiondecode:frustration")
    release.set()
    worker.join(1)
    assert not worker.is_alive()


def test_real_result_requires_release_and_q0(tmp_path: Path) -> None:
    adapter = MotionDecodeReactionAdapter(
        tmp_path,
        real=True,
        enabled=True,
        run_factory=lambda *a, **kw: completed({
            "reaction": "frustration", "status": "pass", "executed": True,
            "released": False, "returned_to_q0": True,
        }),
    )
    with pytest.raises(RuntimeError, match="lacks execution/release/q0 proof"):
        adapter.play_motion("motiondecode:frustration")


def test_attended_real_retains_manual_gate_boundary(tmp_path: Path) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, None, None)

    adapter = MotionDecodeReactionAdapter(
        tmp_path, real=True, enabled=True, attended_real=True, run_factory=run
    )
    adapter.play_motion("motiondecode:frustration")
    command, kwargs = calls[0]
    assert "--engine-authorized" not in command
    assert "--json" not in command
    assert kwargs["capture_output"] is False
    assert adapter.last_result["released"] is True


def test_result_parser_rejects_ambiguous_output() -> None:
    with pytest.raises(RuntimeError, match="found 2"):
        parse_named_result('{}\n{}\n')
