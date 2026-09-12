#!/usr/bin/env python3
"""Extended G1 navigation telemetry diagnosis with readers only.

No application Publisher, DataWriter, Unitree channel, RPC client, motion adapter,
or SLAM operation is constructed. One DDS participant performs discovery first,
then keeps allowlisted readers alive for a bounded observation window.
"""
import argparse
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import time
import xml.etree.ElementTree as ET


TYPE_GO_SPORT = 'unitree_go::msg::dds_::SportModeState_'
TYPE_HG_SPORT = 'unitree_hg::msg::dds_::SportModeState_'
TYPE_ODOM = 'nav_msgs::msg::dds_::Odometry_'
TYPE_CLOUD = 'sensor_msgs::msg::dds_::PointCloud2_'
TYPE_IMU = 'sensor_msgs::msg::dds_::Imu_'
TYPE_RANGE = 'geometry_msgs::msg::dds_::PointStamped_'

# Values are live DDS type name -> local schema key. A reader is never made
# solely from the topic name, and command/request topics are deliberately absent.
TARGETS = {
    'rt/dog_odom': {TYPE_ODOM: 'odometry'},
    'rt/odommodestate': {TYPE_GO_SPORT: 'odomstate'},
    'rt/lf/odommodestate': {TYPE_GO_SPORT: 'odomstate'},
    'rt/sportmodestate': {TYPE_GO_SPORT: 'odomstate', TYPE_HG_SPORT: 'hg_sport'},
    'rt/lf/sportmodestate': {TYPE_GO_SPORT: 'odomstate', TYPE_HG_SPORT: 'hg_sport'},
    'rt/utlidar/cloud_livox_mid360': {TYPE_CLOUD: 'cloud'},
    'rt/utlidar/imu_livox_mid360': {TYPE_IMU: 'imu'},
    'rt/utlidar/range_info': {TYPE_RANGE: 'range'},
}
LIDAR_SWITCH = 'rt/utlidar/switch'


def yaw_from_quaternion(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y),
                      1 - 2 * (q.y * q.y + q.z * q.z))


def summarize(kind, sample):
    if kind == 'odomstate':
        return {'position': list(sample.position), 'velocity': list(sample.velocity),
                'yaw_rad': sample.imu_state.rpy[2], 'imu_rpy': list(sample.imu_state.rpy),
                'mode': sample.mode, 'error_code': sample.error_code,
                'stamp': asdict(sample.stamp)}
    if kind == 'hg_sport':
        return {'fsm_id': sample.fsm_id, 'fsm_mode': sample.fsm_mode,
                'task_id': sample.task_id, 'task_time': sample.task_time,
                'position': None, 'velocity': None, 'yaw_rad': None,
                'note': 'official hg schema has no odometry fields'}
    if kind == 'odometry':
        pose = sample.pose.pose
        return {'header': asdict(sample.header), 'child_frame_id': sample.child_frame_id,
                'position': asdict(pose.position), 'orientation': asdict(pose.orientation),
                'yaw_rad': yaw_from_quaternion(pose.orientation),
                'linear_velocity': asdict(sample.twist.twist.linear),
                'angular_velocity': asdict(sample.twist.twist.angular)}
    if kind == 'cloud':
        return {'header': asdict(sample.header), 'height': sample.height, 'width': sample.width,
                'points': sample.width * sample.height, 'point_step': sample.point_step,
                'row_step': sample.row_step, 'data_bytes': len(sample.data),
                'layout_consistent': len(sample.data) == sample.row_step * sample.height
                    and sample.row_step >= sample.width * sample.point_step}
    if kind == 'imu':
        return {'header': asdict(sample.header), 'orientation': asdict(sample.orientation),
                'yaw_rad': yaw_from_quaternion(sample.orientation),
                'angular_velocity': asdict(sample.angular_velocity),
                'linear_acceleration': asdict(sample.linear_acceleration)}
    if kind == 'range':
        return {'header': asdict(sample.header), 'point': asdict(sample.point)}
    raise ValueError('unsupported telemetry kind')


def trace_config(base_xml, trace_dir):
    root = ET.fromstring(base_xml)
    domain = root.find('Domain')
    tracing = domain.find('Tracing')
    if tracing is None:
        tracing = ET.SubElement(domain, 'Tracing')
    for child in list(tracing):
        tracing.remove(child)
    # This local CycloneDDS 0.10.2 build aborts when any text trace category
    # is enabled. Keep text tracing off; packet capture supplies DATA and
    # DATA_FRAG evidence without the broken configuration printer.
    ET.SubElement(tracing, 'Verbosity').text = 'none'
    ET.SubElement(tracing, 'PacketCaptureFile').text = str(trace_dir / 'cyclonedds.pcap')
    return ET.tostring(root, encoding='unicode')


def status_total(status):
    return int(getattr(status, 'total_count', 0))


