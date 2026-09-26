#!/usr/bin/env bash
set -euo pipefail

# Offline recovery only: copy the prebuilt bundle to the two fixed PC2 /tmp
# paths and verify its manifests. Never install, resolve, search, or start.
root=/home/ubuntu/dev/g1-bottle-reaction
bundle=$root/.runtime/motiondecode-known-good
target=${G1_SSH_TARGET:-unitree@10.42.0.76}
control=${G1_SSH_CONTROL:-$root/.runtime/usb-camera-ssh/wifi-control}
remote_code=/tmp/motiondecode-current
remote_deps=/tmp/motiondecode-hold-deps

fail() { echo "KNOWN-GOOD RESTORE REFUSED: $*" >&2; exit 2; }

test -d "$bundle/code" || fail "bundle code is missing: $bundle/code"
test -d "$bundle/deps" || fail "bundle dependencies are missing: $bundle/deps"
test -f "$bundle/BUNDLE_MANIFEST.sha256" || fail "bundle manifest is missing"
(
  cd "$bundle"
  sha256sum -c BUNDLE_MANIFEST.sha256 >/dev/null
  cd code
  sha256sum -c MANIFEST.sha256 >/dev/null
  cd ../deps
  sha256sum -c MANIFEST.sha256 >/dev/null
) || fail "local bundle hash verification failed"

if [[ ${1:-} == --dry-run ]]; then
  echo "KNOWN_GOOD_BUNDLE_VERIFIED=$bundle"
  echo "RESTORE_TARGET_CODE=$remote_code"
  echo "RESTORE_TARGET_DEPS=$remote_deps"
  echo "DRY_RUN: no SSH, rsync, process, DDS writer, or robot command"
  exit 0
fi

test -S "$control" || fail "SSH ControlMaster socket is missing: $control"
ssh_base=(ssh -T -o BatchMode=yes -o ConnectTimeout=3 -S "$control" -- "$target")

state=$("${ssh_base[@]}" /bin/bash -s -- "$remote_code" "$remote_deps" <<'REMOTE'
set -euo pipefail
code=$1
deps=$2
if test -S /tmp/motiondecode-reaction.sock || \
   pgrep -af '^(python3|/usr/bin/python3) -u scripts/resident_worker.py' >/dev/null; then
  echo WORKER_OR_SOCKET_PRESENT
  exit 3
fi
code_ok=0
deps_ok=0
if test -f "$code/MANIFEST.sha256" && (cd "$code" && sha256sum -c MANIFEST.sha256 >/dev/null 2>&1); then
  code_ok=1
fi
if test -f "$deps/MANIFEST.sha256" && (cd "$deps" && sha256sum -c MANIFEST.sha256 >/dev/null 2>&1); then
  deps_ok=1
fi
printf 'CODE_OK=%s DEPS_OK=%s\n' "$code_ok" "$deps_ok"
REMOTE
) || fail "remote precondition check failed"

case "$state" in
  'CODE_OK=1 DEPS_OK=1') fail "both fixed /tmp trees already match known-good";;
  'CODE_OK='[01]' DEPS_OK='[01]) ;;
  *) fail "unexpected remote state: $state";;
esac

stage=$("${ssh_base[@]}" "mktemp -d /tmp/motiondecode-known-good.restore.XXXXXX")
case "$stage" in
  /tmp/motiondecode-known-good.restore.*) ;;
  *) fail "invalid remote staging path: $stage";;
esac

rsync_ssh="ssh -o BatchMode=yes -o ConnectTimeout=3 -S $control"
rsync -a -e "$rsync_ssh" -- "$bundle/code/" "$target:$stage/code/"
rsync -a -e "$rsync_ssh" -- "$bundle/deps/" "$target:$stage/deps/"

"${ssh_base[@]}" /bin/bash -s -- "$stage" "$remote_code" "$remote_deps" <<'REMOTE'
set -euo pipefail
stage=$1
code=$2
deps=$3
case "$stage" in /tmp/motiondecode-known-good.restore.*) ;; *) exit 20;; esac
test -f "$stage/code/MANIFEST.sha256"
test -f "$stage/deps/MANIFEST.sha256"
(cd "$stage/code" && sha256sum -c MANIFEST.sha256 >/dev/null)
(cd "$stage/deps" && sha256sum -c MANIFEST.sha256 >/dev/null)

suffix=$(basename "$stage")
if test -e "$code"; then mv -- "$code" "$code.pre-restore.$suffix"; fi
if test -e "$deps"; then mv -- "$deps" "$deps.pre-restore.$suffix"; fi
mv -- "$stage/code" "$code"
mv -- "$stage/deps" "$deps"
rmdir -- "$stage"

(cd "$code" && sha256sum -c MANIFEST.sha256 >/dev/null)
(cd "$deps" && sha256sum -c MANIFEST.sha256 >/dev/null)
echo 'KNOWN_GOOD_RESTORE_COMPLETE'
echo 'WORKER_STARTED=NO DDS_WRITER_CREATED=NO ROBOT_COMMAND_SENT=NO'
REMOTE
