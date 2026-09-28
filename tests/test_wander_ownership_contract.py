"""Offline cross-branch contract; no SSH, DDS or hardware initialization."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from g1_bottle_reaction.game_vision.wander_interlock import (
    RemoteWanderController, WanderInterlockError,
)

ROOT = Path(__file__).resolve().parents[1]
FLAG = "--require-locomotion-ownership-v1"
LEGACY = "2d5b04a025db36bb22bed5bde1001c691b49f748"


def approved(runner):
    return RemoteWanderController("unused", robot="g1", enable_real_robot=True,
                                  operator_approved=True, runner=runner)


def shell_body(command):
    words = shlex.split(command[-1])
    assert words[:2] == ["bash", "-lc"]
    return words[2]


def launch_args(command):
    launch = shell_body(command).split("nohup env ", 1)[1].split(" > ", 1)[0]
    words = shlex.split(launch)
    index = next(i for i, word in enumerate(words) if word.endswith(RemoteWanderController.SCRIPT_MARKER))
    return words[index + 1:]


def test_approved_launch_always_carries_contract_without_fallback():
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(2, command, stderr="unrecognized arguments: " + FLAG)
    controller = approved(runner)
    with pytest.raises(WanderInterlockError, match="unrecognized arguments"):
        controller.start()
    assert len(calls) == 1
    assert FLAG in launch_args(calls[0])
    assert not controller.running


@pytest.fixture
def wander_root():
    path = Path(os.environ.get("G1_WANDER_OWNERSHIP_ROOT",
        ROOT.parent / "g1-bottle-reaction-fix-wander-ownership"))
    if not path.is_dir():
        pytest.skip("set G1_WANDER_OWNERSHIP_ROOT for cross-branch validation")
    return path


@pytest.mark.parametrize("legacy", [False, True])
def test_actual_launcher_argv_against_wander_production_main(wander_root, tmp_path, legacy):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "STARTED\n", "")
    approved(runner).start()
    if legacy:
        path = tmp_path / "legacy.py"
        path.write_bytes(subprocess.check_output(["git", "show",
            LEGACY + ":scripts/g1-wander-reactive-mvp.py"], cwd=wander_root))
    else:
        path = wander_root / "scripts/g1-wander-reactive-mvp.py"
    args = launch_args(calls[0]) + ["--config", str(wander_root / "config/wander_live_pc2.json")]
    code = '''
import importlib.util,json,sys
from unittest.mock import Mock
sys.path.insert(0, sys.argv[1])
spec=importlib.util.spec_from_file_location('wander',sys.argv[2])
script=importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)
runtime=Mock()
runtime.create_loco_client.return_value.GetFsmId.return_value=(0,501)
script.body_writer_conflicts=lambda: []
script.JsonlReactiveTelemetry=Mock()
script.run_reactive_mvp=Mock(return_value={'status':'pass'})
args=json.loads(sys.argv[3])
if sys.argv[4]=='legacy':
    try:
        script.main(args,runtime=runtime)
    except SystemExit as exc:
        assert exc.code==2
    else:
        raise AssertionError('legacy accepted ownership flag')
    runtime.create_loco_client.assert_not_called()
    script.JsonlReactiveTelemetry.assert_not_called()
else:
    assert script.main(args,runtime=runtime)==0
    runtime.create_loco_client.assert_called_once()
    script.run_reactive_mvp.assert_called_once()
'''
    result = subprocess.run([sys.executable, "-B", "-c", code, str(wander_root), str(path),
        json.dumps(args), "legacy" if legacy else "migrated"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="local /proc and bash only")
@pytest.mark.parametrize("owned", [False, True])
def test_running_legacy_process_is_not_adopted(tmp_path, owned):
    # Harmless sleeping Python process; marker/flag are inert argv strings.
    child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)",
                              RemoteWanderController.SCRIPT_MARKER, *([FLAG] if owned else [])])
    pid_file = tmp_path / "pid"
    pid_file.write_text(str(child.pid) + "\n")
    calls = []
    def local_runner(command, **kwargs):
        calls.append(command)
        body = shell_body(command).replace(RemoteWanderController.PID_FILE, str(pid_file))
        body = body.replace(RemoteWanderController.LOG_FILE, str(tmp_path / "log"))
        # A regression cannot start any real command through nohup.
        body = "nohup() { echo forbidden-launch >&2; return 99; }; " + body
        return subprocess.run(["bash", "-c", body], **kwargs)
    try:
        controller = approved(local_runner)
        controller.remote_dir = str(tmp_path)
        if owned:
            controller.start()
            assert controller.running
        else:
            with pytest.raises(WanderInterlockError, match="lacks ownership contract"):
                controller.start()
            assert not controller.running
        assert len(calls) == 1
        assert child.poll() is None  # No kill/adoption of legacy processes.
    finally:
        child.kill()
        child.wait(timeout=5)
