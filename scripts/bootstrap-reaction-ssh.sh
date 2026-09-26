#!/usr/bin/env bash
set -euo pipefail

script_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/reaction-ssh.sh
source "$script_root/scripts/reaction-ssh.sh"
reaction_ssh_init

if [[ ${1:-} == --dry-run ]]; then
  echo "REACTION_SSH_TARGET=$REACTION_SSH_TARGET"
  echo "REACTION_SSH_CONTROL=$REACTION_SSH_CONTROL"
  echo "DRY_RUN: no SSH connection or ControlMaster created"
  exit 0
fi
[[ $# == 0 ]] || { echo "usage: $0 [--dry-run]" >&2; exit 2; }

if [[ -S $REACTION_SSH_CONTROL ]]; then
  if "$REACTION_SSH_BIN" -o BatchMode=yes -o ConnectTimeout=3 \
    -S "$REACTION_SSH_CONTROL" -O check -- "$REACTION_SSH_TARGET" >/dev/null 2>&1; then
    echo "REACTION_SSH_MASTER_REUSED=$REACTION_SSH_CONTROL"
    exit 0
  fi
  stale="$REACTION_SSH_CONTROL.stale.$(date +%Y%m%d%H%M%S)"
  mv -- "$REACTION_SSH_CONTROL" "$stale"
  echo "STALE_REACTION_SSH_SOCKET_MOVED=$stale"
fi

mkdir -p -- "$(dirname -- "$REACTION_SSH_CONTROL")"
echo "Interactive SSH authentication may be requested for $REACTION_SSH_TARGET."
"$REACTION_SSH_BIN" -MNf -o ConnectTimeout=10 -o ControlMaster=yes \
  -o ControlPersist=10m -S "$REACTION_SSH_CONTROL" -- "$REACTION_SSH_TARGET"
[[ -S $REACTION_SSH_CONTROL ]] || { echo REACTION_SSH_BOOTSTRAP_FAILED >&2; exit 3; }
"$REACTION_SSH_BIN" -o BatchMode=yes -o ConnectTimeout=3 \
  -S "$REACTION_SSH_CONTROL" -O check -- "$REACTION_SSH_TARGET" >/dev/null
echo "REACTION_SSH_BOOTSTRAP_READY=$REACTION_SSH_CONTROL"
