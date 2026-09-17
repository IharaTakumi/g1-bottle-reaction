from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading

import pytest

from g1_bottle_reaction.adapters.motiondecode_reaction import (
    MotionDecodeReactionAdapter,
    MotionDecodeSafeReturnMiss,
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

    adapter = MotionDecodeReactionAdapter(tmp_path, run_factory=run, resident=False)
    adapter.play_motion("motiondecode:frustration")

    command, kwargs = calls[0]
    assert "run_named_reaction.py" in command[1]
    assert "frustration" in command
    assert "--dry-run" in command
    assert kwargs["capture_output"] is True
    assert adapter.wait_for_motion_complete("motiondecode:frustration") is True


def test_dry_run_resolves_validated_surprise_to_named_cli(tmp_path: Path) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return completed({
            "reaction": "surprise", "status": "pass", "executed": False,
            "released": False,
        })

    adapter = MotionDecodeReactionAdapter(tmp_path, run_factory=run, resident=False)
    adapter.play_motion("motiondecode:surprise")

    command, _ = calls[0]
    assert "surprise" in command
    assert "--dry-run" in command
    assert adapter.wait_for_motion_complete("motiondecode:surprise") is True


def test_dry_run_resolves_validated_found_to_named_cli(tmp_path: Path) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return completed({
            "reaction": "found", "status": "pass", "executed": False,
            "released": False,
        })

    adapter = MotionDecodeReactionAdapter(tmp_path, run_factory=run, resident=False)
    adapter.play_motion("motiondecode:found")

    command, _ = calls[0]
    assert "found" in command
    assert "--dry-run" in command
    assert adapter.wait_for_motion_complete("motiondecode:found") is True


def test_wrong_reaction_is_rejected_before_subprocess(tmp_path: Path) -> None:
    adapter = MotionDecodeReactionAdapter(
        tmp_path, run_factory=lambda *a, **kw: pytest.fail("must not run"), resident=False
    )
    with pytest.raises(ValueError, match="not allowlisted"):
        adapter.play_motion("motiondecode:unknown")


def test_nonzero_exit_is_reported(tmp_path: Path) -> None:
    adapter = MotionDecodeReactionAdapter(
        tmp_path,
        run_factory=lambda *a, **kw: subprocess.CompletedProcess([], 2, "", "failed"),
        resident=False,
    )
    with pytest.raises(RuntimeError, match="exited with code 2"):
        adapter.play_motion("motiondecode:frustration")


def test_timeout_is_not_swallowed(tmp_path: Path) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    adapter = MotionDecodeReactionAdapter(tmp_path, run_factory=timeout, resident=False)
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

    adapter = MotionDecodeReactionAdapter(tmp_path, run_factory=run, resident=False)
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
        enabled=True, resident=False,
        run_factory=lambda *a, **kw: completed({
            "reaction": "frustration", "status": "pass", "executed": True,
            "released": False, "returned_to_q0": True,
        }),
    )
    with pytest.raises(RuntimeError, match="lacks required cleanup proof"):
        adapter.play_motion("motiondecode:frustration")


def test_attended_real_retains_manual_gate_boundary(tmp_path: Path) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, None, None)

    adapter = MotionDecodeReactionAdapter(
        tmp_path, real=True, enabled=True, attended_real=True, run_factory=run, resident=False
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


class FakeResidentChannel:
    def __init__(self):
        self.requests = []; self.closed = False

    def request(self, payload):
        self.requests.append(payload)
        if payload["operation"] == "status":
            return {"accepted": True, "state": "READY"}
        return {"accepted": True, "state": "READY", "reaction": payload["reaction"],
                "status": "pass", "executed": False, "released": True,
                "returned_to_q0": True}

    def close(self):
        self.closed = True


def test_resident_adapter_connects_at_start_and_reuses_channel(tmp_path: Path) -> None:
    channel = FakeResidentChannel()
    adapter = MotionDecodeReactionAdapter(tmp_path, channel_factory=lambda: channel)
    adapter.play_motion("motiondecode:surprise")
    adapter.play_motion("motiondecode:found")
    assert [request["operation"] for request in channel.requests] == [
        "status", "execute", "execute"]
    assert all("trigger_monotonic_s" in request for request in channel.requests[1:])
    adapter.close(); assert channel.closed


def test_resident_worker_unavailable_fails_without_cli_fallback(tmp_path: Path) -> None:
    class NotReady(FakeResidentChannel):
        def request(self, payload):
            return {"accepted": True, "state": "FAULT", "reason": "not ready"}
    with pytest.raises(RuntimeError, match="not READY"):
        MotionDecodeReactionAdapter(tmp_path, channel_factory=NotReady)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("motion_completed", False),
        ("weight_zero", False),
        ("hard_fault", "tracking fault"),
    ],
)
def test_real_resident_requires_full_cleanup_proof(
    tmp_path: Path, field: str, value: object
) -> None:
    class RealChannel(FakeResidentChannel):
        def request(self, payload):
            if payload["operation"] == "status":
                return {"accepted": True, "state": "READY"}
            result = {
                "accepted": True,
                "state": "READY",
                "reaction": payload["reaction"],
                "status": "pass",
                "executed": True,
                "motion_completed": True,
                "released": True,
                "weight_zero": True,
                "returned_to_q0": True,
                "hard_fault": None,
            }
            result[field] = value
            return result
    adapter = MotionDecodeReactionAdapter(
        tmp_path, real=True, enabled=True, channel_factory=RealChannel)
    with pytest.raises(RuntimeError, match="lacks required cleanup proof"):
        adapter.play_motion("motiondecode:surprise")


