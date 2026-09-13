#!/usr/bin/env python3
"""Print the self-contained guarded 1801 client for execution through SSH stdin."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source():
    boundary = (ROOT / 'robot_side/adapters/g1_robot.py').read_text()
    command = (ROOT / 'scripts/g1-pc2-mapping-once.py').read_text()
    lines = [line for line in command.splitlines()
             if not line.startswith('sys.path.insert(')
             and not line.startswith('from robot_side.adapters.g1_robot import ')]
    return boundary + '\n' + '\n'.join(lines) + '\n'


if __name__ == '__main__':
    print(source(), end='')
