#!/usr/bin/env python3
"""Separate local recorder and explicit single-call SLAM commands. No navigation."""
import argparse
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
import uuid
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot_side.adapters.g1_robot import DDS_XML, require_runtime

import yaml

ROOT = Path(__file__).resolve().parents[1]
MAPPING = 'rt/unitree/slam_mapping/odom'
RELOCATION = 'rt/unitree/slam_relocation/odom'
INFO = 'rt/slam_info'
KEY_INFO = 'rt/slam_key_info'
STATIC_ZERO = 'relocate-static-zero'
IDS = {'start': 1801, 'save': 1802, 'relocate': 1804, STATIC_ZERO: 1804}
ZERO_POSE = {'x': 0.0, 'y': 0.0, 'z': 0.0,
             'q_x': 0.0, 'q_y': 0.0, 'q_z': 0.0, 'q_w': 1.0}
KEYS = ('x', 'y', 'z', 'q_x', 'q_y', 'q_z', 'q_w')


def read(path):
    return json.loads(path.read_text())


def write(path, data, exclusive=False):
    encoded = json.dumps(data, indent=2, allow_nan=False) + '\n'
    if exclusive:
        with path.open('x') as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
    else:
        tmp = path.with_suffix('.tmp')
        with tmp.open('w') as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def pose_values(pose, policy):
    values = [pose[k] for k in KEYS]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in values):
        raise ValueError('Nonfinite or nonnumeric pose')
    norm = math.sqrt(sum(v*v for v in values[3:]))
    if abs(norm - 1) > policy['quaternion_norm_tolerance']:
        raise ValueError('Invalid quaternion norm')
    return values


def distance(a, b, policy):
    a, b = pose_values(a, policy), pose_values(b, policy)
    translation = math.dist(a[:3], b[:3])
    dot = abs(sum(x*y for x, y in zip(a[3:], b[3:])))
    dot /= math.sqrt(sum(x*x for x in a[3:]) * sum(x*x for x in b[3:]))
    return translation, 2 * math.acos(min(1., dot))


def odom_pose(payload, policy):
    if (payload['header']['frame_id'] != policy['expected_frame'] or
            payload['child_frame_id'] != policy['expected_child_frame']):
        raise ValueError('Unexpected odometry frames; do not guess a transform')
    p = payload['pose']['pose']
    pose = dict(zip(KEYS, [p['position'][k] for k in ('x', 'y', 'z')] +
                    [p['orientation'][k] for k in ('x', 'y', 'z', 'w')]))
    pose_values(pose, policy)
    return pose


def stamp(sample):
    p = sample['payload']
    s = p.get('header', {}).get('stamp', p)
    sec, ns = s['sec'], s['nanosec']
    if not isinstance(sec, int) or not isinstance(ns, int) or sec < 0 or not 0 <= ns < 10**9:
        raise ValueError('Invalid source timestamp')
    value = sec * 10**9 + ns
    if value <= 0:
        raise ValueError('Zero source timestamp')
    return value


def fresh(sample, now, policy):
    age = (now - sample['received_ns']) / 1e9
    if not 0 <= age <= policy['telemetry_max_age_s']:
        raise ValueError('Telemetry is stale or from another clock epoch')


def stable_pose(samples, after_ns, now, policy):
    samples = [s for s in samples if s['received_ns'] > after_ns]
    if len(samples) < policy['stable_min_samples']:
        raise ValueError('Not enough fresh post-start odometry samples')
    fresh(samples[-1], now, policy)
    if (samples[-1]['received_ns'] - samples[0]['received_ns']) / 1e9 < policy['stable_window_s']:
        raise ValueError('Stationary observation window is too short')
    stamps = [stamp(s) for s in samples]
    if any(b <= a for a, b in zip(stamps, stamps[1:])):
        raise ValueError('Source timestamps do not advance')
    poses = [odom_pose(s['payload'], policy) for s in samples]
    for p in poses:
        dt, dr = distance(p, poses[-1], policy)
        if dt > policy['stable_translation_m'] or dr > policy['stable_rotation_rad']:
            raise ValueError('Robot pose is not stable; stand still before saving')
    return {'pose': poses[-1], 'sample': samples[-1],
            'contract': 'mapping odom used unchanged as same-map initial estimate; firmware unverified'}


