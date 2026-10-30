#!/usr/bin/env bash
set -euo pipefail

script_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/reaction-ssh.sh
source "$script_root/scripts/reaction-ssh.sh"
reaction_ssh_init
target=$REACTION_SSH_TARGET
control=$REACTION_SSH_CONTROL
runtime=${MOTIONDECODE_PRODUCTION_ROOT:-/home/unitree/g1-runtime/motiondecode}
code=$runtime/code
deps=$runtime/deps
python=/usr/bin/python3
socket=/tmp/motiondecode-reaction.sock
pid_file=/tmp/motiondecode-resident.pid
arm_pid_file=/tmp/motiondecode-expected-arm-pid
session_file=/tmp/motiondecode-reaction.session
log=/tmp/motiondecode-resident-production.log
source_ip=${MOTIONDECODE_ARM_SOURCE_IP:-192.168.123.161}
reactions=${MOTIONDECODE_REACTIONS:-found,surprise}

if [[ ${1:-} == --dry-run ]]; then
  echo "DRY_RUN: robot command NONE; reaction NONE; audio NONE; DDS writer NONE; resident start NONE; rsync NONE; install NONE"
  echo "TARGET=$target"
  echo "REACTION_SSH_CONTROL=$control"
  echo "RUNTIME=$runtime"
  echo "CODE=$code"
  echo "DEPS=$deps"
  echo "PYTHON=$python"
  echo "SOCKET=$socket PID=$pid_file SESSION=$session_file"
  echo "ARM_IDENTITY=strict dynamic detection source_ip=$source_ip"
  exit 0