def update_drift(item, payload):
    position = payload.get('position')
    yaw = payload.get('yaw_rad')
    if not isinstance(position, (list, tuple, dict)) or not isinstance(yaw, (int, float)):
        return
    xyz = list(position.values()) if isinstance(position, dict) else list(position)
    if len(xyz) < 3 or not all(math.isfinite(float(v)) for v in xyz[:3]) or not math.isfinite(yaw):
        return
    value = [float(v) for v in xyz[:3]]
    if item['first_position'] is None:
        item['first_position'], item['first_yaw_rad'] = value, yaw
    item['last_position'], item['last_yaw_rad'] = value, yaw
    item['max_translation_from_first_m'] = max(
        item['max_translation_from_first_m'], math.dist(item['first_position'], value))
    angle = math.atan2(math.sin(yaw - item['first_yaw_rad']),
                       math.cos(yaw - item['first_yaw_rad']))
    item['max_yaw_change_from_first_rad'] = max(item['max_yaw_change_from_first_rad'], abs(angle))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--discovery-seconds', type=float, default=20)
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--trace-dir', type=Path)
    args = parser.parse_args()
    if not 0 < args.discovery_seconds <= 60 or not 0 < args.seconds <= 60:
        parser.error('discovery and observation seconds must be in (0, 60]')
    root = Path(__file__).resolve().parents[1]
    config = (root / 'config/g1-readonly-dds.xml').read_text()
    if args.trace_dir is not None:
        trace_dir = args.trace_dir.resolve()
        runtime = (root / '.runtime').resolve()
        if not trace_dir.is_relative_to(runtime):
            parser.error('--trace-dir must be under the repository .runtime directory')
        trace_dir.mkdir(parents=True, exist_ok=False)
        config = trace_config(config, trace_dir)
    os.environ['CYCLONEDDS_URI'] = config

    from cyclonedds.builtin import (BuiltinDataReader, BuiltinTopicDcpsPublication,
                                    BuiltinTopicDcpsSubscription)
    from cyclonedds.core import Listener
    from cyclonedds.domain import Domain, DomainParticipant
    from cyclonedds.internal import InvalidSample
    from cyclonedds.qos import Policy, Qos
    from cyclonedds.sub import DataReader
    from cyclonedds.topic import Topic
    from g1_bottle_reaction.adapters.g1_robot import UnitreeSdkRuntime

    domain = Domain(0, config)
    participant = DomainParticipant(0)
    own_guid = str(participant.guid)
    emit = lambda value: print(json.dumps(value, allow_nan=False), flush=True)
    started_mono, started_unix = time.monotonic(), time.time_ns()
    emit({'event': 'start', 'participant': own_guid, 'interface': 'enp129s0', 'domain': 0,
          'started_unix_ns': started_unix, 'discovery_seconds': args.discovery_seconds,
          'observation_seconds': args.seconds, 'application_writers': 0, 'rpc_clients': 0})
    builtin = {'publication': BuiltinDataReader(participant, BuiltinTopicDcpsPublication),
               'subscription': BuiltinDataReader(participant, BuiltinTopicDcpsSubscription)}
    endpoints = {}

    def drain_discovery(emit_new):
        for direction, reader in builtin.items():
            for sample in reader.take(200):
                if isinstance(sample, InvalidSample) or str(sample.participant_key) == own_guid:
                    continue
                key = (direction, str(sample.key))
                if key in endpoints:
                    continue
                row = {'direction': direction, 'endpoint': str(sample.key),
                       'participant': str(sample.participant_key), 'topic': sample.topic_name,
                       'type': sample.type_name, 'qos': str(sample.qos),
                       'discovered_after_start_s': time.monotonic() - started_mono}
                endpoints[key] = row
                if emit_new and (sample.topic_name in TARGETS or
                                 sample.topic_name.startswith('rt/utlidar/')):
                    emit({'event': 'endpoint', **row})

    discovery_deadline = time.monotonic() + args.discovery_seconds
    while time.monotonic() < discovery_deadline:
        drain_discovery(True)
        time.sleep(0.02)
    drain_discovery(True)

    schemas = UnitreeSdkRuntime().load_readonly_navigation_probe_types()
    readers, topics, listeners, stats = {}, {}, {}, {}
    for name, type_map in TARGETS.items():
        rows = [row for row in endpoints.values() if row['topic'] == name]
        publications = [row for row in rows if row['direction'] == 'publication']
        observed = sorted({row['type'] for row in rows})
        published = sorted({row['type'] for row in publications})
        candidates = [value for value in published if value in type_map]
        if not candidates:
            candidates = [value for value in observed if value in type_map]
        item = {'discovered': bool(rows), 'observed_types': observed,
                'published_types': published, 'writer_endpoints':
                    [row['endpoint'] for row in publications],
                'reader_created': False, 'selected_type': None, 'kind': None,
                'matched_current': 0, 'matched_total': 0, 'match_events': [],
                'samples': 0, 'invalid_samples': 0, 'deserialize_errors': 0,
                'deserialize_error_messages': [], 'sample_lost': 0,
                'sample_rejected': 0, 'incompatible_qos': 0,
                'reader_created_unix_ns': None, 'first_match_unix_ns': None,
                'first_match_after_reader_s': None,
                'first_sample_unix_ns': None, 'last_sample_unix_ns': None,
                'first_sample_after_reader_s': None, 'first_sample_after_match_s': None,
                'last_sample': None, 'first_position': None, 'last_position': None,
                'first_yaw_rad': None, 'last_yaw_rad': None,
                'max_translation_from_first_m': 0.0,
                'max_yaw_change_from_first_rad': 0.0}
        stats[name] = item
        if len(candidates) != 1:
            item['reader_skip_reason'] = ('no supported live type' if not candidates
                                           else 'multiple supported live publication types')
            continue
        selected = candidates[0]
        kind = type_map[selected]
        schema = schemas[kind]
        if schema.__idl_typename__.replace('.', '::') != selected:
            item['reader_skip_reason'] = 'local schema type name differs from live discovery'
            continue
        item.update(reader_created=True, selected_type=selected, kind=kind,
                    reader_created_unix_ns=time.time_ns())

        def matched(reader, status, item=item):
            now = time.time_ns()
            item['matched_current'], item['matched_total'] = status.current_count, status.total_count
            if status.current_count > 0 and item['first_match_unix_ns'] is None:
                item['first_match_unix_ns'] = now
                item['first_match_after_reader_s'] = (
                    now - item['reader_created_unix_ns']) / 1e9
            item['match_events'].append({'unix_ns': now, 'current': status.current_count,
                                         'total': status.total_count})
        def incompatible(reader, status, item=item):
            item['incompatible_qos'] = status_total(status)
        def lost(reader, status, item=item):
            item['sample_lost'] = status_total(status)
        def rejected(reader, status, item=item):
            item['sample_rejected'] = status_total(status)
        listeners[name] = Listener(on_subscription_matched=matched,
                                   on_requested_incompatible_qos=incompatible,
                                   on_sample_lost=lost, on_sample_rejected=rejected)
        topics[name] = Topic(participant, name, schema)
        depth = 1 if kind == 'cloud' else 32
        readers[name] = DataReader(participant, topics[name],
            Qos(Policy.Reliability.BestEffort, Policy.Durability.Volatile,
                Policy.History.KeepLast(depth)), listener=listeners[name])

    observed_started_mono, observed_started_unix = time.monotonic(), time.time_ns()
    next_print = {}
    deadline = observed_started_mono + args.seconds
    while time.monotonic() < deadline:
        drain_discovery(True)
        for name, reader in readers.items():
            item = stats[name]
            try:
                samples = reader.take(100)
            except Exception as exc:
                item['deserialize_errors'] += 1
                message = type(exc).__name__ + ': ' + str(exc)
                if message not in item['deserialize_error_messages'] and len(item['deserialize_error_messages']) < 8:
                    item['deserialize_error_messages'].append(message)
                    emit({'event': 'reader_error', 'topic': name, 'error': message})
                continue
            for sample in samples:
                if isinstance(sample, InvalidSample):
                    item['invalid_samples'] += 1
                    continue
                now = time.time_ns()
                payload = summarize(item['kind'], sample)
                item['samples'] += 1
                item['last_sample_unix_ns'] = now
                item['last_sample'] = payload
                update_drift(item, payload)
                if item['first_sample_unix_ns'] is None:
                    item['first_sample_unix_ns'] = now
                    item['first_sample_after_reader_s'] = (now - item['reader_created_unix_ns']) / 1e9
                    if item['first_match_unix_ns'] is not None:
                        item['first_sample_after_match_s'] = (now - item['first_match_unix_ns']) / 1e9
                if time.monotonic() >= next_print.get(name, 0):
                    emit({'event': 'sample', 'topic': name, 'sample_number': item['samples'],
                          'received_unix_ns': now, 'payload': payload})
                    next_print[name] = time.monotonic() + 5
        time.sleep(0.01)
    elapsed = time.monotonic() - observed_started_mono
    drain_discovery(True)
    for item in stats.values():
        item['observed_rate_hz'] = item['samples'] / elapsed
    lidar_endpoints = [row for row in endpoints.values()
                       if row['topic'].startswith('rt/utlidar/')]
    switch_endpoints = [row for row in lidar_endpoints if row['topic'] == LIDAR_SWITCH]
    emit({'event': 'summary', 'observation_started_unix_ns': observed_started_unix,
          'observation_elapsed_s': elapsed, 'topics': stats,
          'utlidar_endpoints': lidar_endpoints, 'utlidar_switch_endpoints': switch_endpoints,
          'application_writers': 0, 'rpc_clients': 0,
          'write_rpc_motion_commands_sent': 'NONE'})
    return 0 if any(item['samples'] for item in stats.values()) else 2


if __name__ == '__main__':
    raise SystemExit(main())
