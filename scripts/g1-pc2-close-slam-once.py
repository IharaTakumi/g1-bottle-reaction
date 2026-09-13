#!/usr/bin/env python3
"""Explicit one-shot PC2-local official SLAM close (API 1901).

The default is a dry run. Real execution requires both opt-in flags. Exactly
one close RPC is made with the official empty-data payload; there is no retry,
save, localization, navigation, motion command, or cleanup RPC.
"""
import argparse
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot_side.adapters.g1_robot import DDS_XML, configure_sdk_path, require_pc2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--enable-real-robot', action='store_true')
    parser.add_argument('--rpc-timeout', type=float, default=10)
    args = parser.parse_args()
    if args.execute != args.enable_real_robot:
        parser.error('real execution requires both --execute and --enable-real-robot')
    if not 0 < args.rpc_timeout <= 20:
        parser.error('--rpc-timeout must be in (0,20]')

    parameter = {'data': {}}
    preview = {'interface': 'eth0', 'domain': 0, 'service': 'slam_operate',
               'api_id': 1901, 'parameter': parameter, 'rpc_calls_max': 1,
               'save_commands': 0, 'localization_commands': 0,
               'navigation_commands': 0, 'motion_commands': 0,
               'preview': not args.execute}
    print(json.dumps({'slam_close': preview}, indent=2), flush=True)
    if not args.execute:
        return 0

    require_pc2()
    configure_sdk_path()
    from unitree_sdk2py.core import channel
    from unitree_sdk2py.rpc.client import Client
    previous = channel.ChannelConfigHasInterface
    try:
        channel.ChannelConfigHasInterface = DDS_XML
        channel.ChannelFactoryInitialize(0, 'eth0')
    finally:
        channel.ChannelConfigHasInterface = previous
    client = Client('slam_operate', False)
    client.SetTimeout(args.rpc_timeout)
    client._SetApiVerson('1.0.0.1')
    client._RegistApi(1901, 0)

    sent_ns = time.monotonic_ns()
    code, raw = client._Call(1901, json.dumps(parameter, allow_nan=False))
    finished_ns = time.monotonic_ns()
    try:
        response = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        response = {'unparsed': str(raw)[:16384]}
    print(json.dumps({'rpc': {'api_id': 1901, 'call_count': 1,
          'return_code': code, 'response': response,
          'elapsed_ms': (finished_ns - sent_ns) / 1e6}}, indent=2), flush=True)
    del client
    return 0 if code == 0 else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        print('SLAM close failed without retry:', exc, file=sys.stderr)
        raise SystemExit(2)
