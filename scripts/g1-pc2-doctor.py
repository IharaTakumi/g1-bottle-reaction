#!/usr/bin/env python3
"""PC2 READ ONLY import checks; optional bounded subscribers. No RPC or file writes."""
import argparse
import importlib
from importlib import metadata
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot_side.adapters.g1_robot import DDS_XML, UnitreeSdkRuntime, configure_sdk_path, require_pc2


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--subscribe', action='store_true', help='PC2 eth0 DataReaders only; no application publish')
    parser.add_argument('--seconds', type=float, default=15)
    args = parser.parse_args()
    if not 0 < args.seconds <= 60:
        parser.error('--seconds must be in (0,60]')
    report = {'python': sys.version, 'executable': sys.executable, 'machine': platform.machine(),
              'venv': sys.prefix != sys.base_prefix, 'imports': {}, 'missing': []}
    for name, distribution in [('yaml', 'PyYAML'), ('cyclonedds', 'cyclonedds')]:
        try:
            module = importlib.import_module(name)
            try:
                version = metadata.version(distribution)
            except metadata.PackageNotFoundError:
                version = 'unknown (module import succeeded; distribution metadata absent)'
            report['imports'][name] = {'path': module.__file__, 'version': version}
        except Exception as exc:
            report['missing'].append({name: str(exc)})
    try:
        report['sdk_path'] = str(configure_sdk_path())
        runtime = UnitreeSdkRuntime()
        odom, string = runtime.load_readonly_slam_types()
        low = runtime.load_readonly_low_state_type()
        navigation = runtime.load_readonly_navigation_types()
        report['schema_types'] = [t.__idl_typename__ for t in
                                  (low, odom, string, navigation['point_cloud'],
                                   navigation['occupancy_grid'])]
    except Exception as exc:
        report['missing'].append({'sdk_schemas': str(exc)})
    print(json.dumps(report, indent=2), flush=True)
    if report['missing']:
        return 2
    if not args.subscribe:
        return 0
    require_pc2()
    from cyclonedds.builtin import BuiltinDataReader, BuiltinTopicDcpsPublication, BuiltinTopicDcpsSubscription
    from cyclonedds.domain import Domain, DomainParticipant
    from cyclonedds.sub import DataReader
    from cyclonedds.topic import Topic
    from cyclonedds.qos import Qos, Policy
    from cyclonedds.core import Listener
    from cyclonedds.internal import InvalidSample
    domain = Domain(0, DDS_XML)
    participant = DomainParticipant(0)
    own_guid = str(participant.guid)
    builtin_readers = {
        'publication': BuiltinDataReader(participant, BuiltinTopicDcpsPublication),
        'subscription': BuiltinDataReader(participant, BuiltinTopicDcpsSubscription),
    }
    service_endpoints = {'rt/api/slam_operate/request': {'publication': set(), 'subscription': set()},
                         'rt/api/slam_operate/response': {'publication': set(), 'subscription': set()}}
    topics = {
        'rt/lowstate': (low, 'lowstate'),
        'rt/unitree/slam_mapping/odom': (odom, 'odometry'),
        'rt/unitree/slam_relocation/odom': (odom, 'odometry'),
        'rt/slam_info': (string, 'string'),
        'rt/slam_key_info': (string, 'string'),
        'rt/unitree/slam_mapping/points': (navigation['point_cloud'], 'point_cloud'),
        'rt/unitree/slam_relocation/points': (navigation['point_cloud'], 'point_cloud'),
        'rt/unitree/slam_relocation/global_map': (navigation['point_cloud'], 'point_cloud'),
        'rt/global_map': (navigation['occupancy_grid'], 'occupancy_grid'),
    }
    counts = {name: {'samples': 0, 'matched_current': 0, 'matched_total': 0,
                     'invalid_samples': 0, 'sample_lost': 0, 'sample_rejected': 0,
                     'incompatible_qos': 0, '_arrivals_ns': [], '_ticks': set(),
                     'string_types': {}, 'state_by_type': {}} for name in topics}

    def make_matched(name):
        def matched(reader, status):
            counts[name]['matched_current'] = int(status.current_count)
            counts[name]['matched_total'] = int(status.total_count)
        return matched

    def make_status(name, key):
        def status_callback(reader, status):
            counts[name][key] = int(status.total_count)
        return status_callback

    readers = {}
    listeners = {}
    dds_topics = {}
    for name, (typ, kind) in topics.items():
        listeners[name] = Listener(
            on_subscription_matched=make_matched(name),
            on_sample_lost=make_status(name, 'sample_lost'),
            on_sample_rejected=make_status(name, 'sample_rejected'),
            on_requested_incompatible_qos=make_status(name, 'incompatible_qos'))
        dds_topics[name] = Topic(participant, name, typ)
        depth = 1024 if name == 'rt/lowstate' else 8
        readers[name] = DataReader(
            participant, dds_topics[name],
            Qos(Policy.Reliability.BestEffort, Policy.Durability.Volatile,
                Policy.History.KeepLast(depth)),
            listener=listeners[name])

    started_ns = time.monotonic_ns()
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        for direction, builtin in builtin_readers.items():
            for endpoint in builtin.take(100):
                if isinstance(endpoint, InvalidSample) or str(endpoint.participant_key) == own_guid:
                    continue
                if endpoint.topic_name in service_endpoints:
                    service_endpoints[endpoint.topic_name][direction].add(str(endpoint.key))
        for name, reader in readers.items():
            item = counts[name]
            kind = topics[name][1]
            for sample in reader.take(1024):
                if isinstance(sample, InvalidSample):
                    item['invalid_samples'] += 1
                    continue
                item['samples'] += 1
                item['_arrivals_ns'].append(time.monotonic_ns())
                if kind in ('odometry', 'point_cloud', 'occupancy_grid'):
                    item['frame_id'] = sample.header.frame_id
                    item['source_stamp'] = [sample.header.stamp.sec, sample.header.stamp.nanosec]
                    if kind == 'odometry':
                        item['child_frame_id'] = sample.child_frame_id
                    elif kind == 'point_cloud':
                        item['last_data_bytes'] = len(sample.data)
                        item['last_points'] = int(sample.width) * int(sample.height)
                    else:
                        item['last_cells'] = len(sample.data)
                elif kind == 'lowstate':
                    item['tick'] = sample.tick
                    item['_ticks'].add(int(sample.tick))
                elif kind == 'string':
                    try:
                        payload = json.loads(sample.data)
                        category = str(payload.get('type', 'default'))[:128]
                        item['string_types'][category] = item['string_types'].get(category, 0) + 1
                        item['last_string'] = sample.data[:16384]
                        data = payload.get('data', {}) if isinstance(payload, dict) else {}
                        machine = data.get('stateMachine', {}) if isinstance(data, dict) else {}
                        item['state_by_type'][category] = {
                            'state': machine.get('state'),
                            'ctrName': machine.get('ctrName'),
                            'info': payload.get('info'),
                            'errorCode': payload.get('errorCode'),
                            'pcdName': data.get('pcdName'),
                            'address': data.get('address'),
                        }
                    except (TypeError, ValueError):
                        item['string_types']['non_json'] = item['string_types'].get('non_json', 0) + 1
                        item['last_string'] = str(sample.data)[:16384]
        time.sleep(min(0.0005, max(0, end - time.monotonic())))
    ended_ns = time.monotonic_ns()
    elapsed = (ended_ns - started_ns) / 1e9
    for name, item in counts.items():
        arrivals = item.pop('_arrivals_ns')
        ticks = item.pop('_ticks')
        intervals_ms = [(b - a) / 1e6 for a, b in zip(arrivals, arrivals[1:])]
        item['distinct_ticks'] = len(ticks)
        item['rate_hz'] = item['samples'] / elapsed
        item['first_sample_delay_ms'] = ((arrivals[0] - started_ns) / 1e6) if arrivals else None
        item['ending_gap_ms'] = ((ended_ns - arrivals[-1]) / 1e6) if arrivals else None
        item['interval_mean_ms'] = statistics.fmean(intervals_ms) if intervals_ms else None
        item['interval_median_ms'] = statistics.median(intervals_ms) if intervals_ms else None
        item['interval_p95_ms'] = percentile(intervals_ms, 0.95)
        item['interval_p99_ms'] = percentile(intervals_ms, 0.99)
        item['max_gap_ms'] = max(intervals_ms) if intervals_ms else None
        stale_ms = 100 if name == 'rt/lowstate' else 1000
        item['stale_threshold_ms'] = stale_ms
        item['stale_events'] = sum(value > stale_ms for value in intervals_ms)
        item['stops_over_1s'] = sum(value > 1000 for value in intervals_ms)
    endpoint_counts = {name: {direction: len(keys) for direction, keys in directions.items()}
                       for name, directions in service_endpoints.items()}
    print(json.dumps({'topic_results': counts, 'service_endpoint_counts': endpoint_counts,
          'receive_elapsed_s': elapsed,
          'interface': 'eth0', 'domain': 0, 'application_writers': 0, 'rpc_clients': 0,
          'note': ('Arrival intervals are bounded receive-loop observations. Zero samples is '
                   'unverified, not proof of no map or missing dependency.')}, indent=2))
    del readers, dds_topics, builtin_readers, participant, domain
    return 0 if counts['rt/lowstate']['samples'] else 3


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        print('READ ONLY diagnostic failed:', exc, file=sys.stderr)
        raise SystemExit(2)
