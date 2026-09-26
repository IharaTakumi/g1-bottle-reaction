#!/usr/bin/env bash
set -euo pipefail
script_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ ${1:-} == --dry-run ]]; then
  "$script_root/scripts/start-motiondecode-resident.sh" --dry-run
  "$script_root/scripts/check-reaction-ready.sh" --dry-run
  echo "DRY_RUN_COMPLETE: robot command NONE; reaction NONE; audio NONE; DDS writer NONE; resident start NONE; rsync NONE; install NONE"
  exit 0
fi
[[ $# == 0 ]] || { echo "usage: $0 [--dry-run]" >&2; exit 2; }
"$script_root/scripts/start-motiondecode-resident.sh"
"$script_root/scripts/check-reaction-ready.sh"
