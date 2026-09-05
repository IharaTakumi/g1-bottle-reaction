"""Project-local Ubuntu installer. Invoked by setup_ubuntu.sh, stdlib only."""
from pathlib import Path
import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request

PROJECT = Path(__file__).resolve().parents[3]
TOOLS = PROJECT / '.reaction-tools'
MAMBA = TOOLS / 'bin/micromamba'
REVISIONS = {
    'GVHMR': ('https://github.com/zju3dv/GVHMR.git', '6ec3ca39336c50492c0fae65fba2fb831fc7d866'),
    'GMR': ('https://github.com/YanjieZe/GMR.git', 'bb1bbe40774794fceb2a7c579a3464a28e68c844'),
    'pytorch3d': ('https://github.com/facebookresearch/pytorch3d.git', '33824be3cbc87a7dd1db0f6a9a9de9ac81b2d0ba'),
}

def run(*args, **kwargs):
    print('+', ' '.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def download(url, target, sha256=None):
    target = Path(target)
    if target.is_file():
        if sha256 and hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
            raise RuntimeError(f'Checksum mismatch: {target}; existing file preserved')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.partial')
    print('[DOWNLOAD]', url, flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, partial.open('wb') as output:
        expected_size = response.headers.get('Content-Length')
        shutil.copyfileobj(response, output)
    if expected_size is not None and partial.stat().st_size != int(expected_size):
        raise RuntimeError(f'Incomplete download: {partial}; rerun setup to retry')
    if sha256 and hashlib.sha256(partial.read_bytes()).hexdigest() != sha256:
        raise RuntimeError(f'Checksum mismatch: {partial}')
    partial.replace(target)


def checkout(name, root):
    url, revision = REVISIONS[name]
    if not root.exists():
        run('git', 'clone', url, root)
        run('git', '-C', root, 'checkout', '--detach', revision)
    actual = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != revision:
        raise RuntimeError(f'{root}: expected {revision}, found {actual}; checkout left untouched')
    dirty = subprocess.check_output(['git', '-C', str(root), 'diff', 'HEAD', '--name-only'], text=True)
    if dirty.strip():
        raise RuntimeError(f'{root}: tracked modifications found; left untouched')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-downloads', action='store_true')
    args = parser.parse_args()
    capability = subprocess.check_output(['nvidia-smi', '--query-gpu=compute_cap', '--format=csv,noheader'], text=True).splitlines()
    if not capability or any(value.strip() != '12.0' for value in capability):
        raise RuntimeError('This pinned CUDA build targets compute capability 12.0 (RTX 50 series). Review the recipe for this GPU; no driver changes are performed.')
    # Record G1 dependencies once; never overwrite the baseline on reruns.
    g1_python = PROJECT / '.venv-g1/bin/python'
    baseline = TOOLS / 'locks/g1-before.txt'
    if g1_python.is_file() and not baseline.exists():
        baseline.write_bytes(subprocess.check_output([str(g1_python), '-m', 'pip', 'freeze']))
    fingerprints = TOOLS / 'locks/host-before.json'
    if not fingerprints.exists():
        paths = [Path.home()/'.bashrc', Path.home()/'.profile', Path('/usr/bin/python3')]
        fingerprints.write_text(json.dumps({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}, indent=2))
    download('https://github.com/mamba-org/micromamba-releases/releases/download/2.8.1-0/micromamba-linux-64',
             MAMBA, '9689782d863c05a1bf5d2d371ba527104e7a4eb4310c1637d8653b751aed9c82')
    MAMBA.chmod(0o755)
    roots = {name: PROJECT.parent/name for name in ('GVHMR', 'GMR')}
    for name, root in roots.items():
        checkout(name, root)
    config = {'backend': 'ubuntu', **{name.lower()+'_root': str(root) for name, root in roots.items()},
              **{name.lower()+'_revision': REVISIONS[name][1] for name in roots},
              **{name+'_python': str(PROJECT/'tools/reaction_generator/ubuntu'/('python-'+name)) for name in ('gvhmr', 'gmr')}}
    config_path = PROJECT/'tools/reaction_generator/environment.local.json'
    if config_path.exists() and json.loads(config_path.read_text()) != config:
        raise RuntimeError(f'Existing different configuration preserved: {config_path}')
    config_path.write_text(json.dumps(config, indent=2)+'\n')
    for name in ('gvhmr', 'gmr'):
        prefix = TOOLS/'envs'/name
        def mm(*argv):
            return run(MAMBA, '--no-rc', *argv)
        def pip(*argv):
            frozen = Path(__file__).parent/'locks'/(name+'-constraints.txt')
            if argv and argv[0] == 'install' and frozen.is_file():
                argv = (*argv, '-c', frozen)
            return mm('run', '-p', prefix, 'python', '-m', 'pip', '--disable-pip-version-check', *argv)
        stamp = TOOLS/'locks'/(name+'.complete')
        if stamp.exists() and (prefix/'bin/python').exists():
            print(f'[OK] {name} already installed; checking dependencies')
            pip('check')
            continue
        packages = ['python=3.10', 'pip=24.2', 'ffmpeg', 'git', 'make', 'gcc_linux-64=13', 'gxx_linux-64=13', 'libstdcxx-ng']
        if name == 'gvhmr':
            packages += ['cuda-toolkit=12.8']
        frozen_conda = Path(__file__).parent/'locks'/(name+'-conda-explicit.txt')
        if frozen_conda.is_file() and not (prefix/'conda-meta/history').exists():
            mm('create', '-y', '-p', prefix, '--file', frozen_conda)
        else:
            mm('create' if not (prefix/'conda-meta/history').exists() else 'install', '-y', '-p', prefix,
               '--override-channels', '-c', 'conda-forge', *packages)
        pip('install', 'setuptools==80.9.0', 'wheel', 'ninja', 'Cython==3.3.0', 'numpy=='+('1.23.5' if name=='gvhmr' else '1.26.4'))
        if name == 'gvhmr':
            pip('install', '--index-url', 'https://download.pytorch.org/whl/cu128', 'torch==2.7.1', 'torchvision==0.22.1')
            # Retain official requirements except the old GPU-specific pins and wheel.
            lines = (roots['GVHMR']/'requirements.txt').read_text().splitlines()
            excluded = ('--extra-index-url', 'torch==', 'torchvision==', 'pytorch3d @')
            requirements = TOOLS/'locks/gvhmr-ubuntu-requirements.txt'
            requirements.write_text('\n'.join(line for line in lines if not line.startswith(excluded))+'\n'
                                    +'pytorch-lightning==2.3.0\nopencv-python==4.11.0.86\nyacs==0.1.8\n')
            pip('install', '--no-build-isolation', '-r', requirements)
            source = TOOLS/'cache/pytorch3d'
            checkout('pytorch3d', source)
            env = os.environ.copy()
            env.update(CUDA_HOME=str(prefix), FORCE_CUDA='1', TORCH_CUDA_ARCH_LIST='12.0', MAX_JOBS='2')
            run(MAMBA, '--no-rc', 'run', '-p', prefix, 'python', '-m', 'pip', 'install', '--no-build-isolation', str(source), env=env)
            pip('install', '--no-deps', '-e', roots['GVHMR'])
        else:
            pip('install', '--index-url', 'https://download.pytorch.org/whl/cpu', 'torch==2.3.0+cpu', 'torchvision==0.18.0+cpu')
            constraints = TOOLS/'locks/gmr-constraints.txt'
            constraints.write_text('numpy==1.26.4\nopencv-python==4.11.0.86\n')
            tree = ast.parse((roots['GMR']/'setup.py').read_text())
            dependencies = next(ast.literal_eval(keyword.value)
                                for node in ast.walk(tree) if isinstance(node, ast.Call)
                                for keyword in node.keywords if keyword.arg == 'install_requires')
            dependencies = [dep + '@1265df7ba545e8b00f72e7c557c766e15c71632f'
                            if dep == 'smplx @ git+https://github.com/vchoutas/smplx' else dep
                            for dep in dependencies]
            requirements = TOOLS/'locks/gmr-ubuntu-requirements.txt'
            requirements.write_text('\n'.join(dependencies)+'\n')
            pip('install', '-c', constraints, '-r', requirements)
            pip('install', '--no-deps', '-e', roots['GMR'])
        pip('check')
        with (TOOLS/'locks'/(name+'.txt')).open('w') as output:
            run(MAMBA, '--no-rc', 'run', '-p', prefix, 'python', '-m', 'pip', 'freeze', '--all', stdout=output)
        with (TOOLS/'locks'/(name+'-conda-explicit.txt')).open('w') as output:
            run(MAMBA, '--no-rc', 'env', 'export', '-p', prefix, '--explicit', stdout=output)
        stamp.write_text(REVISIONS[name.upper()][1]+'\n')
    if not args.skip_downloads:
        for relative in ('gvhmr/gvhmr_siga24_release.ckpt', 'hmr2/epoch=10-step=25000.ckpt', 'vitpose/vitpose-h-multi-coco.pth', 'yolo/yolov8x.pt'):
            url = 'https://huggingface.co/camenduru/GVHMR/resolve/main/'+relative.replace('=', '%3D')
            download(url, roots['GVHMR']/'inputs/checkpoints'/relative)
    print('[INFO] Licensed SMPL/SMPL-X files are never automatically downloaded.')
    print('SMPL: https://smpl.is.tue.mpg.de/ ; SMPL-X: https://smpl-x.is.tue.mpg.de/')
    return subprocess.call([sys.executable, str(PROJECT/'tools/reaction_generator/doctor.py')])

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        raise SystemExit(2)