def real_result(reaction: str, **overrides) -> dict:
    result = {
        "accepted": True,
        "state": "READY",
        "reaction": reaction,
        "status": "pass",
        "executed": True,
        "motion_completed": True,
        "released": True,
        "returned_to_q0": True,
        "controlled_q0_return_error_rad": 0.02,
        "weight_zero": True,
        "hard_fault": None,
    }
    result.update(overrides)
    return result


def safe_resident_status(**overrides) -> dict:
    status = {
        "accepted": True,
        "state": "READY",
        "lowstate_age_s": 0.01,
        "ownership_safe": True,
        "external_writers": 0,
        "weight": 0.0,
        "fault": None,
    }
    status.update(overrides)
    return status


class ResultSequenceChannel:
    def __init__(self, results, post_statuses=()):
        self.results = list(results)
        self.post_statuses = list(post_statuses)
        self.requests = []

    def request(self, payload):
        self.requests.append(payload)
        if payload["operation"] == "status":
            if self.post_statuses:
                return self.post_statuses.pop(0)
            return safe_resident_status()
        return self.results.pop(0)

    def close(self):
        pass


def test_safe_return_miss_raises_typed_failure_then_allows_next_success(
    tmp_path: Path,
) -> None:
    channel = ResultSequenceChannel([
        real_result("found", returned_to_q0=False,
                    controlled_q0_return_error_rad=0.0322),
        real_result("joy"),
    ])
    adapter = MotionDecodeReactionAdapter(
        tmp_path, real=True, enabled=True, allow_hackathon_joy=True,
        channel_factory=lambda: channel,
    )

    with pytest.raises(MotionDecodeSafeReturnMiss) as caught:
        adapter.play_motion("motiondecode:found")
    assert caught.value.reaction == "found"
    assert caught.value.controlled_q0_return_error_rad == pytest.approx(0.0322)
    assert caught.value.returned_to_q0 is False
    assert caught.value.resident_status["state"] == "READY"
    assert adapter.wait_for_motion_complete("motiondecode:found") is False

    adapter.play_motion("motiondecode:joy")
    assert adapter.wait_for_motion_complete("motiondecode:joy") is True
    assert [request["operation"] for request in channel.requests] == [
        "status", "execute", "status", "execute"
    ]


@pytest.mark.parametrize(
    "unsafe_status",
    [
        safe_resident_status(state="FAULT", fault="resident fault"),
        safe_resident_status(lowstate_age_s=0.15),
        safe_resident_status(ownership_safe=False),
        safe_resident_status(external_writers=1),
        safe_resident_status(weight=0.01),
    ],
)
def test_safe_return_miss_with_unsafe_postflight_is_hard_failure(
    tmp_path: Path, unsafe_status: dict,
) -> None:
    channel = ResultSequenceChannel(
        [real_result("found", returned_to_q0=False,
                     controlled_q0_return_error_rad=0.0322)],
        # First status constructs the adapter; second is the postflight.
        [safe_resident_status(), unsafe_status],
    )
    adapter = MotionDecodeReactionAdapter(
        tmp_path, real=True, enabled=True, channel_factory=lambda: channel
    )
    with pytest.raises(RuntimeError, match="unsafe or ambiguous resident postflight"):
        adapter.play_motion("motiondecode:found")


def test_safe_return_miss_status_ipc_failure_remains_hard_failure(
    tmp_path: Path,
) -> None:
    class StatusFailureChannel(ResultSequenceChannel):
        def request(self, payload):
            if payload["operation"] == "status" and self.requests:
                self.requests.append(payload)
                raise RuntimeError("status IPC failed")
            return super().request(payload)

    channel = StatusFailureChannel([
        real_result("found", returned_to_q0=False,
                    controlled_q0_return_error_rad=0.0322)
    ])
    adapter = MotionDecodeReactionAdapter(
        tmp_path, real=True, enabled=True, channel_factory=lambda: channel
    )
    with pytest.raises(RuntimeError, match="status IPC failed"):
        adapter.play_motion("motiondecode:found")


def test_real_adapter_rejects_unvalidated_joy_before_execute(tmp_path: Path) -> None:
    channel = FakeResidentChannel()
    adapter = MotionDecodeReactionAdapter(
        tmp_path,
        real=True,
        enabled=True,
        channel_factory=lambda: channel,
    )
    with pytest.raises(RuntimeError, match="not validated for real G1: joy"):
        adapter.play_motion("motiondecode:joy")
    assert [request["operation"] for request in channel.requests] == ["status"]


def test_real_adapter_allows_joy_only_with_explicit_hackathon_gate(tmp_path: Path) -> None:
    class RealJoyChannel(FakeResidentChannel):
        def request(self, payload):
            self.requests.append(payload)
            if payload["operation"] == "status":
                return {"accepted": True, "state": "READY"}
            return {
                "accepted": True,
                "state": "READY",
                "reaction": payload["reaction"],
                "status": "pass",
                "executed": True,
                "motion_completed": True,
                "released": True,
                "returned_to_q0": True,
                "weight_zero": True,
                "hard_fault": None,
            }

    channel = RealJoyChannel()
    adapter = MotionDecodeReactionAdapter(
        tmp_path,
        real=True,
        enabled=True,
        allow_hackathon_joy=True,
        channel_factory=lambda: channel,
    )
    adapter.play_motion("motiondecode:joy")
    assert adapter.wait_for_motion_complete("motiondecode:joy")
    assert [request["operation"] for request in channel.requests] == [
        "status", "execute"
    ]


def test_hackathon_joy_gate_is_rejected_for_dry_run(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only for real MotionDecode"):
        MotionDecodeReactionAdapter(
            tmp_path,
            allow_hackathon_joy=True,
            resident=False,
        )
