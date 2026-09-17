#!/usr/bin/env python3
"""Issue at most one tightly bounded G1 locomotion command; dry-run by default."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robot_side.adapters.g1_robot import UnitreeSdkRuntime
from robot_side.wander_locomotion import (
    ALLOWED_ACTIONS,
    DEFAULT_DURATION_S,
    DEFAULT_SPEED,
    MAX_ANGULAR_SPEED_RAD_S,
    MAX_DURATION_S,
    MAX_LINEAR_SPEED_M_S,
    OneShotPlan,
    execute_once,
)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', required=True, choices=ALLOWED_ACTIONS)
    parser.add_argument('--speed', type=float, default=DEFAULT_SPEED)
    parser.add_argument('--duration', type=float, default=DEFAULT_DURATION_S)
    parser.add_argument('--interface', default='eth0')
    parser.add_argument('--timeout', type=float, default=2.0)
    parser.add_argument('--robot', choices=('mock', 'g1'), default='mock')
    parser.add_argument('--enable-real-robot', action='store_true')
    parser.add_argument('--execute-real-g1', action='store_true')
    parser.add_argument('--i-understand-this-will-move-the-robot', action='store_true')
    return parser


def main(argv=None, runtime=None):
    args = build_parser().parse_args(argv)
    if args.timeout <= 0 or args.timeout > 5.0:
        raise ValueError('--timeout must be in (0, 5]')
    plan = OneShotPlan(args.action, args.speed, args.duration)
    plan.validate()
    print('ONE-SHOT INTENT: ' + plan.describe(), flush=True)
    print(
        'HARD LIMITS: linear<=%.2fm/s angular<=%.2frad/s duration<=%.2fs'
        % (MAX_LINEAR_SPEED_M_S, MAX_ANGULAR_SPEED_RAD_S, MAX_DURATION_S),
        flush=True,
    )
    armed = (
        args.robot == 'g1'
        and args.enable_real_robot
        and args.execute_real_g1
        and args.i_understand_this_will_move_the_robot
    )
    if not armed:
        print('DRY RUN', flush=True)
        print('NO G1 COMMAND SENT', flush=True)
        return 0
    print('WARNING: REAL G1 LOCOMOTION -- EXACTLY ONE COMMAND, NO RETRY', flush=True)
    result = execute_once(
        plan,
        runtime or UnitreeSdkRuntime(),
        interface=args.interface,
        timeout=args.timeout,
        robot=args.robot,
        enable_real_robot=args.enable_real_robot,
        execute_real_g1=args.execute_real_g1,
        understand_motion=args.i_understand_this_will_move_the_robot,
    )
    print('COMMAND SENT ONCE result=%r' % (result['result'],), flush=True)
    print('Physical direction and stopping remain operator-observed and unverified.', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        raise SystemExit(2)
