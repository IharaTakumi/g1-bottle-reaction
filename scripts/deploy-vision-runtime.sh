#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_model=${VISION_KNOWN_GOOD_MODEL:-/home/ubuntu/dev/g1-bottle-reaction/.runtime/models/yolo11n.pt}
config=$root/config/vision_runtime.yaml
readarray -t values < <("/home/ubuntu/.venvs/g1-game-vision/bin/python" -B - "$config" <<'PY'
import sys, yaml
data=yaml.safe_load(open(sys.argv[1]))
print(data['model'])
print(data['model_sha256'])
PY
)
model=${values[0]}
expected=${values[1]}
fail() { echo "VISION_DEPLOY_REFUSED: $*" >&2; exit 2; }
test -s "$source_model" || fail "known-good model missing: $source_model"
actual=$(sha256sum "$source_model" | awk '{print $1}')
[[ $actual == "$expected" ]] || fail "known-good model hash mismatch"
if [[ ${1:-} == --dry-run ]]; then
  echo "KNOWN_GOOD_MODEL=$source_model"
  echo "MODEL_TARGET=$model"
  echo "MODEL_SHA256=$expected"
  echo "DRY_RUN: copy NONE; download NONE; install NONE"
  exit 0
fi
[[ $# == 0 ]] || fail "usage: $0 [--dry-run]"
if [[ -s $model ]] && [[ $(sha256sum "$model" | awk '{print $1}') == "$expected" ]]; then
  echo VISION_RUNTIME_ALREADY_READY
  exit 0
fi
mkdir -p -- "$(dirname -- "$model")"
stage="$model.stage.$$"
trap 'rm -f -- "$stage"' EXIT
cp -- "$source_model" "$stage"
chmod 0644 "$stage"
[[ $(sha256sum "$stage" | awk '{print $1}') == "$expected" ]] || fail "staged model hash mismatch"
mv -- "$stage" "$model"
echo VISION_RUNTIME_DEPLOYED
