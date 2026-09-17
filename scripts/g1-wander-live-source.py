#!/usr/bin/env python3
"""Stream only MID-360 + dog_odom as compact JSONL; never create a writer/RPC."""

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robot_side.adapters.g1_robot import UnitreeSdkRuntime
from robot_side.wander_live import decode_xyz, odom_payload, reduce_points, source_stamp_seconds


CLOUD_TOPIC = 'rt/utlidar/cloud_livox_mid360'
ODOM_TOPIC = 'rt/dog_odom'


def config_for_interface(base_xml, interface):
    root = ET.fromstring(base_xml)
    nodes = root.findall('./Domain/General/Interfaces/NetworkInterface')
    if len(nodes) != 1:
        raise ValueError('expected exactly one CycloneDDS NetworkInterface')
    nodes[0].set('name', interface)
    return ET.tostring(root, encoding='unicode')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', default=os.environ.get('G1_NETWORK_INTERFACE', 'eth0'))
    parser.add_argument('--config', type=Path, default=ROOT / 'config/wander_live_pc2.json')
    parser.add_argument('--seconds', type=float, default=0.0, help='0 runs until Ctrl+C')
    parser.add_argument('--debug-axis', action='store_true')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.seconds < 0 or args.seconds > 3600:
        raise ValueError('--seconds must be 0 or in (0, 3600]')
    socket.if_nametoindex(args.interface)
    config = json.loads(args.config.read_text(encoding='utf-8'))
    dds_xml = config_for_interface(
        (ROOT / 'config/g1-readonly-dds.xml').read_text(encoding='utf-8'),
        args.interface,
    )
    os.environ['CYCLONEDDS_URI'] = dds_xml

    from cyclonedds.domain import Domain, DomainParticipant
    from cyclonedds.internal import InvalidSample
    from cyclonedds.qos import Policy, Qos
    from cyclonedds.sub import DataReader
    from cyclonedds.topic import Topic

    schemas = UnitreeSdkRuntime().load_readonly_navigation_types()
    domain = Domain(0, dds_xml)
    participant = DomainParticipant(0)
    qos = Qos(
        Policy.Reliability.BestEffort,
        Policy.Durability.Volatile,
        Policy.History.KeepLast(1),
    )
    topics = {
        'cloud': Topic(participant, CLOUD_TOPIC, schemas['point_cloud']),
        'odom': Topic(participant, ODOM_TOPIC, schemas['odometry']),
    }
    readers = {name: DataReader(participant, topic, qos) for name, topic in topics.items()}
    emit = lambda value: print(json.dumps(value, separators=(',', ':')), flush=True)
    latest_odom = None
    latest_odom_stamp = None
    latest_odom_changed = None
    latest_cloud_stamp = None
    latest_cloud_changed = None
    deadline = None if args.seconds == 0 else time.monotonic() + args.seconds
    try:
        while deadline is None or time.monotonic() < deadline:
            for sample in readers['odom'].take(1):
                if isinstance(sample, InvalidSample):
                    continue
                latest_odom = odom_payload(sample)
                odom_stamp = latest_odom['source_timestamp']
                if odom_stamp is not None and odom_stamp != latest_odom_stamp:
                    latest_odom_stamp = odom_stamp
                    latest_odom_changed = time.monotonic()
            for sample in readers['cloud'].take(1):
                if isinstance(sample, InvalidSample):
                    continue
                received = time.monotonic()
                try:
                    cloud_stamp = source_stamp_seconds(sample.header)
                    if cloud_stamp is None:
                        raise ValueError('PointCloud2 source timestamp is missing')
                    if cloud_stamp != latest_cloud_stamp:
                        latest_cloud_stamp = cloud_stamp
                        latest_cloud_changed = received
                    reduced = reduce_points(decode_xyz(sample), config, args.debug_axis)
                    record = {
                        'timestamp': time.time(),
                        'cloud_source_timestamp': cloud_stamp,
                        'cloud_age_s': max(0.0, time.monotonic() - latest_cloud_changed),
                        'odom_age_s': (
                            None if latest_odom_changed is None
                            else max(0.0, time.monotonic() - latest_odom_changed)
                        ),
                        'odom': latest_odom,
                    }
                    record.update(reduced)
                    emit(record)
                except Exception as exc:
                    emit({
                        'timestamp': time.time(),
                        'valid': False,
                        'invalid_reason': '%s: %s' % (type(exc).__name__, exc),
                        'odom': latest_odom,
                    })
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
