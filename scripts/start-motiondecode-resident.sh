#!/usr/bin/env bash
set -euo pipefail

# Start only the already-provisioned, historically validated PC2 runtime.
# This script deliberately never installs, rsyncs, or searches for Python.
target=${G1_SSH_TARGET:-unitree@10.42.0.76}
control=${G1_SSH_CONTROL:-/home/ubuntu/dev/g1-bottle-reaction/.runtime/usb-camera-ssh/wifi-control}
expected_arm_pid=${MOTIONDECODE_EXPECTED_ARM_PID:-2807}
remote_root=/tmp/motiondecode-current
remote_python=/usr/bin/python3
dependency_root=/tmp/motiondecode-hold-deps
socket_path=/tmp/motiondecode-reaction.sock
log_path=/tmp/motiondecode-current/output/live/resident-production.log

if [[ ${1:-} == --dry-run ]]; then
  printf '%s\n' "DRY_RUN: no SSH, DDS writer, or robot command"
  printf '%s\n' "TARGET=$target"
  printf '%s\n' "PYTHON=$remote_python"
  printf '%s\n' "PYTHONPATH=$dependency_root:$remote_root/scripts:/home/unitree/unitree_sdk2_python"
  printf '%s\n' "EXPECTED_ARM_PID=$expected_arm_pid"
  printf '%s\n' "REACTIONS=found,surprise"
  exit 0
fi

[[ $expected_arm_pid =~ ^[1-9][0-9]*$ ]] || {
  echo "MOTIONDECODE_EXPECTED_ARM_PID must be a positive integer" >&2
  exit 2
}
[[ -S $control ]] || {
  echo "SSH ControlMaster socket is missing: $control" >&2
  exit 2
}

ssh_base=(ssh -T -o BatchMode=yes -o ConnectTimeout=3 -S "$control" -- "$target")
"${ssh_base[@]}" /bin/bash -s -- \
  "$remote_root" "$remote_python" "$dependency_root" "$socket_path" \
  "$log_path" "$expected_arm_pid" <<'REMOTE'
set -euo pipefail
root=$1
python=$2
deps=$3
socket=$4
log=$5
expected_pid=$6
export PYTHONPATH="$deps:$root/scripts:/home/unitree/unitree_sdk2_python"
export LD_LIBRARY_PATH="/home/unitree/work/unitree_sdk2/thirdparty/lib/aarch64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

require_file() { test -f "$1" || { echo "REQUIRED FILE MISSING: $1" >&2; exit 10; }; }
require_dir() { test -d "$1" || { echo "REQUIRED DIRECTORY MISSING: $1" >&2; exit 10; }; }
test -x "$python" || { echo "REQUIRED PYTHON MISSING/NOT EXECUTABLE: $python" >&2; exit 10; }
require_dir "$deps/mujoco"
require_dir /home/unitree/work/unitree_sdk2/thirdparty/lib/aarch64
require_file "$root/scripts/resident_worker.py"
require_file "$root/scripts/resident_worker_client.py"
require_file "$root/scripts/inspect_arm_sdk_publishers.py"
require_file "$root/config/named_reactions.json"
"$python" -B - <<'PY'
import mujoco
from pathlib import Path
path = Path(mujoco.__file__).resolve()
expected = Path('/tmp/motiondecode-hold-deps').resolve()
if expected not in path.parents:
    raise SystemExit('MuJoCo loaded from unexpected path: ' + str(path))
if mujoco.__version__ != '3.1.6':
    raise SystemExit('Expected validated MuJoCo 3.1.6, got ' + mujoco.__version__)
print('MOTIONDECODE_PYTHON_READY executable=/usr/bin/python3 '
      f'mujoco={mujoco.__version__} path={path}')
PY

