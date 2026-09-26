#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
runtime=$root/.runtime/vision
pid_file=$runtime/vision.pid
session_file=$runtime/vision.session
if [[ ${1:-} == --dry-run ]]; then
  echo "DRY_RUN: stop only Vision-owned process group from $pid_file"
  echo "Reaction/Wander/Locomotion/videohub/SSH untouched"
  exit 0
fi
[[ $# == 0 ]] || { echo "usage: $0 [--dry-run]" >&2; exit 2; }
if [[ ! -e $pid_file ]]; then
  echo VISION_ALREADY_STOPPED
  exit 0
fi
pid=$(<"$pid_file")
if [[ ! $pid =~ ^[1-9][0-9]*$ ]] || ! kill -0 "$pid" 2>/dev/null; then
  rm -f -- "$pid_file" "$session_file"
  echo VISION_STALE_MARKER_REMOVED
  exit 0
fi
[[ $(readlink -f "/proc/$pid/cwd") == "$root" ]] || { echo VISION_STOP_IDENTITY_MISMATCH >&2; exit 3; }
cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline")
[[ $cmd == *"$root/tools/g1_dual_camera.py"* && $cmd == *"--g1-camera-transport ssh-rtp"* ]] || { echo VISION_STOP_IDENTITY_MISMATCH >&2; exit 3; }
[[ $(ps -o pgid= -p "$pid" | tr -d ' ') == "$pid" ]] || { echo VISION_STOP_PROCESS_GROUP_MISMATCH >&2; exit 3; }
kill -INT -- "-$pid"
for unused in $(seq 1 150); do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f -- "$pid_file" "$session_file"
    echo VISION_STOPPED
    exit 0
  fi
  sleep .1
done
echo VISION_STOP_TIMEOUT >&2
exit 4
