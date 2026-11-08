#!/usr/bin/env python3
"""Open the G1 game operator dashboard on this PC: http://127.0.0.1:8770/

Closing the browser or this server does not stop a running game; start the
server again to reconnect. The emergency stop on the page stops walking only.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from g1_bottle_reaction.dashboard.server import serve  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--port", type=int, default=8770)
    serve(parser.parse_args().port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