def snapshot(session, policy):
    data = read(session / 'telemetry.json')
    if data['session_id'] != session.name or data['boot_id'] != boot_id():
        raise ValueError('Recorder session/boot mismatch')
    fresh(data, time.monotonic_ns(), policy)
    return data


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def initialize(base, policy):
    if policy['interface'] != 'eth0':
        raise ValueError('Robot-side runtime requires eth0')
    sid = 'g1_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex
    session = base / sid
    session.mkdir(parents=True, exist_ok=False)
    write(session / 'session.json', {'session_id': sid, 'boot_id': boot_id(),
          'address': '/home/unitree/' + sid + '.pcd', 'pcd_host': '192.168.123.161',
          'runtime_host': '192.168.123.164', 'policy': policy}, exclusive=True)
    print(session.resolve())
    return session


def success(result):
    r = result.get('response')
    return (result.get('rpc_code') == 0 and isinstance(r, dict) and
            r.get('succeed') is True and r.get('errorCode') == 0)


def completed(session, action):
    result = read(session / (action + '.result.json'))
    if not success(result):
        raise ValueError(action + ' has no confirmed successful RPC result')
    return result


def prepare(session, action, stationary=False, accept_seed=False, accept_static_zero=False):
    meta = read(session / 'session.json')
    policy = meta['policy']
    if policy['interface'] != 'eth0':
        raise ValueError('Desktop sessions are unsupported; initialize a new PC2 session')
    if meta['boot_id'] != boot_id() or meta['session_id'] != session.name:
        raise ValueError('Session must remain on the same host boot')
    # No user-chosen filenames or reused test1.pcd. The UUID path is fixed at init.
    if meta['address'] != '/home/unitree/' + session.name + '.pcd':
        raise ValueError('Map path differs from this session')
    attempted_actions = (('relocate', STATIC_ZERO) if action in ('relocate', STATIC_ZERO)
                         else (action,))
    if any((session / (name + '.attempt.json')).exists() for name in attempted_actions):
        raise ValueError('Operation already attempted; no automatic or repeated call allowed')
    data = snapshot(session, policy)
    parameter = {'data': {}}
    seed = None
    if action == 'start':
        parameter['data'] = {'slam_type': 'indoor'}
    else:
        if not stationary:
            raise ValueError('--confirm-stationary is required (remain still from BEFORE save)')
        parameter['data']['address'] = meta['address']
        if action == 'save':
            started = completed(session, 'start')
            seed = stable_pose(data['mapping'], started['finished_ns'], time.monotonic_ns(), policy)
        elif action == 'relocate':
            completed(session, 'save')
            if not accept_seed:
                raise ValueError('--accept-mapping-pose-seed is required; see pose contract documentation')
            saved = read(session / 'save.attempt.json')
            seed = saved['seed']
            if not 0 <= (time.monotonic_ns() - seed['sample']['received_ns']) / 1e9 <= policy['max_seed_age_s']:
                raise ValueError('Frozen pose is too old; relocation not sent')
            pose_values(seed['pose'], policy)
            parameter['data'].update(seed['pose'])
        else:
            completed(session, 'save')
            if not accept_static_zero:
                raise ValueError('--accept-static-same-pose-zero is required for this dedicated test')
            seed = {'pose': dict(ZERO_POSE), 'sample': None,
                    'contract': 'Unitree example origin pose; static same-pose test only'}
            parameter['data'].update(ZERO_POSE)
    return meta, parameter, seed


