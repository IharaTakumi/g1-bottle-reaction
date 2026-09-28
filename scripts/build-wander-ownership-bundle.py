#!/usr/bin/env python3
"""Build an offline, self-contained source bundle; never deploy or launch it."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "scripts/g1-wander-reactive-mvp.py", "scripts/g1-wander-live-source.py",
    "robot_side/__init__.py", "robot_side/adapters/__init__.py",
    "robot_side/adapters/g1_robot.py", "robot_side/ownership_lock.py",
    "robot_side/wander_reactive_mvp.py", "robot_side/wander_live.py",
    "config/wander_live_pc2.json",
)


def build(output, root=ROOT):
    payloads = {name: (root / name).read_bytes() for name in FILES}
    manifest = {
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "ownership_contract": "--require-locomotion-ownership-v1",
        "canonical_lock": "/tmp/g1-project-locomotion.lock",
        "sha256": {name: hashlib.sha256(data).hexdigest()
                   for name, data in payloads.items()},
    }
    with tarfile.open(output, "w:gz") as archive:
        for name, data in list(payloads.items()) + [
                ("ownership-manifest.json", json.dumps(manifest, indent=2).encode())]:
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    print(json.dumps(build(parser.parse_args().output), indent=2))
