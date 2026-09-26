#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
config=$root/config/vision_runtime.yaml
runtime=$root/.runtime/vision
pid_file=$runtime/vision.pid
log=$runtime/vision.log
# shellcheck source=scripts/vision-ssh.sh
source "$root/scripts/vision-ssh.sh"
vision_ssh_init
readarray -t cfg < <("/home/ubuntu/.venvs/g1-game-vision/bin/python" -B - "$config" <<'PY'
import sys, yaml
d=yaml.safe_load(open(sys.argv[1]))
for key in ('network_interface','local_ip','g1_ip','python','model','camera_transport'):
    print(d[key])
PY
)
interface=${cfg[0]}; local_ip=${cfg[1]}; g1_ip=${cfg[2]}; python=${cfg[3]}; model=${cfg[4]}; transport=${cfg[5]}
if [[ ${1:-} == --dry-run ]]; then
  echo "VISION_READY_DRY_RUN: SSH NONE; camera NONE; YOLO NONE; G1 command NONE"
  echo "NETWORK_INTERFACE=$interface G1_IP=$g1_ip SSH=$VISION_SSH_CONTROL"
  echo "TRANSPORT=$transport MODEL=$model PYTHON=$python"
  exit 0
fi
[[ $# == 0 ]] || { echo "usage: $0 [--dry-run]" >&2; exit 2; }
[[ $transport == ssh-rtp ]] || { echo CAMERA_TRANSPORT_NOT_READY >&2; exit 3; }
"$python" -B "$root/scripts/vision-runtime-validate.py" model --config "$config" >/dev/null
route=$(ip -j route get "$g1_ip")
ROUTE=$route "$python" -B - "$interface" "$local_ip" <<'PY'
import json, os, sys
route=json.loads(os.environ['ROUTE'])
if not route or route[0].get('dev') != sys.argv[1] or route[0].get('prefsrc') != sys.argv[2] or route[0].get('gateway'):
    raise SystemExit('VISION_NETWORK_ROUTE_MISMATCH')
PY
vision_ssh_ensure || exit $?
"${vision_ssh_base[@]}" "pgrep -af '^/unitree/module/video_hub_pc4/videohub_pc4_chest ' >/dev/null" || { echo VIDEOHUB_NOT_READY >&2; exit 4; }
[[ -s $pid_file ]] || { echo VISION_PROCESS_NOT_RUNNING >&2; exit 5; }
pid=$(<"$pid_file")
[[ $pid =~ ^[1-9][0-9]*$ ]] && kill -0 "$pid" 2>/dev/null || { echo VISION_PROCESS_NOT_RUNNING >&2; exit 5; }
[[ $(readlink -f "/proc/$pid/cwd") == "$root" ]] || { echo VISION_PROCESS_IDENTITY_MISMATCH >&2; exit 5; }
cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline")
[[ $cmd == *"$root/tools/g1_dual_camera.py"* && $cmd == *"--g1-camera-transport ssh-rtp"* ]] || { echo VISION_PROCESS_IDENTITY_MISMATCH >&2; exit 5; }
[[ $cmd != *"--found-audio"* && $cmd != *"--with-wander"* && $cmd != *"--enable-real-robot"* ]] || { echo VISION_SCOPE_VIOLATION >&2; exit 5; }
command -v nvidia-smi >/dev/null && timeout 3 nvidia-smi -L | grep -F 'GPU 0:' >/dev/null || { echo VISION_GPU_NOT_READY >&2; exit 6; }
"$python" -B - <<'PY'
import torch
if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
    raise SystemExit('VISION_CUDA_NOT_READY')
print('CUDA=' + torch.cuda.get_device_name(0))
PY
health=$("$python" -B "$root/scripts/vision-runtime-validate.py" health --config "$config" --log "$log")
echo "VISION RUNTIME CHECK"
echo "Network: PASS"
echo "G1 route: PASS"
echo "SSH: PASS"
echo "Videohub: PASS"
echo "Camera transport: PASS ($transport)"
HEALTH=$health "$python" -B - <<'PY'
import json, os
h=json.loads(os.environ['HEALTH'])
print('Camera frames: PASS')
print('Resolution: PASS (' + h['resolution'] + ')')
print(f"Frame freshness: PASS ({h['frame_age_ms']:.1f}ms)")
print(f"FPS: PASS ({h['camera_fps']:.1f})")
print('YOLO model: PASS')
print('YOLO SHA256: PASS')
print('GPU: PASS')
print('CUDA: PASS')
print('YOLO load: PASS')
print(f"Warm-up inference: PASS ({h['inference_ms']:.1f}ms)")
PY
echo "Reaction: OFF"
echo "Audio: OFF"
echo "Wander: OFF"
echo "Locomotion: OFF"
echo "VISION READY"
