#!/usr/bin/env python3
"""Listen to domain-0 SPDP multicast on G1 Ethernet; never send a datagram.

Only RTPS packet headers and source addresses are recorded, not payloads.
This identifies network origin of participant GUID prefixes, not remote PIDs.
"""
import argparse
import json
import socket
import time


def rtps_identity(data):
    if len(data) < 20 or data[:4] != b'RTPS':
        return None
    return {'guid_prefix': data[8:20].hex(), 'protocol': list(data[4:6]),
            'vendor_id': data[6:8].hex()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=25)
    args = parser.parse_args()
    if not 0 < args.seconds <= 60:
        parser.error('--seconds must be in (0, 60]')
    interface = 'enp129s0'
    local_ip = '192.168.123.99'
    ifindex = socket.if_nametoindex(interface)
    membership = socket.inet_aton('239.255.0.1') + socket.inet_aton(local_ip) + ifindex.to_bytes(4, 'little')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Linux IP_MULTICAST_ALL=0: receive only groups joined by this socket.
        sock.setsockopt(socket.IPPROTO_IP, 49, 0)
        sock.bind(('', 7400))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.settimeout(0.5)
        deadline = time.monotonic() + args.seconds
        seen = {}
        while time.monotonic() < deadline:
            try:
                data, (address, port) = sock.recvfrom(65535)
            except socket.timeout:
                continue
            identity = rtps_identity(data)
            if identity is None or address not in {'192.168.123.161', '192.168.123.164'}:
                continue
            key = (address, identity['guid_prefix'])
            if key not in seen:
                seen[key] = {'source_ip': address, 'source_port': port, **identity, 'packets': 0}
            seen[key]['packets'] += 1
        for row in seen.values():
            print(json.dumps(row))
        print(json.dumps({'event': 'summary', 'participants': len(seen),
                          'interface': interface, 'local_ip': local_ip, 'seconds': args.seconds}))
        return 0 if seen else 2


if __name__ == '__main__':
    raise SystemExit(main())
