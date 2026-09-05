#!/usr/bin/env bash
set -euo pipefail
project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$project"
# Only these two exact project-local directories are removed. Keep repos/assets.
for directory in .reaction-tools .reaction-cache; do
  [[ ! -L "$directory" ]] || { echo "[ERROR] Refusing symlink: $directory"; exit 2; }
done
if [[ -d .reaction-tools ]]; then
  exec 9>.reaction-tools/setup.lock
  flock -n 9 || { echo '[ERROR] Setup is running; teardown refused'; exit 2; }
fi
printf 'Remove project-local environments and caches under %s? Type DELETE: ' "$project"
read -r answer
[[ "$answer" == DELETE ]] || { echo 'Cancelled'; exit 1; }
rm -rf -- "$project/.reaction-tools" "$project/.reaction-cache"
# Keep configuration for repeatable setup. Doctor reports missing envs until rebuilt.
echo 'Removed environments/caches. GVHMR, GMR, inputs, motions and configuration preserved.'
