from pathlib import Path
import json
import os
import subprocess
import sys
import pytest

from tools.reaction_generator.environment import resolve_environment

ROOT = Path(__file__).resolve().parents[1]


def test_ubuntu_config_routes_each_stage_to_its_wrapper(tmp_path, monkeypatch):
    for name in ('GVHMR_ROOT', 'GMR_ROOT', 'GVHMR_PYTHON', 'GMR_PYTHON'):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path/'environment.local.json'
    config.write_text(json.dumps({
        'backend': 'ubuntu', 'gvhmr_root': '/work/GVHMR', 'gmr_root': '/work/GMR',
        'gvhmr_python': '/work/project/tools/reaction_generator/ubuntu/python-gvhmr',
        'gmr_python': '/work/project/tools/reaction_generator/ubuntu/python-gmr',
    }))
    env = resolve_environment(config)
    assert env.gvhmr_runtime.command('/work/GVHMR/demo.py', ['-s'], cwd='/work/GVHMR') == [
        '/work/project/tools/reaction_generator/ubuntu/python-gvhmr', '/work/GVHMR/demo.py', '-s']
    assert env.gmr_runtime.executable.endswith('/python-gmr')


@pytest.mark.skipif(sys.platform != "linux", reason="Ubuntu shell isolation")
def test_runtime_removes_inherited_install_targets_and_localizes_cache(tmp_path):
    # Mirror the runtime's project-relative location so no real cache is touched.
    target = tmp_path/'tools/reaction_generator/ubuntu'
    target.mkdir(parents=True)
    script = target/'runtime.sh'
    script.write_text((ROOT/'tools/reaction_generator/ubuntu/runtime.sh').read_text())
    env = os.environ.copy()
    env.update(PIP_TARGET='/do-not-touch', PIP_PREFIX='/do-not-touch', PYTHONPATH='/old/venv',
               VIRTUAL_ENV='/old/venv', LD_LIBRARY_PATH='/old/cyclonedds')
    command = 'source "$1"; python3 -c "import os,json; print(json.dumps(dict(os.environ)))"'
    result = subprocess.run(['bash', '-c', command, 'test', str(script)], env=env,
                            capture_output=True, text=True, check=True)
    child = json.loads(result.stdout)
    for name in ('PIP_TARGET', 'PIP_PREFIX', 'PYTHONPATH', 'VIRTUAL_ENV', 'LD_LIBRARY_PATH'):
        assert name not in child
    for name in ('TORCH_HOME', 'HF_HOME', 'XDG_CACHE_HOME', 'PIP_CACHE_DIR', 'TMPDIR'):
        assert child[name].startswith(str(tmp_path)+'/')
    assert env['PIP_TARGET'] == '/do-not-touch'


@pytest.mark.skipif(sys.platform != 'linux', reason='Ubuntu teardown')
def test_teardown_preserves_input_motion_and_repositories(tmp_path):
    scripts = tmp_path/'tools/reaction_generator'
    scripts.mkdir(parents=True)
    script = scripts/'teardown_ubuntu.sh'
    script.write_text((ROOT/'tools/reaction_generator/teardown_ubuntu.sh').read_text())
    for name in ('.reaction-tools', '.reaction-cache', 'input', 'motions', 'GVHMR', 'GMR'):
        (tmp_path/name).mkdir()
        (tmp_path/name/'keep').write_text('fixture')
    result = subprocess.run(['bash', str(script)], input='DELETE\n', capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path/'.reaction-tools').exists()
    assert not (tmp_path/'.reaction-cache').exists()
    for name in ('input', 'motions', 'GVHMR', 'GMR'):
        assert (tmp_path/name/'keep').read_text() == 'fixture'
