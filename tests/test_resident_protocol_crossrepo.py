"""Offline cross-repo contract tests; point MOTIONDECODE_TEST_ROOT at its checkout.

Only the resident's pure protocol fixtures are imported. No worker launcher,
real backend, SSH, SDK or DDS is used.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import sys

import pytest

resident_root = os.environ.get("MOTIONDECODE_TEST_ROOT")
if not resident_root:
    pytest.skip("MOTIONDECODE_TEST_ROOT required for cross-repo tests", allow_module_level=True)
sys.path[:0] = [str(Path(resident_root) / "scripts"), str(Path(resident_root) / "tests")]
from test_resident_protocol import worker
from g1_bottle_reaction.adapters.motiondecode_reaction import (
    LocalResidentChannel, MotionDecodeReactionAdapter,
)


@pytest.mark.parametrize("initial_mode", ["dry-run", "real"])
@pytest.mark.parametrize("replacement_mode", ["dry-run", "real"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("operation", ["preflight_bound", "execute_bound"])
def test_production_adapter_rejects_live_replacement(
    tmp_path, initial_mode, replacement_mode, legacy, operation,
):
    path = tmp_path / "worker.sock"
    with ExitStack() as active:
        a, _ = active.enter_context(worker(path, initial_mode))
        old_session = a.session_id

        class SwapChannel(LocalResidentChannel):
            def __init__(self):
                super().__init__(str(path), 3)
                self.calls = []
                self.backend = None
                self.response = None

            def request(self, payload):
                self.calls.append(dict(payload))
                if payload["operation"] == operation:
                    assert payload["expected_session_id"] == old_session
                    assert payload["expected_mode"] == initial_mode
                    # Replace only after the adapter's fresh status validation.
                    active.close()
                    _, self.backend = active.enter_context(worker(
                        path, replacement_mode, legacy=legacy))
                self.response = super().request(payload)
                return self.response

        channel = SwapChannel()
        real = initial_mode == "real"
        adapter = MotionDecodeReactionAdapter(tmp_path, real=real, enabled=real,
                                              channel_factory=lambda: channel)
        try:
            if operation == "preflight_bound":
                assert not adapter.preflight_motion()
            else:
                with pytest.raises(RuntimeError, match="rejected"):
                    adapter.play_motion("motiondecode:surprise")
            assert channel.response["accepted"] is False
            if legacy:
                assert channel.response["reason"] == "unknown operation"
            assert channel.backend.executions == channel.backend.preflight_calls == 0
            calls = list(channel.calls)
            assert [c["operation"] for c in calls] == ["status", "status", operation]
            assert not adapter.preflight_motion()
            with pytest.raises(RuntimeError):
                adapter.play_motion("motiondecode:surprise")
            assert channel.calls == calls  # No refresh, retry, or legacy fallback.
            assert adapter._expected_resident_session == old_session
        finally:
            adapter.close()


@pytest.mark.parametrize("real", [False, True])
def test_legacy_startup_is_rejected_before_execute(tmp_path, real):
    path = tmp_path / "worker.sock"
    with worker(path, "real" if real else "dry-run", legacy=True) as (_, backend):
        with pytest.raises(RuntimeError, match="protocol_version"):
            MotionDecodeReactionAdapter(tmp_path, real=real, enabled=real,
                                        transport="local", socket_path=str(path))
        assert backend.executions == backend.preflight_calls == 0


@pytest.mark.parametrize("real", [False, True])
def test_new_protocol_round_trip(tmp_path, real):
    path = tmp_path / "worker.sock"
    with worker(path, "real" if real else "dry-run") as (_, backend):
        dry_execute = backend.execute

        def fake_execute(reaction, request, received):
            result = dry_execute(reaction, request, received)
            result.update(executed=real, motion_completed=True, weight_zero=True, hard_fault=None)
            return result

        backend.execute = fake_execute
        adapter = MotionDecodeReactionAdapter(tmp_path, real=real, enabled=real,
                                              transport="local", socket_path=str(path))
        try:
            assert adapter.preflight_motion()
            adapter.play_motion("motiondecode:surprise")
            assert adapter.wait_for_motion_complete("motiondecode:surprise")
            assert backend.executions == backend.preflight_calls == 1
        finally:
            adapter.close()
