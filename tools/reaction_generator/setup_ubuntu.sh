#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/ubuntu/runtime.sh"
cd "$reaction_project"
[[ $(uname -sm) == 'Linux x86_64' ]] || { echo '[ERROR] Linux x86_64 required'; exit 2; }
mkdir -p .reaction-tools/{bin,envs,locks} .reaction-cache
exec 9>.reaction-tools/setup.lock
flock -n 9 || { echo '[ERROR] Another setup is running'; exit 2; }
python3 tools/reaction_generator/ubuntu/setup.py "$@"
