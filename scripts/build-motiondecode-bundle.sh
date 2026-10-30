#!/usr/bin/env bash
set -euo pipefail

# Build a PC2 MotionDecode runtime bundle from one motiondecode-test commit,
# reusing the verified aarch64 dependency tree of an existing bundle.
# Offline only: no SSH, no robot.  Deploy with
#   MOTIONDECODE_KNOWN_GOOD_BUNDLE=<out> scripts/deploy-motiondecode-runtime.sh
usage() { echo "usage: $0 <motiondecode-test repo> <commit> <out dir> [deps bundle]" >&2; exit 2; }
[[ $# == 3 || $# == 4 ]] || usage
repo=$1; commit=$2; out=$3
deps_bundle=${4:-/home/ubuntu/dev/g1-bottle-reaction/.runtime/motiondecode-known-good}
fail() { echo "BUILD_REFUSED: $*" >&2; exit 2; }

[[ ! -e $out ]] || fail "output already exists: $out"
sha=$(git -C "$repo" rev-parse --verify "$commit^{commit}") || fail "unknown commit: $commit"
(cd "$deps_bundle/deps" && sha256sum -c --quiet MANIFEST.sha256) || fail "deps manifest mismatch: $deps_bundle"

stage=$(mktemp -d "$(dirname -- "$out")/.bundle-stage.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
mkdir -p "$stage/code"
git -C "$repo" archive "$sha" | tar -x -C "$stage/code"
# Run outputs are evidence, not runtime; external/cyclonedds is the Ubuntu x86
# build (PC2 uses its own CycloneDDS install).
rm -rf -- "$stage/code/output" "$stage/code/external/cyclonedds"
(cd "$stage/code" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z |
  xargs -0 sha256sum > MANIFEST.sha256)
cp -a -- "$deps_bundle/deps" "$stage/deps"
reactions=$(python3 -c 'import json,sys; print(", ".join(json.load(open(sys.argv[1]))["reactions"]))' \
  "$stage/code/config/named_reactions.json")
cat > "$stage/BUNDLE_INFO.txt" <<EOF
MotionDecode runtime bundle
Source: motiondecode-test $sha
Built: $(date -Iseconds)
Target: PC2 Linux aarch64, /usr/bin/python3 (Python 3.8)
Dependencies: copied from $deps_bundle/deps
CycloneDDS runtime: existing PC2 /home/unitree/cyclonedds_ws/install/cyclonedds
Unitree SDK runtime: existing PC2 /home/unitree/unitree_sdk2_python
Profiles: $reactions
Excluded: output/, external/cyclonedds (Ubuntu x86)
EOF
(cd "$stage" && sha256sum BUNDLE_INFO.txt code/MANIFEST.sha256 deps/MANIFEST.sha256 > BUNDLE_MANIFEST.sha256)
mv -- "$stage" "$out"
trap - EXIT
echo "BUNDLE_BUILT=$out SOURCE=$sha"
