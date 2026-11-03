from types import SimpleNamespace
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from patrol import run_patrol


def test_writer_conflicts_allows_reactions_and_blocks_locomotion_writers(monkeypatch):
    processes = """\
101 python3 python3 tools/g1_dual_camera.py --robot motiondecode
102 python3 python3 -u scripts/resident_worker.py --reaction found
103 python3 python3 walk_forward_real.py
104 python3 python3 patrol/run_patrol.py --mode real
"""
    monkeypatch.setattr(
        run_patrol.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=processes),
    )
    monkeypatch.setattr(run_patrol.os, "getpid", lambda: 999)

    conflicts = run_patrol.writer_conflicts()

    assert conflicts == [
        "103 python3 python3 walk_forward_real.py",
        "104 python3 python3 patrol/run_patrol.py --mode real",
    ]
