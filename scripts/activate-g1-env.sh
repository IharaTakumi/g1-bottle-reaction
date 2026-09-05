#!/usr/bin/env bash
# Source this file; no persistent user or OS configuration is changed.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo 'Use: source scripts/activate-g1-env.sh' >&2
  exit 2
fi
_g1_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "$_g1_root/.venv-g1/bin/activate" ]]; then
  echo 'Missing .venv-g1; see docs/G1_LOCAL_ENV.md' >&2
  unset _g1_root
  return 1
fi
source "$_g1_root/.venv-g1/bin/activate"
export G1_PROJECT_ROOT="$_g1_root"
export PIP_REQUIRE_VIRTUALENV=true
export PIP_CACHE_DIR="$_g1_root/.runtime/pip-cache"
export XDG_CACHE_HOME="$_g1_root/.runtime/cache"
export XDG_CONFIG_HOME="$_g1_root/.runtime/config"
export XDG_DATA_HOME="$_g1_root/.runtime/data"
export TMPDIR="$_g1_root/.runtime/tmp"
export UV_CACHE_DIR="$_g1_root/.runtime/cache/uv"
export UV_PYTHON_INSTALL_DIR="$_g1_root/.runtime/python"
export UV_PYTHON_BIN_DIR="$_g1_root/.runtime/bin"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export CYCLONEDDS_HOME="$_g1_root/.runtime/cyclonedds"
export CMAKE_PREFIX_PATH="$CYCLONEDDS_HOME${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
export LD_LIBRARY_PATH="$CYCLONEDDS_HOME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export G1_NETWORK_INTERFACE=enp129s0
export CYCLONEDDS_URI="file://$_g1_root/config/g1-readonly-dds.xml"
mkdir -p "$PIP_CACHE_DIR" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$TMPDIR"
unset _g1_root
