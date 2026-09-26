from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import socket
import subprocess

import pytest
import yaml

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
CONFIG = ROOT / "config/vision_runtime.yaml"
START = SCRIPTS / "start-vision-runtime.sh"
CHECK = SCRIPTS / "check-vision-ready.sh"
STOP = SCRIPTS / "stop-vision-runtime.sh"
SSH_LIB = SCRIPTS / "vision-ssh.sh"

spec = importlib.util.spec_from_file_location("vision_runtime_validate", SCRIPTS / "vision-runtime-validate.py")
validator = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(validator)


def config_for(tmp_path: Path, model: Path) -> dict:
    data = yaml.safe_load(CONFIG.read_text())
    data["model"] = str(model)
    data["model_sha256"] = hashlib.sha256(b"trusted-model").hexdigest()
    return data


def ready_log(config: dict) -> str:
    return (
        "YOLO device: CUDA / Test GPU\n"
        f"Model: {config['model']}; person confidence=0.25; classes=[0,46,77]\n"
        "g1: 1920x1080, FPS=12.5, local_age=30.0ms, frames=20, "
        "display_skips=1, RTP_lost=0, status=LIVE\n"
        "display_loop FPS=90.0; YOLO FPS=8.0, inference=12.5ms, "
        "result_age=40.0ms, status=RUNNING\n"
    )


def test_fixed_model_sha_missing_and_mismatch_fail(tmp_path: Path) -> None:
    model = tmp_path / "model.pt"
    config = config_for(tmp_path, model)
    with pytest.raises(ValueError, match="VISION_MODEL_MISSING"):
        validator.verify_model(config)
    model.write_bytes(b"wrong")
    with pytest.raises(ValueError, match="VISION_MODEL_MISMATCH"):
        validator.verify_model(config)
    model.write_bytes(b"trusted-model")
    assert validator.verify_model(config)["sha256"] == config["model_sha256"]


def test_camera_fresh_frames_and_yolo_warmup_are_required(tmp_path: Path) -> None:
    model = tmp_path / "model.pt"
    model.write_bytes(b"trusted-model")
    config = config_for(tmp_path, model)
    log = tmp_path / "vision.log"
    log.write_text(ready_log(config))
    health = validator.validate_health(config, log)
    assert health["resolution"] == "1920x1080"
    assert health["frames"] >= 3
    assert health["inference_ms"] == 12.5  # Empty detections are still READY.
    log.write_text(ready_log(config).replace("local_age=30.0ms", "local_age=900.0ms"))
    with pytest.raises(ValueError, match="frame_age"):
        validator.validate_health(config, log)
    log.write_text(ready_log(config).replace("YOLO device: CUDA / Test GPU\n", ""))
    with pytest.raises(ValueError, match="cuda"):
        validator.validate_health(config, log)
    log.write_text(ready_log(config).replace("status=RUNNING", "status=WAITING"))
    with pytest.raises(ValueError, match="yolo_status"):
        validator.validate_health(config, log)


def test_transport_fatal_and_stale_status_fail_closed(tmp_path: Path) -> None:
    model = tmp_path / "model.pt"
    model.write_bytes(b"trusted-model")
    config = config_for(tmp_path, model)
    log = tmp_path / "vision.log"
    log.write_text(ready_log(config) + "CAMERA WORKER ERROR: failure\n")
    with pytest.raises(ValueError, match="CAMERA_TRANSPORT_NOT_READY"):
        validator.validate_health(config, log)
    log.write_text(ready_log(config))
    old = log.stat().st_mtime - 30
    os.utime(log, (old, old))
    with pytest.raises(ValueError, match="VISION_STATUS_STALE"):
        validator.validate_health(config, log)


def test_production_transport_scope_and_lifecycle_are_fail_closed() -> None:
    start = START.read_text()
    check = CHECK.read_text()
    stop = STOP.read_text()
    assert yaml.safe_load(CONFIG.read_text())["camera_transport"] == "ssh-rtp"
    assert "HEALTHY_VISION_PROCESS_REUSED" in start
    assert "VISION_PID_OR_PROCESS_STALE_FAIL_CLOSED" in start
    assert "CAMERA_PROCESS_CONFLICT" in start
    assert "--found-audio" not in start and "--with-wander" not in start
    assert "--enable-real-robot" not in start
    assert "pip " not in start and "wget " not in start and "curl " not in start
    assert "kill -INT -- \"-$pid\"" in stop
    kill_lines = [line for line in stop.splitlines() if line.strip().startswith("kill ")]
    assert "kill -9" not in stop
    assert all("videohub" not in line.lower() and "reaction" not in line.lower()
               and "wander" not in line.lower() for line in kill_lines)
    assert "Camera frames: PASS" in check and "Warm-up inference: PASS" in check


def test_vision_ssh_is_owned_and_does_not_reference_reaction() -> None:
    combined = "\n".join(path.read_text() for path in (SSH_LIB, START, CHECK))
    assert ".runtime/ssh/g1-control" in combined
    assert "g1-reaction-runtime" not in combined
    assert "usb-camera-ssh" not in combined
    assert "sshpass" not in combined
    assert "StrictHostKeyChecking=yes" in combined
    assert "StrictHostKeyChecking=no" not in combined


def test_dry_run_has_no_side_effects(tmp_path: Path) -> None:
    control = tmp_path / "ssh" / "g1-control"
    result = subprocess.run(
        [str(START), "--dry-run"], check=True, text=True, capture_output=True,
        env=os.environ | {"VISION_SSH_CONTROL": str(control)},
    )
    assert "camera start NONE" in result.stdout
    assert "YOLO start NONE" in result.stdout
    assert "download NONE" in result.stdout and "install NONE" in result.stdout
    assert str(control) in result.stdout
    assert not control.parent.exists()


def _fake_ssh(tmp_path: Path) -> tuple[Path, Path]:
    executable, log = tmp_path / "fake-ssh", tmp_path / "ssh.log"
    executable.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SSH_TEST_LOG\"\n"
        "case \" $* \" in *' -O check '*) exit \"${SSH_CHECK_RC:-0}\";; "
        "*' true '*) exit \"${SSH_DIRECT_RC:-0}\";; esac\nexit 0\n"
    )
    executable.chmod(0o755)
    return executable, log


def test_healthy_vision_master_reused_and_stale_not_trusted(tmp_path: Path) -> None:
    fake, calls = _fake_ssh(tmp_path)
    control = tmp_path / "control"
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(control))
        base = os.environ | {"VISION_SSH_BIN": str(fake), "VISION_SSH_CONTROL": str(control),
                             "SSH_TEST_LOG": str(calls)}
        command = ["bash", "-c", 'source "$1"; vision_ssh_ensure', "bash", str(SSH_LIB)]
        good = subprocess.run(command, env=base, text=True, capture_output=True)
        stale = subprocess.run(command, env=base | {"SSH_CHECK_RC": "1"}, text=True, capture_output=True)
    finally:
        sock.close()
    assert good.returncode == 0 and "VISION_SSH_MASTER_REUSED" in good.stdout
    assert stale.returncode == 41 and "SSH_STALE_VISION_CONTROL_SOCKET" in stale.stderr
    assert control.exists()
