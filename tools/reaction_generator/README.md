# Video-to-G1 upper-body reaction generator

This is an offline authoring pipeline. It converts a short human video through
[GVHMR](https://github.com/zju3dv/GVHMR) and
[GMR](https://github.com/YanjieZe/GMR), then keeps only the 14 arm joints of the
Unitree G1 29-DoF model. It does not connect to a robot and does
not implement real-time imitation.

## One-time setup

The supported Windows path is WSL2 plus an NVIDIA CUDA GPU. Keep `.venv-g1` as
the game environment; the setup creates isolated Python 3.10 environments
under `~/.local/share/g1-reaction-generator` inside WSL. GVHMR gets its pinned
PyTorch 2.3.0/CUDA 12.1/PyTorch3D stack, while GMR gets a separate CPU PyTorch
environment. No package in `.venv-g1` is installed, removed, or upgraded.

From PowerShell, run once:

```powershell
powershell -ExecutionPolicy Bypass -File tools\reaction_generator\setup.ps1
```

On a new machine, install an `Ubuntu-22.04` WSL2 distribution first. The setup
auto-selects that name when it is installed, otherwise it uses the existing
`Ubuntu` distribution. Use `-WslDistribution <name>` to override the choice.

The setup pins the tested external revisions, bootstraps a checksum-verified
micromamba, installs both environments, downloads the four redistributable
inference checkpoints used by the upstream GVHMR Colab, records package locks,
and saves machine-local paths in the ignored
`tools/reaction_generator/environment.local.json` file. Existing clean clones
at `C:\dev\GVHMR` and `C:\dev\GMR` are reused.

SMPL and SMPL-X data is license-gated and is never downloaded automatically.
Download it from the official model sites and place these files exactly:

```text
C:\dev\GVHMR\inputs\checkpoints\body_models\smplx\SMPLX_NEUTRAL.npz
C:\dev\GVHMR\inputs\checkpoints\body_models\smpl\SMPL_NEUTRAL.pkl
C:\dev\GMR\assets\body_models\smplx\SMPLX_NEUTRAL.npz
```

The final NPZ can be the same licensed SMPL-X neutral model copied to the GMR
location. Rerun `setup.ps1` after placing the files; completed downloads and
environments are reused.

Check everything in one pass:

```powershell
python tools\reaction_generator\doctor.py
python tools\reaction_generator\generate.py --doctor
```

The doctor reports the host Python/PyTorch, WSL release, WSL-visible NVIDIA
GPU, both isolated interpreters, CUDA/PyTorch/PyTorch3D, MuJoCo, ffmpeg,
checkouts, checkpoints, and licensed body models. An error exits nonzero.

| Environment | Intended use | Assessment |
| --- | --- | --- |
| WSL2 Ubuntu 22.04 | Full pipeline on a Windows PC with NVIDIA CUDA | Recommended: closest to upstream Linux/CUDA instructions while keeping Windows asset access |
| Native Ubuntu | Full pipeline on a dedicated workstation | Equally suitable and simpler at the CUDA boundary, but requires moving the input/output files |
| Native Windows | Cleaner, tests, asset inspection, MuJoCo | Supported for the lightweight stages only; do not merge the ML stack into the game environment |
| Docker/WSL2 | Pinned full-pipeline image | Potentially reproducible, but deferred until the upstream checkpoints/body-model mounts and working versions have been proven once |

Run the complete pipeline:

```powershell
python tools\reaction_generator\generate.py input\surprised_01.mp4 --name surprised
```

`generate.py` reads the saved configuration and calls both WSL interpreters;
the user does not activate either environment. `--gvhmr-root`, `--gmr-root`,
`--gvhmr-python`, and `--gmr-python` remain available as explicit overrides.

Use `--static-camera` only when the source camera was physically static. It
passes GVHMR's documented `-s` option. Use `--dry-run` to print the external
commands without loading models or writing output.

The intermediate is stored below `.cache/reaction_generator/<name>/` and the
final asset is `motions/<name>.npz`. To clean a copied GMR intermediate on
Windows without installing GVHMR or GMR:

```powershell
python tools/reaction_generator/generate.py input/surprised.mp4 `
  --name surprised --from-gmr-npz input/surprised_g1.npz
```

Inspect without MuJoCo, or preview with the optional `sim` dependency and the
external Unitree MuJoCo checkout already used by this project:

```bash
python tools/reaction_generator/preview_motion.py motions/surprised.npz --inspect-only
python tools/reaction_generator/preview_motion.py motions/surprised.npz
python tools/reaction_generator/preview_motion.py motions/surprised.npz --headless
```

## Safety boundary

The final file is a non-pickle NumPy archive containing canonical joint names,
50 Hz relative position offsets, format metadata, and source revisions. Root
pose, translation, all 12 leg joints, and all 3 waist joints are discarded.
Cleaning applies
official G1 joint ranges, configurable amplitude scaling, zero-phase smoothing,
blend-in/out, offset caps, and conservative per-group velocity caps from
`config/reaction_motion.yaml`.

Joint order is based on Unitree's official
[G1 29-DoF DDS index table](https://github.com/unitreerobotics/unitree_mujoco/blob/main/unitree_robots/g1/g1_joint_index_dds.md),
and ranges are taken from the official
[G1 29-DoF model](https://github.com/unitreerobotics/unitree_ros/blob/master/robots/g1_description/g1_29dof_rev_1_0.xml).
The local code still selects GMR columns by emitted joint name, never by an
assumed intermediate array index.

The asset is relative to the future runtime start pose. A real-robot player is
deliberately not implemented in this phase. That player must read fresh
LowState, reject `start_pose + offset` values outside hardware limits, obtain
exclusive arm control, ramp control weight, monitor state throughout playback,
and release ownership on completion or error. MuJoCo contact/range inspection
is useful authoring feedback, not proof of balance, collision safety, torque
safety, or safe execution on hardware.

GMR's upstream demo creates an interactive viewer and saves data without a
stable joint-name contract. The local retarget worker instead calls GMR's
official Python API in its own environment and writes a joint-name-bearing,
non-pickle intermediate. The retargeting/IK algorithm remains wholly upstream.
The integration follows GMR's official
[`gvhmr_to_robot.py`](https://github.com/YanjieZe/GMR/blob/master/scripts/gvhmr_to_robot.py)
flow with `tgt_robot="unitree_g1"` and its documented 30 Hz alignment, before
the local cleaner resamples the selected upper body to 50 Hz.

## Why native Windows is not used

The pinned GVHMR requirements use the upstream Linux-only
`pytorch3d-0.7.6` wheel built for Python 3.10, CUDA 12.1, and PyTorch 2.3.0.
PyTorch3D's own Windows instructions require a matching Visual Studio compiler
toolchain and may require PyTorch-header patches. GVHMR also contains explicit
`.cuda()` calls in preprocessing, inference, and rendering, so Intel/AMD-only
machines are not a supported CPU fallback. `setup.ps1` therefore checks CUDA
before downloading several gigabytes and stops with a concise explanation when
no NVIDIA GPU is visible inside WSL. `-SkipGpuCheck` is intended only for
dependency inspection and cannot make GVHMR run on CPU.
