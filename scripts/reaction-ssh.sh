#!/usr/bin/env bash
# Shared, Reaction-owned SSH connection management. Source this file.

reaction_ssh_init() {
  local root
  root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
  REACTION_SSH_TARGET=${G1_SSH_TARGET:-unitree@10.42.0.76}
  REACTION_SSH_CONTROL=${REACTION_SSH_CONTROL:-$root/.runtime/ssh/g1-control}
  REACTION_SSH_BIN=${REACTION_SSH_BIN:-ssh}
  reaction_ssh_base=("$REACTION_SSH_BIN" -T -o BatchMode=yes -o ConnectTimeout=3 -S "$REACTION_SSH_CONTROL" -- "$REACTION_SSH_TARGET")
}

reaction_ssh_ensure() {
  reaction_ssh_init
  if [[ -S $REACTION_SSH_CONTROL ]]; then
    if "$REACTION_SSH_BIN" -o BatchMode=yes -o ConnectTimeout=3 -S "$REACTION_SSH_CONTROL" -O check -- "$REACTION_SSH_TARGET" >/dev/null 2>&1; then
      echo "REACTION_SSH_MASTER_REUSED=$REACTION_SSH_CONTROL"
      return 0
    fi
    echo "SSH_STALE_REACTION_CONTROL_SOCKET: $REACTION_SSH_CONTROL" >&2
    echo "Run scripts/bootstrap-reaction-ssh.sh explicitly; stale sockets are not removed by production startup." >&2
    return 41
  fi

  if ! "$REACTION_SSH_BIN" -T -o BatchMode=yes -o ConnectTimeout=3 -- "$REACTION_SSH_TARGET" true >/dev/null 2>&1; then
    echo "SSH_AUTH_BOOTSTRAP_REQUIRED" >&2
    echo "Run scripts/bootstrap-reaction-ssh.sh interactively." >&2
    return 42
  fi

  mkdir -p -- "$(dirname -- "$REACTION_SSH_CONTROL")"
  "$REACTION_SSH_BIN" -MNf -o BatchMode=yes -o ConnectTimeout=3 \
    -o ControlMaster=yes -o ControlPersist=10m -S "$REACTION_SSH_CONTROL" \
    -- "$REACTION_SSH_TARGET"
  if ! [[ -S $REACTION_SSH_CONTROL ]] || \
     ! "$REACTION_SSH_BIN" -o BatchMode=yes -o ConnectTimeout=3 \
       -S "$REACTION_SSH_CONTROL" -O check -- "$REACTION_SSH_TARGET" >/dev/null 2>&1; then
    echo "REACTION_SSH_MASTER_ESTABLISH_FAILED" >&2
    return 43
  fi
  echo "REACTION_SSH_MASTER_ESTABLISHED=$REACTION_SSH_CONTROL"
}
