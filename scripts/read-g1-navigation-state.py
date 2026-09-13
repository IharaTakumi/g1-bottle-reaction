#!/usr/bin/env python3
"""Receive allowlisted navigation telemetry; no application writers or RPCs.

DDS discovery and acknowledgement traffic is necessary. The domain remains 0;
the existing interface can be selected per invocation. No navigation coordinator
is instantiated.
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import statistics
import time
import xml.etree.ElementTree as ET

# Do not add command/request topics. Schema names must match live discovery.
TOPICS = {
    'rt/odommodestate': 'odomstate',
    'rt/lf/odommodestate': 'odomstate',
    'rt/dog_odom': 'odometry',
    'rt/unitree/slam_mapping/odom': 'odometry',
    'rt/unitree/slam_relocation/odom': 'odometry',
    'rt/slam_info': 'string',
    'rt/slam_key_info': 'string',
    'rt/unitree_slam/waypoints': 'string',
    'rt/utlidar/cloud_livox_mid360': 'cloud',
    'rt/utlidar/imu_livox_mid360': 'imu',
    'rt/unitree/slam_mapping/points': 'cloud',
    'rt/unitree/slam_relocation/points': 'cloud',
    'rt/unitree/slam_relocation/global_map': 'cloud',
}


def summarize(kind, sample):
    """Bound output and preserve source stamps/frames; never infer readiness."""
    if kind == 'string':
        if len(sample.data) > 16384:
            return {'truncated': True, 'text': sample.data[:16384]}
        try:
            return {'json': json.loads(sample.data)}
        except ValueError:
            return {'text': sample.data}
    if kind == 'cloud':
        return {'header': asdict(sample.header), 'height': sample.height, 'width': sample.width,
                'points': sample.width * sample.height,
                'point_step': sample.point_step, 'row_step': sample.row_step,
                'data_bytes': len(sample.data), 'fields': [asdict(f) for f in sample.fields],
                'layout_consistent': len(sample.data) == sample.row_step * sample.height
                    and sample.row_step >= sample.width * sample.point_step,
                'is_dense': sample.is_dense, 'is_bigendian': sample.is_bigendian}
    if kind == 'imu':
        return {'header': asdict(sample.header),
                'orientation': asdict(sample.orientation),
                'angular_velocity': asdict(sample.angular_velocity),
                'linear_acceleration': asdict(sample.linear_acceleration)}
    if kind == 'odomstate':
        return {'stamp': asdict(sample.stamp), 'position': list(sample.position),
                'velocity': list(sample.velocity), 'imu_rpy': list(sample.imu_state.rpy),
                'mode': sample.mode, 'error_code': sample.error_code,
                'frame_id': None, 'pose_validity': 'not established'}
    return asdict(sample)


def source_stamp(payload):
    value = payload.get('json', payload)
    if not isinstance(value, dict):
        return None
    header = value.get('header')
    stamp = header.get('stamp') if isinstance(header, dict) else value.get('stamp', value)
    if isinstance(stamp, dict) and 'sec' in stamp and 'nanosec' in stamp:
        return (stamp['sec'], stamp['nanosec'])
    return None


def config_for_interface(base_xml, interface):
    root = ET.fromstring(base_xml)
    nodes = root.findall('./Domain/General/Interfaces/NetworkInterface')
    if len(nodes) != 1:
        raise ValueError('expected exactly one CycloneDDS NetworkInterface')
    nodes[0].set('name', interface)
    return ET.tostring(root, encoding='unicode')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', default=os.environ.get('G1_NETWORK_INTERFACE', 'enp129s0'))
    parser.add_argument('--seconds', type=float, default=15, help='Receive window after discovery (max 60)')
    parser.add_argument('--discovery-seconds', type=float, default=5,
                        help='Discovery window before selecting matching schemas (max 60)')
    parser.add_argument('--group', choices=['state', 'cloud', 'all'], default='state')
    parser.add_argument('--sample-period', type=float, default=2,
                        help='Seconds between sample summaries; 0 logs every received sample (no cloud payload)')
    args = parser.parse_args()
    if not 0 < args.seconds <= 60:
        parser.error('--seconds must be in (0, 60]')
    if not 0 < args.discovery_seconds <= 60:
        parser.error('--discovery-seconds must be in (0, 60]')
    if not 0 <= args.sample_period <= 60:
        parser.error('--sample-period must be in [0, 60]')
    socket.if_nametoindex(args.interface)
    root = Path(__file__).resolve().parents[1]
    config = config_for_interface(
        (root / 'config/g1-readonly-dds.xml').read_text(), args.interface)
    os.environ['CYCLONEDDS_URI'] = config
    from cyclonedds.domain import Domain, DomainParticipant
    from cyclonedds.builtin import BuiltinDataReader, BuiltinTopicDcpsPublication, BuiltinTopicDcpsSubscription
    from cyclonedds.core import Listener
    from cyclonedds.internal import InvalidSample
    from cyclonedds.sub import DataReader
    from cyclonedds.topic import Topic
    from cyclonedds.qos import Qos, Policy
    from g1_bottle_reaction.adapters.g1_robot import UnitreeSdkRuntime

    domain = Domain(0, config)
    participant = DomainParticipant(0)
    own_guid = str(participant.guid)
    emit = lambda obj: print(json.dumps(obj), flush=True)
    emit({'event': 'start', 'interface': args.interface, 'domain': 0,
          'own_participant': own_guid, 'receive_seconds': args.seconds, 'group': args.group,
          'discovery_seconds': args.discovery_seconds, 'sample_period': args.sample_period})
    builtins = [('publication', BuiltinDataReader(participant, BuiltinTopicDcpsPublication)),
                ('subscription', BuiltinDataReader(participant, BuiltinTopicDcpsSubscription))]
    published = {}
    seen = set()
    deadline = time.monotonic() + args.discovery_seconds
    while time.monotonic() < deadline:
        for direction, reader in builtins:
            for s in reader.take(100):
                if isinstance(s, InvalidSample) or str(s.participant_key) == own_guid:
                    continue
                if direction == 'publication':
                    published.setdefault(s.topic_name, set()).add(s.type_name)
                key = (direction, str(s.key))
                if key in seen:
                    continue
                seen.add(key)
                emit({'event': 'endpoint', 'direction': direction, 'topic': s.topic_name,
                      'type': s.type_name, 'participant': str(s.participant_key),
                      'qos': str(s.qos)})
        time.sleep(0.05)

    schemas = UnitreeSdkRuntime().load_readonly_navigation_probe_types()
    readers, topics, listeners, stats = {}, {}, {}, {}
    for name, kind in TOPICS.items():
        if args.group != 'all' and (kind == 'cloud') != (args.group == 'cloud'):
            continue
        schema = schemas[kind]
        expected = schema.__idl_typename__.replace('.', '::')
        stats[name] = {'kind': kind, 'samples': 0, 'matched_current': 0, 'matched_total': 0,
                       'incompatible_qos': 0, 'stamp_changes': 0, 'last_stamp': None,
                       'last_by_type': {}, 'observed_types': sorted(published.get(name, set())),
                       'reader_created_unix_ns': None, 'first_match_unix_ns': None,
                       'first_sample_unix_ns': None, 'last_sample_unix_ns': None,
                       '_arrival_monotonic_ns': []}
        item = stats[name]
        if expected not in published.get(name, set()):
            item['skipped'] = 'no publication with matching SDK type in discovery window'
            continue
        def matched(reader, status, item=item):
            item['matched_current'] = status.current_count
            item['matched_total'] = status.total_count
            if status.current_count > 0 and item['first_match_unix_ns'] is None:
                item['first_match_unix_ns'] = time.time_ns()
        def incompatible(reader, status, item=item):
            item['incompatible_qos'] = status.total_count
        listeners[name] = Listener(on_subscription_matched=matched, on_requested_incompatible_qos=incompatible)
        topics[name] = Topic(participant, name, schema)
        item['reader_created_unix_ns'] = time.time_ns()
        readers[name] = DataReader(participant, topics[name],
            Qos(Policy.Reliability.BestEffort, Policy.Durability.Volatile, Policy.History.KeepLast(1)),
            listener=listeners[name])
    next_print = {}
    receive_started = time.monotonic()
    deadline = receive_started + args.seconds
    while time.monotonic() < deadline:
        for name, reader in readers.items():
            item = stats[name]
            for s in reader.take(1):
                if isinstance(s, InvalidSample):
                    continue
                payload = summarize(item['kind'], s)
                received_unix_ns = time.time_ns()
                received_monotonic_ns = time.monotonic_ns()
                item['samples'] += 1
                item['_arrival_monotonic_ns'].append(received_monotonic_ns)
                if item['first_sample_unix_ns'] is None:
                    item['first_sample_unix_ns'] = received_unix_ns
                item['last_sample_unix_ns'] = received_unix_ns
                stamp = source_stamp(payload)
                if stamp is not None and stamp != item['last_stamp']:
                    item['stamp_changes'] += 1
                    item['last_stamp'] = stamp
                raw = payload.get('json')
                category = str(raw.get('type', 'default'))[:128] if isinstance(raw, dict) else 'default'
                # Cap categories to avoid unbounded memory from telemetry strings.
                if category in item['last_by_type'] or len(item['last_by_type']) < 32:
                    item['last_by_type'][category] = payload
                key = (name, category)
                if time.monotonic() >= next_print.get(key, 0):
                    emit({'event': 'sample', 'topic': name, 'payload': payload,
                          'received_monotonic': time.monotonic(),
                          'received_unix_ns': received_unix_ns})
                    next_print[key] = time.monotonic() + args.sample_period
        time.sleep(0.02)
    elapsed = time.monotonic() - receive_started
    for item in stats.values():
        item['observed_rate_hz'] = item['samples'] / elapsed
        arrivals = item.pop('_arrival_monotonic_ns')
        intervals_ms = [(b - a) / 1e6 for a, b in zip(arrivals, arrivals[1:])]
        item['interval_mean_ms'] = statistics.fmean(intervals_ms) if intervals_ms else None
        item['interval_median_ms'] = statistics.median(intervals_ms) if intervals_ms else None
        item['max_gap_ms'] = max(intervals_ms) if intervals_ms else None
        item['stale_events_over_1s'] = sum(value > 1000 for value in intervals_ms)
    emit({'event': 'summary', 'receive_elapsed_s': elapsed, 'topics': stats})
    return 0 if any(v['samples'] for v in stats.values()) else 2


if __name__ == '__main__':
    raise SystemExit(main())
