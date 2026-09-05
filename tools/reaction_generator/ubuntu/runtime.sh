#!/usr/bin/env bash
# Source only in a subprocess. Never modifies the calling interactive shell.
reaction_project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
export MAMBA_ROOT_PREFIX="$reaction_project/.reaction-tools/cache/mamba"
export CONDA_PKGS_DIRS="$reaction_project/.reaction-tools/cache/pkgs"
export TORCH_HOME="$reaction_project/.reaction-cache/torch"
export HF_HOME="$reaction_project/.reaction-cache/huggingface"
export XDG_CACHE_HOME="$reaction_project/.reaction-cache"
export XDG_CONFIG_HOME="$reaction_project/.reaction-cache/config"
export XDG_DATA_HOME="$reaction_project/.reaction-cache/data"
export PIP_CACHE_DIR="$reaction_project/.reaction-tools/cache/pip"
export PIP_CONFIG_FILE=/dev/null
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$reaction_project/.reaction-tools/cache/tmp"
export MPLCONFIGDIR="$reaction_project/.reaction-cache/matplotlib"
export YOLO_CONFIG_DIR="$reaction_project/.reaction-cache/ultralytics"
export MUJOCO_GL="${REACTION_MUJOCO_GL:-egl}"
if [[ "$MUJOCO_GL" == glfw ]]; then
  export PYOPENGL_PLATFORM=glx
else
  export PYOPENGL_PLATFORM="$MUJOCO_GL"
fi
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MAX_JOBS=2
# Clear inherited G1/venv/compiler/pip paths; installation must target only -p.
unset VIRTUAL_ENV PYTHONPATH PYTHONHOME PIP_REQUIRE_VIRTUALENV PIP_TARGET PIP_PREFIX PIP_USER
unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_NO_INDEX PIP_FIND_LINKS PIP_CONSTRAINT PIP_REQUIREMENT
unset CC CXX CPP CFLAGS CXXFLAGS LDFLAGS CUDAHOSTCXX CPATH LIBRARY_PATH
unset CONDA_PREFIX CONDA_DEFAULT_ENV CUDA_HOME CUDA_PATH CMAKE_PREFIX_PATH LD_LIBRARY_PATH
export PATH=/usr/bin:/bin
mkdir -p "$MAMBA_ROOT_PREFIX" "$CONDA_PKGS_DIRS" "$TORCH_HOME" "$HF_HOME" \
 "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$PIP_CACHE_DIR" "$TMPDIR" "$MPLCONFIGDIR" "$YOLO_CONFIG_DIR"
