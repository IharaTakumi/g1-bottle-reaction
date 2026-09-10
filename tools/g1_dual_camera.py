#!/usr/bin/env python3
"""Convenience entry point using the existing dedicated venv."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from g1_bottle_reaction.game_vision.app import main

if __name__ == "__main__":
    raise SystemExit(main(["--source", "dual"] + sys.argv[1:]))
