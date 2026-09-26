#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
config=$root/config/vision_runtime.yaml
runtime=$root/.runtime/vision
pid_file=$runtime/vision.pid
session_file=$runtime/vision.session
log=$runtime/vision.log
# shellcheck source=scripts/vision-ssh.sh
source "$root/scripts/vision-ssh.sh"
vision_ssh_init
readarray -t cfg < <("/home/ubuntu/.venvs/g1-game-vision/bin/python" -B - "$config" <<'PY'
import sys, yaml
d=yaml.safe_load(open(sys.argv[1]))
for key in ('network_interface','local_ip','g1_ip','python','model','model_sha256','camera_transport','camera_port','camera_fps','yolo_fps'):
    print(d[key])
PY
)
interface=${cfg[0]}; local_ip=${cfg[1]}; g1_ip=${cfg[2]}; python=${cfg[3]}; model=${cfg[4]}; model_sha=${cfg[5]}
transport=${cfg[6]}; port=${cfg[7]}; camera_fps=${cfg[8]}; yolo_fps=${cfg[9]}
if [[ ${1:-} == --dry-run ]]; then
  echo "DRY_RUN: SSH master NONE; videohub change NONE; camera start NONE; YOLO start NONE; download NONE; install NONE; G1 command NONE"
  echo "NETWORK_INTERFACE=$interface"
  echo "LOCAL_IP=$local_ip G1_IP=$g1_ip"
  echo "VISION_SSH_CONTROL=$VISION_SSH_CONTROL"
  echo "CAMERA_TRANSPORT=$transport PORT=$port FPS=$camera_fps"
  echo "MODEL=$model"
  echo "MODEL_SHA256=$model_sha"
  echo "PYTHON=$python YOLO_FPS=$yolo_fps"
  exit 0
fi
[[ $# == 0 ]] || { echo "usage: $0 [--dry-run]" >&2; exit 2; }
[[ $transport == ssh-rtp ]] || { echo CAMERA_TRANSPORT_NOT_READY >&2; exit 3; }
"$python" -B "$root/scripts/vision-runtime-validate.py" model --config "$config" >/dev/null
test -x "$python" || { echo VISION_PYTHON_NOT_READY >&2; exit 3; }
route=$(ip -j route get "$g1_ip")
ROUTE=$route "$python" -B - "$interface" "$local_ip" <<'PY'
import json, os, sys
r=json.loads(os.environ['ROUTE'])
if not r or r[0].get('dev') != sys.argv[1] or r[0].get('prefsrc') != sys.argv[2] or r[0].get('gateway'):
    raise SystemExit('VISION_NETWORK_ROUTE_MISMATCH')
PY
vision_ssh_ensure || exit $?
"${vision_ssh_base[@]}" "pgrep -af '^/unitree/module/video_hub_pc4/videohub_pc4_chest ' >/dev/null" || { echo VIDEOHUB_NOT_READY >&2; exit 4; }

if [[ -e $pid_file ]]; then
  if "$root/scripts/check-vision-ready.sh" >/dev/null 2>&1; then
    echo HEALTHY_VISION_PROCESS_REUSED
    "$root/scripts/check-vision-ready.sh"
    exit 0
  fi
  echo VISION_PID_OR_PROCESS_STALE_FAIL_CLOSED >&2
  exit 5
fi
if pgrep -af "[g]1_dual_camera.py|[g]1_camera_pipe.py.*rtp" >/dev/null; then
  echo CAMERA_PROCESS_CONFLICT >&2
  exit 5
fi
if "${vision_ssh_base[@]}" "pgrep -af 'python3 -u -B -c.*[G]etImageSample' >/dev/null"; then
  echo CAMERA_PROCESS_CONFLICT >&2
  exit 5
fi

mkdir -p -- "$runtime"
command=("$python" -B "$root/tools/g1_dual_camera.py"
  --usb-bind "$local_ip" --usb-host "$g1_ip" --network-interface "$interface"
  --ssh-target "$VISION_SSH_TARGET" --ssh-control "$VISION_SSH_CONTROL"
  --g1-camera-transport "$transport" --g1-camera-port "$port"
  --g1-camera-fps "$camera_fps" --no-usb-camera --headless
  --yolo --yolo-model "$model" --yolo-fps "$yolo_fps")
cd "$root"
setsid nohup env YOLO_AUTOINSTALL=false "${command[@]}" >"$log" 2>&1 </dev/null &
pid=$!
printf '%s\n' "$pid" >"$pid_file"
printf 'pid=%s\nstarted_at=%s\ntransport=%s\nmodel_sha256=%s\n' \
  "$pid" "$(date -Iseconds)" "$transport" "$model_sha" >"$session_file"

for unused in $(seq 1 60); do
  if ! kill -0 "$pid" 2>/dev/null; then
    tail -n 80 "$log" >&2 || true
    exit 6
  fi
  if "$python" -B "$root/scripts/vision-runtime-validate.py" health --config "$config" --log "$log" >/dev/null 2>&1; then
    "$root/scripts/check-vision-ready.sh"
    exit 0
  fi
  sleep 2
done
tail -n 80 "$log" >&2 || true
echo VISION_START_TIMEOUT >&2
exit 7
