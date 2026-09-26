#!/usr/bin/env bash
set -euo pipefail

root=/home/ubuntu/dev/g1-bottle-reaction
target=${G1_SSH_TARGET:-unitree@10.42.0.76}
control=${G1_SSH_CONTROL:-$root/.runtime/usb-camera-ssh/wifi-control}
remote_root=/tmp/motiondecode-current
socket_path=/tmp/motiondecode-reaction.sock
python=/home/ubuntu/.venvs/g1-game-vision/bin/python

check_local_assets() {
  PYTHONPATH="$root/src" "$python" -B - <<'PY'
from pathlib import Path
from g1_bottle_reaction.game_vision.found_audio import validate_sound
root = Path('/home/ubuntu/dev/g1-bottle-reaction')
assets = {
    'person': root / 'assets/audio/reactions/person/detected.wav',
    'banana': root / 'assets/audio/reactions/banana/detected.wav',
    'plushie': root / 'assets/audio/reactions/plushie/plushie_affectionate.wav',
}
for name, path in assets.items():
    validate_sound(path)
    print(f'AUDIO_{name.upper()}_READY={path}')
model = root / '.runtime/models/yolo11n.pt'
if not model.is_file() or model.stat().st_size == 0:
    raise SystemExit('YOLO model is missing: ' + str(model))
print('YOLO_MODEL_READY=' + str(model))
PY
}

check_local_assets
if [[ ${1:-} == --dry-run ]]; then
  echo 'DRY_RUN_READY_CHECK: local assets PASS; SSH/camera/worker checks skipped'
  exit 0
fi

ip -4 addr show dev wlp128s20f3 | grep -F '10.42.0.1/24' >/dev/null
command -v nvidia-smi >/dev/null
timeout 2 nvidia-smi -L | grep -F 'GPU 0:' >/dev/null
[[ -S $control ]] || { echo "SSH ControlMaster socket is missing: $control" >&2; exit 2; }

remote=$(timeout 7 ssh -T -o BatchMode=yes -o ConnectTimeout=2 -S "$control" -- "$target" /bin/bash -s -- "$remote_root" "$socket_path" <<'REMOTE'
set -euo pipefail
root=$1
socket=$2
test -x /usr/bin/python3
test -d /tmp/motiondecode-hold-deps/mujoco
test -f "$root/scripts/resident_worker_client.py"
test -S "$socket"
test -s /tmp/motiondecode-expected-arm-pid
case "$(cat /tmp/motiondecode-expected-arm-pid)" in
  ''|*[!0-9]*) echo 'Verified Arm PID marker is invalid' >&2; exit 3;;
esac
command -v gst-launch-1.0 >/dev/null
pgrep -af '^/unitree/module/video_hub_pc4/videohub_pc4_chest ' >/dev/null
if pgrep -af '[g]1-wander-reactive-mvp.py' >/dev/null || test -e /tmp/g1-mapless-wander.pid; then
  echo 'Wander residual process or PID file exists' >&2
  exit 3
fi
cd "$root"
status=
i=0
while test "$i" -lt 20; do
  status=$(/usr/bin/python3 -B scripts/resident_worker_client.py --socket "$socket" --timeout 3)
  if STATUS="$status" /usr/bin/python3 -B - <<'PY'
import json, os
s = json.loads(os.environ['STATUS'])
raise SystemExit(0 if s.get('arm_action') == 'IDLE' else 1)
PY
  then
    printf '%s\n' "$status"
    exit 0
  fi
  sleep .2
  i=$((i + 1))
done
printf '%s\n' "$status"
exit 4
REMOTE
)

STATUS=$remote "$python" -B - <<'PY'
import json, os
lines = [line for line in os.environ['STATUS'].splitlines() if line.lstrip().startswith('{')]
if len(lines) != 1:
    raise SystemExit('expected exactly one worker status response')
s = json.loads(lines[0])
checks = {
    'accepted': s.get('accepted') is True,
    'state': s.get('state') == 'READY',
    'mode': s.get('mode') == 'real',
    'arm_action': s.get('arm_action') == 'IDLE',
    'weight': s.get('weight') == 0.0,
    'external_writers': s.get('external_writers') == 0,
    'ownership_safe': s.get('ownership_safe') is True,
    'fault': s.get('fault') is None,
    'preflight': s.get('preflight') == {'found': True, 'surprise': True},
}
failed = [key for key, passed in checks.items() if not passed]
if failed:
    raise SystemExit('DEMO_NOT_READY ' + ','.join(failed) + ' ' + json.dumps(s, sort_keys=True))
print('SSH_READY')
print('CAMERA_TRANSPORT_READY')
print('WANDER_RESIDUAL_NONE')
print('MOTIONDECODE_READY ' + json.dumps(s, sort_keys=True))
print('FINAL DEMO READY')
PY
