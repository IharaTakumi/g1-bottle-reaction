from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
START = SCRIPTS / "start-motiondecode-resident.sh"
DEPLOY = SCRIPTS / "deploy-motiondecode-runtime.sh"

spec = importlib.util.spec_from_file_location("runtime_validate", SCRIPTS / "reaction-runtime-validate.py")
validator = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(validator)

def arm_report(publishers: list[dict] | None = None, **overrides) -> dict:
    candidate = {
        "hostname": "Unitree", "process_name": "python3", "pid": 4321,
        "source_ips": ["192.168.123.161"],
        "related_publications": sorted(validator.REQUIRED_PUBLICATIONS),
        "related_subscriptions": sorted(validator.REQUIRED_SUBSCRIPTIONS),
    }
    report = {
        "topics": {"rt/arm_sdk": {"publishers": publishers if publishers is not None else [candidate]},
                   "rt/arm/action/state": {"status_during_observation": "IDLE"}},
        "lowstate": {"status": "PASS", "stationary": "PASS"},
    }
    report.update(overrides)
    return report

def test_arm_candidate_must_be_exactly_one() -> None:
    report = arm_report()
    assert validator.strict_arm_pid(report, "192.168.123.161") == 4321
    with pytest.raises(ValueError, match="candidates=0"):
        validator.strict_arm_pid(arm_report([]), "192.168.123.161")
    with pytest.raises(ValueError, match="candidates=2"):
        validator.strict_arm_pid(arm_report(report["topics"]["rt/arm_sdk"]["publishers"] * 2), "192.168.123.161")

def test_ready_status_requires_all_production_invariants() -> None:
    status = {"accepted": True, "state": "READY", "mode": "real", "arm_action": "IDLE",
              "weight": 0.0, "external_writers": 0, "ownership_safe": True, "fault": None,
              "preflight": {"found": True, "surprise": True}}
    validator.ready_status(status)
    status["external_writers"] = 1
    with pytest.raises(ValueError, match="external_writers"):
        validator.ready_status(status)

def test_production_paths_and_start_are_fail_closed() -> None:
    text = START.read_text()
    assert "/home/unitree/g1-runtime/motiondecode" in text
    assert "/tmp/motiondecode-current" not in text
    assert "/tmp/motiondecode-hold-deps" not in text
    assert "MOTIONDECODE_EXPECTED_ARM_PID" not in text
    assert "RUNTIME_NOT_INSTALLED" in text and "RUNTIME_HASH_MISMATCH" in text
    assert "PROCESS_WITHOUT_SOCKET_FAIL_CLOSED" in text
    assert "HEALTHY_RESIDENT_REUSED" in text
    assert "{\"operation\":\"stop\"}" in text
    assert "rm -f -- \"$pid_file\"" in text
    assert "rsync -" not in text and "pip install" not in text

def test_deploy_is_separate_and_never_starts_worker() -> None:
    text = DEPLOY.read_text()
    assert "rsync -a" in text
    assert "nohup" not in text
    assert "ALREADY_READY" in text
    assert "BUNDLE_MANIFEST.sha256" in text

def test_dry_run_has_no_external_side_effects() -> None:
    result = subprocess.run([str(SCRIPTS / "start-reaction-runtime.sh"), "--dry-run"],
                            check=True, text=True, capture_output=True)
    assert "DRY_RUN_COMPLETE" in result.stdout
    for marker in ("robot command NONE", "DDS writer NONE", "resident start NONE", "rsync NONE", "install NONE"):
        assert marker in result.stdout