def command(session, action, execute, enable, stationary=False, accept_seed=False,
            accept_static_zero=False, factory=None):
    if execute and not enable:
        raise ValueError('--execute also requires --enable-real-robot')
    meta, parameter, seed = prepare(session, action, stationary, accept_seed,
                                    accept_static_zero)
    print(json.dumps({'api_id': IDS[action], 'parameter': parameter, 'preview': not execute}, indent=2))
    if not execute:
        return 0
    if factory is None:
        require_runtime()
    # One lock across sessions prevents concurrent command processes on this host.
    import fcntl
    with (ROOT / '.runtime/g1-slam-command.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        meta, parameter, seed = prepare(session, action, stationary, accept_seed,
                                        accept_static_zero)
        # Persist intent BEFORE any SDK construction, including uncertain failure/crash.
        attempt = {'api_id': IDS[action], 'parameter': parameter, 'seed': seed,
                   'created_ns': time.monotonic_ns(), 'session_id': session.name}
        write(session / (action + '.attempt.json'), attempt, exclusive=True)
        result = {'rpc_code': None, 'response': None}
        try:
            print('REAL G1: one SLAM state-changing RPC; no navigation, retry or cleanup RPC.', flush=True)
            if factory is None:
                from robot_side.adapters.g1_robot import UnitreeSdkRuntime
                factory = UnitreeSdkRuntime().create_slam_single_call_client
            client = factory(meta['policy']['interface'], meta['policy']['rpc_timeout_s'], IDS[action])
            if action == 'save':
                # SDK setup can take time: capture the final stable pose immediately before 1802.
                data = snapshot(session, meta['policy'])
                seed = stable_pose(data['mapping'], completed(session, 'start')['finished_ns'],
                                   time.monotonic_ns(), meta['policy'])
                attempt['seed'] = seed
                write(session / (action + '.attempt.json'), attempt)
            else:
                snapshot(session, meta['policy'])
                if action == 'relocate' and (time.monotonic_ns() - seed['sample']['received_ns']) / 1e9 > meta['policy']['max_seed_age_s']:
                    raise ValueError('Frozen pose expired during SDK setup; not sent')
            result['sent_ns'] = time.monotonic_ns()
            code, raw = client._Call(IDS[action], json.dumps(parameter, allow_nan=False))
            result.update(rpc_code=code, raw=raw)
            result['response'] = json.loads(raw) if raw else None
        except Exception as exc:
            result['error'] = str(exc)
        result['finished_ns'] = time.monotonic_ns()
        write(session / (action + '.result.json'), result, exclusive=True)
        print(json.dumps(result, indent=2))
        return 0 if success(result) else 2


def record(session):
    """Readers only. Writes telemetry locally on PC2, never the server PCD."""
    require_runtime()
    meta = read(session / 'session.json')
    policy = meta['policy']
    if policy['interface'] != 'eth0':
        raise ValueError('Desktop sessions are unsupported; initialize a new PC2 session')
    from cyclonedds.domain import Domain, DomainParticipant
    from cyclonedds.internal import InvalidSample
    from cyclonedds.sub import DataReader
    from cyclonedds.topic import Topic
    from cyclonedds.qos import Qos, Policy
    from robot_side.adapters.g1_robot import UnitreeSdkRuntime
    # Exclusive file prevents two recorders or a restarted recorder hiding a discontinuity.
    with (session / 'telemetry.jsonl').open('x') as log:
        config = DDS_XML
        domain = Domain(0, config)
        participant = DomainParticipant(0)
        odom, string = UnitreeSdkRuntime().load_readonly_slam_types()
        readers = {name: DataReader(participant, Topic(participant, name, schema),
                   Qos(Policy.Reliability.BestEffort, Policy.Durability.Volatile, Policy.History.KeepLast(100)))
                   for name, schema in ((MAPPING, odom), (RELOCATION, odom), (INFO, string), (KEY_INFO, string))}
        data = {'session_id': session.name, 'boot_id': boot_id(), 'mapping': [], 'relocation': [], 'info': {}, 'pos_history': []}
        pos_history = deque(maxlen=100)
        queues = {MAPPING: deque(), RELOCATION: deque()}
        print('READ ONLY subscribers created. Ctrl-C ends recording only.', flush=True)
        try:
            while True:
                for name, reader in readers.items():
                    for msg in reader.take(100):
                        if isinstance(msg, InvalidSample):
                            continue
                        try:
                            payload = asdict(msg) if name in queues else json.loads(msg.data)
                            item = {'topic': name, 'received_ns': time.monotonic_ns(),
                                    'received_unix_ns': time.time_ns(), 'payload': payload}
                            log.write(json.dumps(item, allow_nan=False) + '\n')
                        except (ValueError, TypeError):
                            continue
                        if name in queues:
                            q = queues[name]
                            q.append(item)
                            while len(q) > 1 and (item['received_ns'] - q[0]['received_ns']) / 1e9 > policy['stable_window_s'] + 1:
                                q.popleft()
                        elif isinstance(payload, dict):
                            category = name + ':' + str(payload.get('type'))
                            if category in data['info'] or len(data['info']) < 64:
                                data['info'][category] = item
                            if name == INFO and payload.get('type') == 'pos_info':
                                pos_history.append(item)
                data.update(mapping=list(queues[MAPPING]), relocation=list(queues[RELOCATION]),
                            received_ns=time.monotonic_ns(), pos_history=list(pos_history))
                log.flush()
                write(session / 'telemetry.json', data)
                time.sleep(0.05)
        except KeyboardInterrupt:
            print('Recorder stopped. No RPC sent.')
        # Keep domain alive for the lifetime of readers.
        del readers, participant, domain


