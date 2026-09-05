#!/usr/bin/env bash
# Explicit preparation only. Writes a NEW project-local venv; never runs DDS/RPC.
set -eu
TASK_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
TASK_PYTHON="${G1_BASE_PYTHON:-/usr/bin/python3}"
TASK_VENV="$TASK_ROOT/.venv-robot"
if [ "$(uname -m)" != aarch64 ]; then
  echo 'Run preparation on PC2 aarch64 only.' >&2
  exit 2
fi
if [ -e "$TASK_VENV" ]; then
  echo 'Existing .venv-robot will not be modified. Run doctor with its interpreter.' >&2
  exit 2
fi
# Inherit existing SDK dependencies read-only, avoiding a second DDS binary stack.
# If python3-venv/ensurepip is missing, stop; do not install system packages.
"$TASK_PYTHON" -m venv --system-site-packages "$TASK_VENV"
PYTHONDONTWRITEBYTECODE=1 "$TASK_VENV/bin/python" -B "$TASK_ROOT/scripts/g1-pc2-doctor.py"
