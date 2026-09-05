#!/usr/bin/env python3
"""Bounded DDS discovery / state subscription. No writers, clients or RPCs.

Use a fresh process per invocation. DDS discovery/acknowledgements are network
traffic, but no application command topics are published.
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['discovery', 'lowstate', 'slam'], default='discovery')
    parser.add_argument('--seconds', type=float, default=10)
    args = parser.parse_args()
    if not 0 < args.seconds <= 60:
        parser.error('--seconds must be greater than 0 and at most 60')
    root = Path(__file__).resolve().parents[1]
    # Override inherited DDS settings; this diagnostic uses only enp129s0/domain 0.
    config = (root / 'config/g1-readonly-dds.xml').read_text()
    os.environ['CYCLONEDDS_URI'] = config
    from cyclonedds.domain import Domain, DomainParticipant
    from cyclonedds.builtin import BuiltinDataReader, BuiltinTopicDcpsPublication, BuiltinTopicDcpsSubscription
    from cyclonedds.internal import InvalidSample

    domain = Domain(0, config)
    participant = DomainParticipant(0)
    own_guid = str(participant.guid)
    print(json.dumps({'mode': args.mode, 'interface': 'enp129s0', 'domain': 0, 'participant': own_guid}), flush=True)
    if args.mode == 'discovery':
        readers = [('publication', BuiltinDataReader(participant, BuiltinTopicDcpsPublication)),
                   ('subscription', BuiltinDataReader(participant, BuiltinTopicDcpsSubscription))]
        seen = set()
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            for kind, reader in readers:
                for sample in reader.take(100):
                    if isinstance(sample, InvalidSample) or str(sample.participant_key) == own_guid:
                        continue
                    key = (kind, str(sample.key))
                    if key in seen:
                        continue
                    seen.add(key)
                    print(json.dumps({'kind': kind, 'topic': sample.topic_name, 'type': sample.type_name,
                                      'participant': str(sample.participant_key), 'endpoint': str(sample.key)}), flush=True)
            time.sleep(0.05)
        print(json.dumps({'remote_endpoints': len(seen)}), flush=True)
        return 0 if seen else 2

    from cyclonedds.sub import DataReader
    from cyclonedds.topic import Topic
    from cyclonedds.qos import Qos, Policy
    from g1_bottle_reaction.adapters.g1_robot import UnitreeSdkRuntime
    if args.mode == 'slam':
        odometry, string = UnitreeSdkRuntime().load_readonly_slam_types()
        schemas = {'rt/slam_info': string, 'rt/slam_key_info': string,
                   'rt/unitree/slam_mapping/odom': odometry,
                   'rt/unitree/slam_relocation/odom': odometry}
        topics = {name: Topic(participant, name, schema) for name, schema in schemas.items()}
        readers = {name: DataReader(participant, topic, Qos(Policy.Reliability.BestEffort,
                   Policy.Durability.Volatile, Policy.History.KeepLast(1))) for name, topic in topics.items()}
        counts = dict.fromkeys(schemas, 0)
        next_prints = dict.fromkeys(schemas, 0.0)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            for name, reader in readers.items():
                for sample in reader.take(10):
                    if isinstance(sample, InvalidSample):
                        continue
                    counts[name] += 1
                    if time.monotonic() >= next_prints[name]:
                        payload = {'data': sample.data[:16384]} if hasattr(sample, 'data') else asdict(sample)
                        print(json.dumps({'topic': name, 'sample': payload}), flush=True)
                        next_prints[name] = time.monotonic() + 1
            time.sleep(0.01)
        print(json.dumps({'samples_by_topic': counts}), flush=True)
        return 0 if any(counts.values()) else 2
    state_type = UnitreeSdkRuntime().load_readonly_low_state_type()
    topic = Topic(participant, 'rt/lowstate', state_type)
    reader = DataReader(participant, topic, Qos(Policy.Reliability.BestEffort, Policy.Durability.Volatile,
                                              Policy.History.KeepLast(1)))
    deadline = time.monotonic() + args.seconds
    count = 0
    ticks = set()
    next_print = 0.0
    while time.monotonic() < deadline:
        for sample in reader.take(10):
            if isinstance(sample, InvalidSample):
                continue
            count += 1
            ticks.add(sample.tick)
            if time.monotonic() >= next_print:
                print(json.dumps({'topic': 'rt/lowstate', 'tick': sample.tick,
                                  'mode_machine': sample.mode_machine,
                                  'imu_quaternion': list(sample.imu_state.quaternion),
                                  'imu_rpy': list(sample.imu_state.rpy),
                                  'imu_gyroscope': list(sample.imu_state.gyroscope),
                                  'imu_accelerometer': list(sample.imu_state.accelerometer),
                                  'joint_q': [s.q for s in sample.motor_state],
                                  'joint_dq': [s.dq for s in sample.motor_state],
                                  'motor_slots': len(sample.motor_state)}), flush=True)
                next_print = time.monotonic() + 1
        time.sleep(0.01)
    print(json.dumps({'samples': count, 'distinct_ticks': len(ticks)}), flush=True)
    return 0 if len(ticks) > 1 else 2


if __name__ == '__main__':
    raise SystemExit(main())
