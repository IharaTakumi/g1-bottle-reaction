from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess

import pytest

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
START = SCRIPTS / "start-motiondecode-resident.sh"
DEPLOY = SCRIPTS / "deploy-motiondecode-runtime.sh"
SSH_LIB = SCRIPTS / "reaction-ssh.sh"

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

def _fake_ssh(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "ssh.log"
    executable = tmp_path / "ssh"
    executable.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >>\"$SSH_TEST_LOG\"\n"
        "case \" $* \" in\n"
        "  *' -O check '*) exit \"${SSH_CHECK_RC:-0}\";;\n"
        "  *' true '*) exit \"${SSH_DIRECT_RC:-0}\";;\n"
        "  *' -MNf '*) exit 99;;\n"
        "esac\n"
        "exit 0\n"
    )
    executable.chmod(0o755)
    return executable, log

def _ensure_ssh(tmp_path: Path, control: Path, *, check: int = 0, direct: int = 0):
    fake, log = _fake_ssh(tmp_path)
    env = os.environ | {
        "REACTION_SSH_BIN": str(fake), "REACTION_SSH_CONTROL": str(control),
        "SSH_TEST_LOG": str(log), "SSH_CHECK_RC": str(check), "SSH_DIRECT_RC": str(direct),
    }
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; reaction_ssh_ensure', "bash", str(SSH_LIB)],
        text=True, capture_output=True, env=env,
    )
    return result, log.read_text() if log.exists() else ""

def test_healthy_reaction_owned_master_is_reused(tmp_path: Path) -> None:
    control = tmp_path / "reaction-control"
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(control))
        result, calls = _ensure_ssh(tmp_path, control)
    finally:
        sock.close()
    assert result.returncode == 0
    assert "REACTION_SSH_MASTER_REUSED" in result.stdout
    assert "-O check" in calls and "-MNf" not in calls

def test_stale_reaction_socket_is_not_trusted_or_removed(tmp_path: Path) -> None:
    control = tmp_path / "reaction-control"
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(control))
        result, calls = _ensure_ssh(tmp_path, control, check=1)
        assert control.exists()
    finally:
        sock.close()
    assert result.returncode == 41
    assert "SSH_STALE_REACTION_CONTROL_SOCKET" in result.stderr
    assert "-MNf" not in calls

def test_batch_mode_auth_failure_requests_explicit_bootstrap(tmp_path: Path) -> None:
    control = tmp_path / "runtime" / "reaction-control"
    result, calls = _ensure_ssh(tmp_path, control, direct=255)
    assert result.returncode == 42
    assert "SSH_AUTH_BOOTSTRAP_REQUIRED" in result.stderr
    assert "-MNf" not in calls
    assert not control.parent.exists()

def test_no_camera_ssh_dependency_or_weakened_host_keys() -> None:
    production_scripts = [DEPLOY, START, SCRIPTS / "check-reaction-ready.sh", SSH_LIB]
    combined = "\n".join(path.read_text() for path in production_scripts)
    assert "usb-camera-ssh" not in combined
    assert "StrictHostKeyChecking=no" not in combined
    assert "UserKnownHostsFile=/dev/null" not in combined
    assert "sshpass" not in combined
    assert "password=" not in combined.lower()
    assert ".runtime/ssh/g1-control" in SSH_LIB.read_text()

def test_dry_run_does_not_create_reaction_master(tmp_path: Path) -> None:
    control = tmp_path / "ssh" / "reaction-control"
    result = subprocess.run(
        [str(SCRIPTS / "start-reaction-runtime.sh"), "--dry-run"],
        check=True, text=True, capture_output=True,
        env=os.environ | {"REACTION_SSH_CONTROL": str(control)},
    )
    assert str(control) in result.stdout
    assert not control.exists()
    assert not control.parent.exists()
