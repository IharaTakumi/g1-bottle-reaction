#!/usr/bin/env python3
"""PC2 READ ONLY import checks; optional bounded subscribers. No RPC or file writes."""
import argparse
import importlib
from importlib import metadata
import json
from pathlib import Path
import platform
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot_side.adapters.g1_robot import DDS_XML, UnitreeSdkRuntime, configure_sdk_path, require_pc2


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
        report['schema_types'] = [t.__idl_typename__ for t in (low, odom, string)]
    except Exception as exc:
        report['missing'].append({'sdk_schemas': str(exc)})
    print(json.dumps(report, indent=2), flush=True)
    if report['missing']:
        return 2
    if not args.subscribe:
        return 0
    require_pc2()
    from cyclonedds.domain import Domain, DomainParticipant
    from cyclonedds.sub import DataReader
    from cyclonedds.topic import Topic
    from cyclonedds.qos import Qos, Policy
    from cyclonedds.internal import InvalidSample
    domain = Domain(0, DDS_XML)
    participant = DomainParticipant(0)
    topics = {'rt/lowstate': low, 'rt/unitree/slam_mapping/odom': odom,
              'rt/unitree/slam_relocation/odom': odom, 'rt/slam_info': string, 'rt/slam_key_info': string}
    readers = {name: DataReader(participant, Topic(participant, name, typ),
               Qos(Policy.Reliability.BestEffort, Policy.Durability.Volatile, Policy.History.KeepLast(10)))
               for name, typ in topics.items()}
    counts = {name: {'samples': 0, 'matched': 0} for name in topics}
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        for name, reader in readers.items():
            counts[name]['matched'] = reader.get_subscription_matched_status().current_count
            for sample in reader.take(100):
                if isinstance(sample, InvalidSample):
                    continue
                item = counts[name]
                item['samples'] += 1
                if hasattr(sample, 'header'):
                    item['frame_id'] = sample.header.frame_id
                    item['child_frame_id'] = sample.child_frame_id
                    item['source_stamp'] = [sample.header.stamp.sec, sample.header.stamp.nanosec]
                elif hasattr(sample, 'tick'):
                    item['tick'] = sample.tick
        time.sleep(0.02)
    print(json.dumps({'topic_results': counts,
          'note': 'Zero samples is unverified, not proof of no map or missing dependency.'}, indent=2))
    del readers, participant, domain
    return 0 if counts['rt/lowstate']['samples'] else 3


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        print('READ ONLY diagnostic failed:', exc, file=sys.stderr)
        raise SystemExit(2)
