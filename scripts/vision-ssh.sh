#!/usr/bin/env bash

vision_ssh_init() {
  local root
  root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
  VISION_SSH_TARGET=${VISION_SSH_TARGET:-unitree@10.42.0.76}
  VISION_SSH_CONTROL=${VISION_SSH_CONTROL:-$root/.runtime/ssh/g1-control}
  VISION_SSH_BIN=${VISION_SSH_BIN:-ssh}
  vision_ssh_base=("$VISION_SSH_BIN" -T -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=3 -S "$VISION_SSH_CONTROL" -- "$VISION_SSH_TARGET")
}

vision_ssh_ensure() {
  vision_ssh_init
  if [[ -S $VISION_SSH_CONTROL ]]; then
    if "$VISION_SSH_BIN" -o StrictHostKeyChecking=yes -o BatchMode=yes \
      -o ConnectTimeout=3 -S "$VISION_SSH_CONTROL" -O check -- \
      "$VISION_SSH_TARGET" >/dev/null 2>&1; then
      echo "VISION_SSH_MASTER_REUSED=$VISION_SSH_CONTROL"
      return 0
    fi
    echo "SSH_STALE_VISION_CONTROL_SOCKET: $VISION_SSH_CONTROL" >&2
    return 41
  fi
  if ! "$VISION_SSH_BIN" -T -o StrictHostKeyChecking=yes -o BatchMode=yes \
    -o ConnectTimeout=3 -- "$VISION_SSH_TARGET" true >/dev/null 2>&1; then
    echo SSH_AUTH_BOOTSTRAP_REQUIRED >&2
    return 42
  fi
  mkdir -p -- "$(dirname -- "$VISION_SSH_CONTROL")"
  "$VISION_SSH_BIN" -MNf -o StrictHostKeyChecking=yes -o BatchMode=yes \
    -o ConnectTimeout=3 -o ControlMaster=yes -o ControlPersist=10m \
    -S "$VISION_SSH_CONTROL" -- "$VISION_SSH_TARGET"
  [[ -S $VISION_SSH_CONTROL ]] && \
    "$VISION_SSH_BIN" -o StrictHostKeyChecking=yes -o BatchMode=yes \
      -o ConnectTimeout=3 -S "$VISION_SSH_CONTROL" -O check -- \
      "$VISION_SSH_TARGET" >/dev/null 2>&1 || {
        echo VISION_SSH_MASTER_ESTABLISH_FAILED >&2
        return 43
      }
  echo "VISION_SSH_MASTER_ESTABLISHED=$VISION_SSH_CONTROL"
}