"$python" -B - "$root" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
data = json.loads((root / 'config/named_reactions.json').read_text())['reactions']
for name in ('found', 'surprise'):
    item = data.get(name)
    if not isinstance(item, dict):
        raise SystemExit(f'REACTION CONFIG MISSING: {name}')
    if item.get('real_g1_validated') is not True or item.get('prevalidated_runtime') is not True:
        raise SystemExit(f'REACTION NOT REAL/PREVALIDATED: {name}')
    asset = root / item['source_csv']
    if not asset.is_file():
        raise SystemExit(f'REACTION ASSET MISSING: {name}: {asset}')
    actual = hashlib.sha256(asset.read_bytes()).hexdigest()
    if actual != item.get('source_sha256'):
        raise SystemExit(f'REACTION ASSET HASH MISMATCH: {name}: {asset}')
    print(f'REACTION_ASSET_READY {name} {asset}')
PY

# PID is a G1-internal DDS participant property, not a stable PC2 process ID.
# Observe it read-only and accept only one exact Unitree Arm service signature.
ownership_report="$root/output/live/arm-ownership-startup.json"
mkdir -p "$(dirname "$ownership_report")"
rm -f "$ownership_report"
set +e
"$python" -B "$root/scripts/inspect_arm_sdk_publishers.py" \
  --network-interface eth0 --seconds 15 --json "$ownership_report" >/dev/null 2>&1
probe_rc=$?
set -e
test -f "$ownership_report" || {
  echo "ARM PID READ-ONLY PROBE FAILED (exit $probe_rc; no report)" >&2
  exit 11
}
"$python" -B - "$ownership_report" "$expected_pid" <<'PY'
import json, sys
report_path, asserted_pid = sys.argv[1:]
data = json.load(open(report_path))
publishers = data.get('topics', {}).get('rt/arm_sdk', {}).get('publishers', [])
required_publications = {'rt/api/arm/response', 'rt/api/sport/request',
                         'rt/arm/action/state', 'rt/arm_sdk'}
required_subscriptions = {'rt/api/arm/request', 'rt/api/sport/response',
                          'rt/armsdk', 'rt/lf/bmsstate', 'rt/lowstate',
                          'rt/sportmodestate'}
candidates = [
    item for item in publishers
    if item.get('hostname') == 'Unitree'
    and item.get('process_name') == 'python3'
    and '192.168.123.161' in item.get('source_ips', [])
    and required_publications <= set(item.get('related_publications', []))
    and required_subscriptions <= set(item.get('related_subscriptions', []))
]
if len(candidates) != 1:
    raise SystemExit(f'ARM PID AMBIGUOUS: expected exactly one strict candidate, got {len(candidates)}')
observed_pid = str(candidates[0].get('pid'))
if observed_pid != asserted_pid:
    raise SystemExit(f'ARM PID MISMATCH: configured={asserted_pid} observed={observed_pid}')
if data.get('lowstate', {}).get('status') != 'PASS':
    raise SystemExit('ARM PID PROBE BLOCKED: LowState is not PASS')
if data.get('lowstate', {}).get('stationary') != 'PASS':
    raise SystemExit('ARM PID PROBE BLOCKED: G1 is not stationary')
action = data.get('topics', {}).get('rt/arm/action/state', {}).get('status_during_observation')
if action != 'IDLE':
    raise SystemExit(f'ARM PID PROBE BLOCKED: Arm Action is {action}')
print(f'ARM_PID_VERIFIED={observed_pid}')
PY
printf '%s\n' "$expected_pid" > /tmp/motiondecode-expected-arm-pid

status_json() {
  cd "$root"
  "$python" -B scripts/resident_worker_client.py --socket "$socket" --timeout 3
}

if test -S "$socket"; then
  existing=
  i=0
  while test "$i" -lt 20; do
    existing=$(status_json)
    if STATUS="$existing" "$python" -B - <<'PY'
