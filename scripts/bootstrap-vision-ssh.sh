#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/vision-ssh.sh
source "$root/scripts/vision-ssh.sh"
vision_ssh_init
if [[ ${1:-} == --dry-run ]]; then
  echo "VISION_SSH_TARGET=$VISION_SSH_TARGET"
  echo "VISION_SSH_CONTROL=$VISION_SSH_CONTROL"
  echo "DRY_RUN: SSH master generation NONE; credentials stored NONE"
  exit 0
fi
[[ $# == 0 ]] || { echo "usage: $0 [--dry-run]" >&2; exit 2; }
if [[ -S $VISION_SSH_CONTROL ]]; then
  if "$VISION_SSH_BIN" -o StrictHostKeyChecking=yes -o BatchMode=yes \
    -o ConnectTimeout=3 -S "$VISION_SSH_CONTROL" -O check -- \
    "$VISION_SSH_TARGET" >/dev/null 2>&1; then
    echo "VISION_SSH_MASTER_REUSED=$VISION_SSH_CONTROL"
    exit 0
  fi
  stale="$VISION_SSH_CONTROL.stale.$(date +%Y%m%d%H%M%S)"
  mv -- "$VISION_SSH_CONTROL" "$stale"
  echo "STALE_VISION_SSH_SOCKET_MOVED=$stale"
fi
mkdir -p -- "$(dirname -- "$VISION_SSH_CONTROL")"
echo "Interactive SSH authentication may be requested for $VISION_SSH_TARGET."
"$VISION_SSH_BIN" -MNf -o StrictHostKeyChecking=yes -o ConnectTimeout=10 \
  -o ControlMaster=yes -o ControlPersist=10m -S "$VISION_SSH_CONTROL" \
  -- "$VISION_SSH_TARGET"
[[ -S $VISION_SSH_CONTROL ]] || { echo VISION_SSH_BOOTSTRAP_FAILED >&2; exit 3; }
"$VISION_SSH_BIN" -o StrictHostKeyChecking=yes -o BatchMode=yes \
  -o ConnectTimeout=3 -S "$VISION_SSH_CONTROL" -O check -- \
  "$VISION_SSH_TARGET" >/dev/null
echo "VISION_SSH_BOOTSTRAP_READY=$VISION_SSH_CONTROL"