fi
[[ $# == 0 ]] || { echo "usage: $0 [--dry-run]" >&2; exit 2; }
reaction_ssh_ensure || exit $?
ssh_base=("${reaction_ssh_base[@]}")
report=$(mktemp /tmp/reaction-arm-report.XXXXXX.json)
status_file=$(mktemp /tmp/reaction-status.XXXXXX.json)
trap 'rm -f -- "$report" "$status_file"' EXIT

"${ssh_base[@]}" /bin/bash -s -- "$runtime" "$python" "$deps" <<'REMOTE'
set -euo pipefail
root=$1; python=$2; deps=$3
test -d "$root/code" && test -d "$root/deps" && test -f "$root/BUNDLE_MANIFEST.sha256" || { echo RUNTIME_NOT_INSTALLED >&2; exit 10; }
(cd "$root" && sha256sum -c BUNDLE_MANIFEST.sha256 >/dev/null) && (cd "$root/code" && sha256sum -c MANIFEST.sha256 >/dev/null) && (cd "$root/deps" && sha256sum -c MANIFEST.sha256 >/dev/null) || { echo RUNTIME_HASH_MISMATCH >&2; exit 11; }
test -x "$python" || { echo VALIDATED_PYTHON_MISSING >&2; exit 12; }
PYTHONPATH="$deps:$root/code/scripts:/home/unitree/unitree_sdk2_python" "$python" -B - "$deps" <<'PY'
import mujoco, sys
from pathlib import Path
deps = Path(sys.argv[1]).resolve(); path = Path(mujoco.__file__).resolve()
if deps not in path.parents or mujoco.__version__ != '3.1.6':
    raise SystemExit(f'VALIDATED_MUJOCO_MISMATCH version={mujoco.__version__} path={path}')
PY
REMOTE

# Receive-only inspection: this creates readers but never an Arm command writer.
"${ssh_base[@]}" "cd '$code' && mkdir -p output/live && rm -f output/live/motiondecode-arm-identity.json && (PYTHONPATH='$deps:$code/scripts:/home/unitree/unitree_sdk2_python' '$python' -B scripts/inspect_arm_sdk_publishers.py --network-interface eth0 --seconds 15 --json output/live/motiondecode-arm-identity.json >/dev/null 2>&1 || test -s output/live/motiondecode-arm-identity.json) && cat output/live/motiondecode-arm-identity.json" >"$report"
arm_pid=$("$script_root/scripts/reaction-runtime-validate.py" arm "$report" --source-ip "$source_ip")
"${ssh_base[@]}" "printf '%s\n' '$arm_pid' > '$arm_pid_file'"

set +e
"${ssh_base[@]}" /bin/bash -s -- "$code" "$deps" "$python" "$socket" "$pid_file" "$session_file" "$log" "$arm_pid" "$reactions" <<'REMOTE'
set -euo pipefail
code=$1; deps=$2; python=$3; socket=$4; pid_file=$5; session=$6; log=$7; arm_pid=$8
reaction_args=(); for r in ${9//,/ }; do reaction_args+=(--reaction "$r"); done
export PYTHONPATH="$deps:$code/scripts:/home/unitree/unitree_sdk2_python"
export LD_LIBRARY_PATH="/home/unitree/work/unitree_sdk2/thirdparty/lib/aarch64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
status() { cd "$code" && "$python" -B scripts/resident_worker_client.py --socket "$socket" --timeout 3; }
if test -S "$socket"; then
  if current=$(status 2>/dev/null); then printf '%s\n' "$current"; exit 20; fi
  echo SOCKET_NON_READY_FAIL_CLOSED >&2
  exit 21
fi
if pgrep -af 'resident_worker[.]py' >/dev/null; then echo PROCESS_WITHOUT_SOCKET_FAIL_CLOSED >&2; exit 22; fi
# A stale PID marker alone is not worker evidence.
rm -f -- "$pid_file" "$session"
cd "$code"
nohup env PYTHONPATH="$PYTHONPATH" LD_LIBRARY_PATH="$LD_LIBRARY_PATH" "$python" -u scripts/resident_worker.py --real --confirm-site-ready --network-interface eth0 --expected-arm-pid "$arm_pid" --lowstate-backend cyclonedds "${reaction_args[@]}" --socket "$socket" >"$log" 2>&1 </dev/null &
pid=$!
printf '%s\n' "$pid" >"$pid_file"
printf 'arm_pid=%s\nstarted_at=%s\n' "$arm_pid" "$(date -Iseconds)" >"$session"
for unused in $(seq 1 360); do
  kill -0 "$pid" 2>/dev/null || { tail -n 60 "$log" >&2 || true; exit 23; }
  if test -S "$socket" && status 2>/dev/null; then exit 0; fi
  sleep .25
done
exit 24
REMOTE
remote_rc=$?
set -e
if [[ $remote_rc == 20 ]]; then
  "${ssh_base[@]}" "cd '$code' && '$python' -B scripts/resident_worker_client.py --socket '$socket' --timeout 3" >"$status_file"
  if "$script_root/scripts/reaction-runtime-validate.py" status "$status_file" >/dev/null; then
    echo HEALTHY_RESIDENT_REUSED
    cat "$status_file"
    exit 0
  fi
  # Only a responsive protocol peer is asked to stop. Its stop operation owns
  # the established release/weight-zero safety sequence.
  printf '{"operation":"stop"}\n' | "${ssh_base[@]}" "cd '$code' && '$python' -B scripts/resident_worker_client.py --stdio --socket '$socket' --timeout 5" >/dev/null
  "${ssh_base[@]}" /bin/bash -s -- "$socket" <<'REMOTE'
set -euo pipefail
socket=$1
for unused in $(seq 1 50); do test ! -S "$socket" && exit 0; sleep .1; done
echo NON_READY_RESIDENT_DID_NOT_STOP >&2
exit 25
REMOTE
  exec "$0"
fi
if [[ $remote_rc == 0 ]]; then
  "${ssh_base[@]}" "cd '$code' && '$python' -B scripts/resident_worker_client.py --socket '$socket' --timeout 3" >"$status_file"
  "$script_root/scripts/reaction-runtime-validate.py" status "$status_file" >/dev/null
  echo MOTIONDECODE_RESIDENT_READY
  cat "$status_file"
  exit 0
fi
exit "$remote_rc"