import json, os
s = json.loads(os.environ['STATUS'])
ok = (
    s.get('accepted') is True and s.get('state') == 'READY'
    and s.get('mode') == 'real' and s.get('arm_action') == 'IDLE'
    and s.get('weight') == 0.0 and s.get('external_writers') == 0
    and s.get('ownership_safe') is True and s.get('fault') is None
    and s.get('preflight') == {'found': True, 'surprise': True}
)
raise SystemExit(0 if ok else 1)
PY
    then
      printf '%s\n' "$existing"
      exit 0
    fi
    sleep .1
    i=$((i + 1))
  done

  # A FAULT/non-ready worker is never reused. Its own stop operation performs
  # the existing zero-weight release before the process exits.
  printf '{"operation":"stop"}\n' | \
    "$python" -B "$root/scripts/resident_worker_client.py" \
      --stdio --socket "$socket" --timeout 5 >/dev/null
  i=0
  while test "$i" -lt 50 && test -S "$socket"; do
    sleep .1
    i=$((i + 1))
  done
  test ! -S "$socket" || {
    echo 'non-ready resident worker did not stop cleanly' >&2
    exit 3
  }
fi
if pgrep -af '^(python3|/usr/bin/python3) -u scripts/resident_worker.py' >/dev/null; then
  echo 'resident_worker is running without its socket; refusing a duplicate' >&2
  exit 3
fi

mkdir -p "$(dirname "$log")"
cd "$root"
nohup env \
  PYTHONPATH="$PYTHONPATH" \
  LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
  "$python" -u scripts/resident_worker.py \
    --real --confirm-site-ready \
    --network-interface eth0 \
    --expected-arm-pid "$expected_pid" \
    --lowstate-backend cyclonedds \
    --reaction found --reaction surprise \
    --socket "$socket" \
    > "$log" 2>&1 < /dev/null &
pid=$!
printf '%s\n' "$pid" > /tmp/motiondecode-resident.pid

i=0
while test "$i" -lt 360; do
  if ! kill -0 "$pid" 2>/dev/null; then
    tail -n 60 "$log" >&2 || true
    exit 4
  fi
  if test -f "$log" && grep -Eq '"state"[[:space:]]*:[[:space:]]*"FAULT"|(^|[^[:alpha:]])FAULT([^[:alpha:]]|$)' "$log"; then
    tail -n 60 "$log" >&2 || true
    exit 4
  fi
  if test -S "$socket"; then
    status=
    if status=$(status_json 2>/dev/null) && STATUS="$status" "$python" -B - <<'PY'
import json, os
s = json.loads(os.environ['STATUS'])
required = (
    s.get('accepted') is True,
    s.get('state') == 'READY',
    s.get('mode') == 'real',
    s.get('arm_action') == 'IDLE',
    s.get('weight') == 0.0,
    s.get('external_writers') == 0,
    s.get('ownership_safe') is True,
    s.get('fault') is None,
    s.get('preflight') == {'found': True, 'surprise': True},
)
raise SystemExit(0 if all(required) else 1)
PY
    then
      printf '%s\n' "$status"
      exit 0
    fi
  fi
  sleep .25
  i=$((i + 1))
done
tail -n 60 "$log" >&2 || true
exit 5
REMOTE

status=
for unused in $(seq 1 20); do
  status=$("${ssh_base[@]}" "cd '$remote_root' && '$remote_python' -B scripts/resident_worker_client.py --socket '$socket_path' --timeout 3")
  if STATUS=$status /home/ubuntu/.venvs/g1-game-vision/bin/python -B - <<'PY'
import json, os
s = json.loads(os.environ['STATUS'])
required = (
    s.get('accepted') is True,
    s.get('state') == 'READY',
    s.get('mode') == 'real',
    s.get('arm_action') == 'IDLE',
    s.get('weight') == 0.0,
    s.get('external_writers') == 0,
    s.get('ownership_safe') is True,
    s.get('fault') is None,
    s.get('preflight') == {'found': True, 'surprise': True},
)
if not all(required):
    raise SystemExit(1)
PY
  then
    printf '%s\n' "MOTIONDECODE_RESIDENT_READY $status"
    exit 0
  fi
  sleep .25
done
echo "resident worker is not production READY: $status" >&2
exit 6
