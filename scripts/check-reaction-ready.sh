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
source_ip=${MOTIONDECODE_ARM_SOURCE_IP:-192.168.123.161}

if [[ ${1:-} == --dry-run ]]; then
  echo "DRY_RUN_READY_CHECK: no SSH, process start, DDS writer, or robot command"
  echo "REACTION_SSH_CONTROL=$control"
  echo "RUNTIME=$runtime CODE=$code DEPS=$deps SOCKET=$socket"
  echo "CHECKS=manifests,python,mujoco,assets,LowState,stationary,ArmIdentity,ArmAction,resident,preflight,fault"
  exit 0
fi
[[ $# == 0 ]] || { echo "usage: $0 [--dry-run]" >&2; exit 2; }
reaction_ssh_ensure || exit $?
ssh_base=("${reaction_ssh_base[@]}")
report=$(mktemp /tmp/reaction-ready-arm.XXXXXX.json)
status_file=$(mktemp /tmp/reaction-ready-status.XXXXXX.json)
trap 'rm -f -- "$report" "$status_file"' EXIT

"${ssh_base[@]}" /bin/bash -s -- "$runtime" "$python" "$deps" <<'REMOTE'
set -euo pipefail
root=$1; python=$2; deps=$3; code=$root/code
test -d "$code" && test -d "$deps" && test -f "$root/BUNDLE_MANIFEST.sha256" || { echo RUNTIME_NOT_INSTALLED >&2; exit 10; }
(cd "$root" && sha256sum -c BUNDLE_MANIFEST.sha256 >/dev/null) && (cd "$code" && sha256sum -c MANIFEST.sha256 >/dev/null) && (cd "$deps" && sha256sum -c MANIFEST.sha256 >/dev/null) || { echo RUNTIME_HASH_MISMATCH >&2; exit 11; }
PYTHONPATH="$deps:$code/scripts:/home/unitree/unitree_sdk2_python" "$python" -B - "$deps" "$code" <<'PY'
import hashlib, json, mujoco, sys
from pathlib import Path
deps, code = map(lambda x: Path(x).resolve(), sys.argv[1:])
module = Path(mujoco.__file__).resolve()
if deps not in module.parents or mujoco.__version__ != '3.1.6':
    raise SystemExit('VALIDATED_MUJOCO_MISMATCH')
reactions = json.loads((code / 'config/named_reactions.json').read_text())['reactions']
for name in ('found', 'surprise'):
    item = reactions.get(name, {})
    asset = code / item.get('source_csv', '')
    if item.get('real_g1_validated') is not True or item.get('prevalidated_runtime') is not True or not asset.is_file() or hashlib.sha256(asset.read_bytes()).hexdigest() != item.get('source_sha256'):
        raise SystemExit(f'{name.upper()}_ASSET_HASH_MISMATCH')
    print(f'{name.upper()}_ASSET_HASH_PASS')
print('VALIDATED_PYTHON_PASS')
print('VALIDATED_MUJOCO_PASS')
PY
REMOTE

"${ssh_base[@]}" "cd '$code' && mkdir -p output/live && rm -f output/live/motiondecode-arm-ready.json && (PYTHONPATH='$deps:$code/scripts:/home/unitree/unitree_sdk2_python' '$python' -B scripts/inspect_arm_sdk_publishers.py --network-interface eth0 --seconds 15 --json output/live/motiondecode-arm-ready.json >/dev/null 2>&1 || test -s output/live/motiondecode-arm-ready.json) && cat output/live/motiondecode-arm-ready.json" >"$report"
arm_pid=$("$script_root/scripts/reaction-runtime-validate.py" arm "$report" --source-ip "$source_ip")
echo "ARM_IDENTITY_PASS pid=$arm_pid"
"${ssh_base[@]}" "test -S '$socket' && cd '$code' && '$python' -B scripts/resident_worker_client.py --socket '$socket' --timeout 3" >"$status_file"
"$script_root/scripts/reaction-runtime-validate.py" status "$status_file" >/dev/null
echo "RESIDENT_READY_PASS"
echo "REACTION READY"