def check(session):
    meta = read(session / 'session.json')
    policy = meta['policy']
    if policy['interface'] != 'eth0':
        raise ValueError('Desktop sessions are unsupported; initialize a new PC2 session')
    completed(session, 'relocate')
    attempt = read(session / 'relocate.attempt.json')
    data = snapshot(session, policy)
    after = read(session / 'relocate.result.json')['sent_ns']
    current = stable_pose(data['relocation'], after, time.monotonic_ns(), policy)
    pos = data['info'][INFO + ':pos_info']
    fresh(pos, time.monotonic_ns(), policy)
    if pos['received_ns'] <= after or pos['payload'].get('errorCode') != 0:
        raise ValueError('No successful post-relocation pos_info')
    stamp(pos)
    history = [s for s in data.get('pos_history', []) if s['received_ns'] > after]
    if len(history) < 2 or any(stamp(b) <= stamp(a) for a, b in zip(history, history[1:])):
        raise ValueError('pos_info source timestamps are not continuously advancing')
    info = pos['payload']['data']
    if info.get('address') != meta['address']:
        raise ValueError('pos_info does not identify the saved map path')
    for p in (info['currentPose'], attempt['seed']['pose']):
        dt, dr = distance(current['pose'], p, policy)
        if dt > policy['localization_translation_m'] or dr > policy['localization_rotation_rad']:
            raise ValueError('Relocation pose differs from pos_info or frozen seed')
    for item in data['info'].values():
        if item['received_ns'] > after and item['payload'].get('errorCode', 0) != 0:
            raise ValueError('Post-relocation SLAM error observed; inspect telemetry log')
    print(json.dumps({'localization_evidence': 'consistent; operator must verify physical position',
                      'address': meta['address'], 'pose': current['pose']}, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['init', 'record', 'start', 'save', 'relocate',
                                      STATIC_ZERO, 'check'])
    p.add_argument('--session', type=Path)
    p.add_argument('--execute', action='store_true', help='Send exactly one RPC (otherwise preview)')
    p.add_argument('--enable-real-robot', action='store_true')
    p.add_argument('--confirm-stationary', action='store_true')
    p.add_argument('--accept-mapping-pose-seed', action='store_true')
    p.add_argument('--accept-static-same-pose-zero', action='store_true')
    args = p.parse_args()
    try:
        if args.action == 'init':
            initialize(ROOT / '.runtime', yaml.safe_load((ROOT / 'config/g1-slam-session.yaml').read_text()))
        elif args.session is None:
            p.error('--session is required')
        elif args.action == 'record':
            record(args.session.resolve())
        elif args.action == 'check':
            check(args.session.resolve())
        else:
            return command(args.session.resolve(), args.action, args.execute, args.enable_real_robot,
                           args.confirm_stationary, args.accept_mapping_pose_seed,
                           args.accept_static_same_pose_zero)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('REFUSED:', exc)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
