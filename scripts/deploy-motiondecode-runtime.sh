#!/usr/bin/env bash
set -euo pipefail

script_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/reaction-ssh.sh
source "$script_root/scripts/reaction-ssh.sh"
reaction_ssh_init
bundle=${MOTIONDECODE_KNOWN_GOOD_BUNDLE:-/home/ubuntu/dev/g1-bottle-reaction/.runtime/motiondecode-known-good}
target=$REACTION_SSH_TARGET
control=$REACTION_SSH_CONTROL
production=${MOTIONDECODE_PRODUCTION_ROOT:-/home/unitree/g1-runtime/motiondecode}
fail() { echo "DEPLOY_REFUSED: $*" >&2; exit 2; }
test -d "$bundle/code" || fail "known-good code missing: $bundle/code"
test -d "$bundle/deps" || fail "known-good deps missing: $bundle/deps"
test -f "$bundle/BUNDLE_MANIFEST.sha256" || fail "bundle manifest missing"
(cd "$bundle" && sha256sum -c BUNDLE_MANIFEST.sha256 >/dev/null && cd code && sha256sum -c MANIFEST.sha256 >/dev/null && cd ../deps && sha256sum -c MANIFEST.sha256 >/dev/null) || fail "known-good bundle hash verification failed"
if [[ ${1:-} == --dry-run ]]; then
  echo "KNOWN_GOOD_BUNDLE_VERIFIED=$bundle"
  echo "PRODUCTION_ROOT=$production"
  echo "TARGET=$target"
  echo "DRY_RUN: no SSH, rsync, install, worker, DDS writer, or robot command"
  exit 0
fi
[[ $# == 0 ]] || fail "usage: $0 [--dry-run]"
reaction_ssh_ensure || exit $?
ssh_base=("${reaction_ssh_base[@]}")
state=$("${ssh_base[@]}" /bin/bash -s -- "$production" <<'REMOTE'
set -euo pipefail
root=$1
if test -S /tmp/motiondecode-reaction.sock || pgrep -af 'resident_worker[.]py' >/dev/null; then echo WORKER_OR_SOCKET_PRESENT; exit 3; fi
if test -f "$root/BUNDLE_MANIFEST.sha256" && test -f "$root/code/MANIFEST.sha256" && test -f "$root/deps/MANIFEST.sha256" && (cd "$root" && sha256sum -c BUNDLE_MANIFEST.sha256 >/dev/null 2>&1) && (cd "$root/code" && sha256sum -c MANIFEST.sha256 >/dev/null 2>&1) && (cd "$root/deps" && sha256sum -c MANIFEST.sha256 >/dev/null 2>&1); then echo ALREADY_READY; else echo DEPLOY_REQUIRED; fi
REMOTE
) || fail "remote precondition check failed"
[[ $state != WORKER_OR_SOCKET_PRESENT ]] || fail "resident worker or socket present"
if [[ $state == ALREADY_READY ]]; then echo ALREADY_READY; exit 0; fi
[[ $state == DEPLOY_REQUIRED ]] || fail "unexpected remote state: $state"
parent=$(dirname "$production")
stage=$("${ssh_base[@]}" "mkdir -p '$parent' && mktemp -d '$parent/.motiondecode.stage.XXXXXX'")
case "$stage" in "$parent"/.motiondecode.stage.*) ;; *) fail "invalid stage: $stage";; esac
rsync_ssh="$REACTION_SSH_BIN -o BatchMode=yes -o ConnectTimeout=3 -S $control"
rsync -a -e "$rsync_ssh" -- "$bundle/code/" "$target:$stage/code/"
rsync -a -e "$rsync_ssh" -- "$bundle/deps/" "$target:$stage/deps/"
rsync -a -e "$rsync_ssh" -- "$bundle/BUNDLE_MANIFEST.sha256" "$bundle/BUNDLE_INFO.txt" "$target:$stage/"
"${ssh_base[@]}" /bin/bash -s -- "$stage" "$production" <<'REMOTE'
set -euo pipefail
stage=$1; production=$2; parent=$(dirname "$production")
case "$stage" in "$parent"/.motiondecode.stage.*) ;; *) exit 20;; esac
(cd "$stage" && sha256sum -c BUNDLE_MANIFEST.sha256 >/dev/null)
(cd "$stage/code" && sha256sum -c MANIFEST.sha256 >/dev/null)
(cd "$stage/deps" && sha256sum -c MANIFEST.sha256 >/dev/null)
old="$parent/.motiondecode.previous.$(date +%Y%m%d%H%M%S)"
if test -e "$production"; then mv -- "$production" "$old"; fi
if ! mv -- "$stage" "$production"; then test ! -e "$production" && test -e "$old" && mv -- "$old" "$production"; exit 21; fi
(cd "$production" && sha256sum -c BUNDLE_MANIFEST.sha256 >/dev/null)
(cd "$production/code" && sha256sum -c MANIFEST.sha256 >/dev/null)
(cd "$production/deps" && sha256sum -c MANIFEST.sha256 >/dev/null)
echo DEPLOY_COMPLETE
echo 'WORKER_STARTED=NO DDS_WRITER_CREATED=NO ROBOT_COMMAND_SENT=NO'
REMOTE
