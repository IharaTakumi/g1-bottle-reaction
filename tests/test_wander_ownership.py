"""Offline only: fake SDK, actual processes/flock, strict legacy parser."""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import time
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
GIT_ROOT = Path(os.environ.get("G1_REVIEW_GIT_ROOT", ROOT))
PHASE1A = "cf1e4d94e75fc9bb64d9941fba863b11dbbb0120"
LEGACY = "2d5b04a025db36bb22bed5bde1001c691b49f748"
FLAG = "--require-locomotion-ownership-v1"
APPROVAL = ["--robot", "g1", "--enable-real-robot", "--execute-real-g1",
            "--i-understand-this-will-move-the-robot"]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git_source(commit, path):
    return subprocess.check_output(["git", "show", commit + ":" + path], cwd=GIT_ROOT)


def test_lock_is_identical_to_reviewed_phase1a():
    assert (ROOT / "robot_side/ownership_lock.py").read_bytes() == git_source(
        PHASE1A, "patrol/ownership_lock.py")


def test_missing_contract_rejected_before_runtime_or_probe(monkeypatch):
    script = load("wander_gate", ROOT / "scripts/g1-wander-reactive-mvp.py")
    runtime, probe = Mock(), Mock(side_effect=AssertionError("probe forbidden"))
    monkeypatch.setattr(script, "body_writer_conflicts", probe)
    with pytest.raises(ValueError, match="require-locomotion-ownership-v1"):
        script.main(APPROVAL, runtime=runtime)
    runtime.create_loco_client.assert_not_called()
    probe.assert_not_called()


def test_new_script_with_old_runtime_rejects_before_sdk(monkeypatch):
    script = load("mixed_wander", ROOT / "scripts/g1-wander-reactive-mvp.py")
    initialized = Mock()
    class LegacyRuntime:
        def create_loco_client(self, interface, timeout):
            initialized()
    monkeypatch.setattr(script, "body_writer_conflicts", lambda: [])
    with pytest.raises(TypeError, match="require_locomotion_ownership_v1"):
        script.main(APPROVAL + [FLAG], runtime=LegacyRuntime())
    initialized.assert_not_called()


@pytest.mark.parametrize("missing", ["--robot", "--enable-real-robot",
    "--execute-real-g1", "--i-understand-this-will-move-the-robot"])
def test_contract_does_not_replace_existing_approval(missing, monkeypatch):
    script = load("wander_approval", ROOT / "scripts/g1-wander-reactive-mvp.py")
    runtime = Mock()
    monkeypatch.setattr(script, "body_writer_conflicts", Mock(side_effect=AssertionError("probe")))
    args = [a for a in APPROVAL if a != missing and not (missing == "--robot" and a == "g1")]
    assert script.main(args + [FLAG], runtime=runtime) == 0
    runtime.create_loco_client.assert_not_called()


def test_legacy_production_main_rejects_contract_before_sdk(tmp_path):
    source = git_source(LEGACY, "scripts/g1-wander-reactive-mvp.py")
    path = tmp_path / "legacy.py"
    path.write_bytes(source)
    script = load("legacy_wander", path)
    runtime = Mock()
    with pytest.raises(SystemExit) as failure:
        script.main(APPROVAL + [FLAG], runtime=runtime)
    assert failure.value.code == 2
    runtime.create_loco_client.assert_not_called()


@pytest.fixture
def processes(tmp_path):
    if sys.platform != "linux":
        pytest.skip("actual Linux flock/process test")
    phase1a = tmp_path / "phase1a_lock.py"
    phase1a.write_bytes(git_source(PHASE1A, "patrol/ownership_lock.py"))
    lock = tmp_path / "canonical.lock"
    children = []

    def start(mode):
        directory = tmp_path / str(len(children))
        directory.mkdir()
        log = directory / "events.jsonl"
        proc = subprocess.Popen([sys.executable, "-B", str(ROOT / "tests/ownership_process.py"),
            str(ROOT), str(lock), str(log), mode, str(phase1a)], cwd=directory,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(proc)
        return proc, log
    yield start, lock
    for proc in children:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def wait_for(proc, log, event):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(row[0] == event for row in events(log)):
            return
        if proc.poll() is not None:
            pytest.fail(str(proc.communicate()))
        time.sleep(.01)
    pytest.fail("child did not report " + event)


@pytest.mark.parametrize("first,second", [("patrol", "pulse"), ("wander", "patrol")])
def test_actual_cross_writer_contention_before_sdk_and_crash_release(processes, first, second):
    start, lock = processes
    a, a_log = start(first)
    wait_for(a, a_log, "GC_DONE" if first == "wander" else "SDK_INIT")
    inode = lock.stat().st_ino
    b, b_log = start(second)
    _, error = b.communicate(timeout=5)
    assert b.returncode != 0 and "BlockingIOError" in error
    assert events(b_log) == []  # DDS/SDK initialization and SetVelocity are all zero.
    a.kill()
    a.wait(timeout=5)
    c, c_log = start("patrol")
    wait_for(c, c_log, "SDK_INIT")
    assert lock.stat().st_ino == inode


@pytest.mark.parametrize("mode", ["pulse", "exception", "signal"])
def test_cli_sdk_pulse_stop_and_process_lock_release(processes, mode):
    start, lock = processes
    proc, log = start(mode)
    wait_for(proc, log, "SetVelocity")
    if mode == "signal":
        proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=5)
    assert proc.returncode == {"pulse": 0, "exception": 2, "signal": 143}[mode]
    recorded = events(log)
    assert recorded[0:2] == [["DDS_INIT"], ["SDK_INIT"]]
    pulses = [row for row in recorded if row[0] == "SetVelocity"]
    assert pulses == [["SetVelocity", .2, 0, 0, .5]]
    assert any(row[0] == "StopMove" for row in recorded)
    assert recorded[-1] == ["TELEMETRY_CLOSE"]
    inode = lock.stat().st_ino
    other, other_log = start("patrol")
    wait_for(other, other_log, "SDK_INIT")
    assert lock.stat().st_ino == inode


def test_bundle_contains_ownership_and_runs_dry_without_desktop_tree(tmp_path):
    builder = load("bundle", ROOT / "scripts/build-wander-ownership-bundle.py")
    archive = tmp_path / "bundle.tar.gz"
    manifest = builder.build(archive, root=GIT_ROOT)
    unpacked = tmp_path / "deployment"
    unpacked.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(unpacked, filter="data")
    assert manifest["canonical_lock"] == "/tmp/g1-project-locomotion.lock"
    assert manifest["ownership_contract"] == FLAG
    for name, digest in manifest["sha256"].items():
        assert hashlib.sha256((unpacked / name).read_bytes()).hexdigest() == digest
        if name.endswith(".py"):
            ast.parse((unpacked / name).read_text(), feature_version=(3, 8))
    env = dict(os.environ, PYTHONPATH="", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-B", str(unpacked / "scripts/g1-wander-reactive-mvp.py")],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert "NO G1 COMMAND SENT" in result.stdout
