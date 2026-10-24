#!/usr/bin/env bash
set -euo pipefail

root=/home/ubuntu/dev/g1-bottle-reaction
control=${G1_SSH_CONTROL:-$root/.runtime/usb-camera-ssh/wifi-control}
target=${G1_SSH_TARGET:?Set G1_SSH_TARGET to the currently routed G1 session}
host=${target##*@}
route=$(ip -j route get "$host")
read -r interface local_ip < <(ROUTE="$route" python3 - <<'PY'
import json, os
r = json.loads(os.environ['ROUTE'])
if len(r) != 1 or not r[0].get('dev') or not r[0].get('prefsrc'):
    raise SystemExit('G1 route is not uniquely resolved')
print(r[0]['dev'], r[0]['prefsrc'])
PY
)

command=(/home/ubuntu/.venvs/g1-game-vision/bin/python -B tools/g1_dual_camera.py \
  --usb-bind "$local_ip" \
  --usb-host "$host" \
  --network-interface "$interface" \
  --ssh-target "$target" \
  --ssh-control "$control" \
  --g1-camera-transport ssh-rtp \
  --g1-camera-port 56001 \
  --g1-camera-fps 30 \
  --no-usb-camera \
  --windowed \
  --yolo \
  --yolo-model "$root/.runtime/models/yolo11n.pt" \
  --yolo-confidence 0.25 \
  --banana-confidence 0.25 \
  --plushie-confidence 0.25 \
  --reaction-target all \
  --found-audio \
  --found-output g1 \
  --found-duration 0.3 \
  --audio-cooldown 2.0 \
  --robot motiondecode \
  --motiondecode-repository /home/ubuntu/dev/motiondecode-test \
  --motiondecode-transport ssh \
  --motiondecode-socket /tmp/motiondecode-reaction.sock \
  --motiondecode-timeout 30 \
  --enable-real-robot \
  --confirm-site-ready \
  --hackathon-runtime \
  --with-wander \
  --wander-ssh-target "$target" \
  --wander-remote-dir /home/unitree/g1-bottle-reaction-wander)

# This preflight only connects to an already-running worker. It never starts,
# deploys, installs, or searches for a MotionDecode environment.
if [[ ${1:-} == --dry-run ]]; then
  "$root/scripts/check-demo-ready.sh" --dry-run
  printf 'DEMO_COMMAND='
  printf '%q ' "${command[@]}"
  printf '\n'
  exit 0
fi

"$root/scripts/check-demo-ready.sh"
cd "$root"
exec "${command[@]}"
