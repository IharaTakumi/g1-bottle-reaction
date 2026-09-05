"""Offline commissioning tests. No SDK, DDS participant or robot required."""
import importlib.util
import json
from pathlib import Path
import time

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('slam_session', ROOT / 'scripts/g1-slam-session.py')
slam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slam)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(slam, 'ROOT', tmp_path)
    monkeypatch.setattr(slam, 'boot_id', lambda: 'offline-boot')
    policy = yaml.safe_load((ROOT / 'config/g1-slam-session.yaml').read_text())
    session = slam.initialize(tmp_path / '.runtime', policy)
    now = time.monotonic_ns()
    samples = []
    for i in range(31):
        samples.append({'received_ns': now - (30-i)*100_000_000,
                        'payload': {'header': {'frame_id': 'map', 'stamp': {'sec': 20, 'nanosec': i*10_000_000}},
                                    'child_frame_id': 'base_link', 'pose': {'pose': {
                                        'position': {'x': 1., 'y': 2., 'z': .3},
                                        'orientation': {'x': 0., 'y': 0., 'z': 0., 'w': 1.}}}}})
    slam.write(session / 'telemetry.json', {'received_ns': now, 'session_id': session.name,
               'boot_id': 'offline-boot', 'mapping': samples, 'relocation': [], 'info': {}})
    return session, policy, samples, now


def start_result(session, now):
    slam.write(session / 'start.result.json', {'rpc_code': 0,
        'response': {'succeed': True, 'errorCode': 0}, 'finished_ns': now - 4_000_000_000})


def test_pose_preserves_xyz_quaternion_without_tf(setup):
    _, policy, samples, now = setup
    seed = slam.stable_pose(samples, 0, now, policy)
    assert seed['pose'] == dict(zip(slam.KEYS, [1., 2., .3, 0., 0., 0., 1.]))
    negative = dict(seed['pose'], q_w=-1.)
    assert slam.distance(seed['pose'], negative, policy) == (0, 0)


@pytest.mark.parametrize('fault', ['frame', 'quaternion', 'stale', 'duplicate_stamp', 'moving', 'pre_start'])
def test_invalid_seed_is_rejected(setup, fault):
    _, policy, samples, now = setup
    after = 0
    if fault == 'frame':
        samples[-1]['payload']['child_frame_id'] = 'livox_frame'
    elif fault == 'quaternion':
        samples[-1]['payload']['pose']['pose']['orientation']['w'] = float('nan')
    elif fault == 'stale':
        now += 2_000_000_000
    elif fault == 'duplicate_stamp':
        samples[-1]['payload']['header']['stamp'] = samples[-2]['payload']['header']['stamp']
    elif fault == 'moving':
        samples[-1]['payload']['pose']['pose']['position']['x'] += 1
    else:
        after = now
    with pytest.raises(ValueError):
        slam.stable_pose(samples, after, now, policy)


def test_preview_and_opt_in_do_not_construct_client(setup):
    session, _, _, _ = setup
    def forbidden(*args):
        pytest.fail('SDK factory must not run')
    assert slam.command(session, 'start', False, False, factory=forbidden) == 0
    assert not (session / 'start.attempt.json').exists()
    with pytest.raises(ValueError):
        slam.command(session, 'start', True, False, factory=forbidden)


@pytest.mark.skipif(__import__('sys').platform == 'win32', reason='Ubuntu command lock uses flock')
@pytest.mark.parametrize('code,raw', [(0, '{"succeed":true,"errorCode":0}'), (3104, ''),
                                    (0, '{"succeed":false,"errorCode":507}'), (0, 'invalid')])
def test_exactly_one_call_and_no_replay_even_on_failure(setup, code, raw):
    session, _, _, _ = setup
    calls = []
    class Client:
        def _Call(self, api, parameter):
            calls.append((api, json.loads(parameter)))
            return code, raw
    slam.command(session, 'start', True, True, factory=lambda *args: Client())
    assert calls == [(1801, {'data': {'slam_type': 'indoor'}})]
    with pytest.raises(ValueError, match='already attempted'):
        slam.command(session, 'start', True, True, factory=lambda *args: Client())
    assert len(calls) == 1


@pytest.mark.skipif(__import__('sys').platform == 'win32', reason='Ubuntu command lock uses flock')
def test_save_freezes_pose_and_relocation_reuses_same_map(setup):
    session, _, _, now = setup
    start_result(session, now)
    calls = []
    class Client:
        def _Call(self, api, parameter):
            calls.append((api, json.loads(parameter)))
            return 0, '{"succeed":true,"errorCode":0}'
    factory = lambda *args: Client()
    assert slam.command(session, 'save', True, True, stationary=True, factory=factory) == 0
    assert [c[0] for c in calls] == [1802]  # No chained relocation.
    with pytest.raises(ValueError, match='accept-mapping-pose-seed'):
        slam.command(session, 'relocate', True, True, stationary=True, factory=factory)
    assert slam.command(session, 'relocate', True, True, stationary=True, accept_seed=True, factory=factory) == 0
    assert [c[0] for c in calls] == [1802, 1804]
    assert calls[0][1]['data']['address'] == calls[1][1]['data']['address']
    assert calls[1][1]['data']['x'] == 1.
    assert 'test1.pcd' not in calls[0][1]['data']['address']


def test_save_requires_successful_start_and_stationary_confirmation(setup):
    session, _, _, _ = setup
    with pytest.raises(ValueError, match='stationary'):
        slam.prepare(session, 'save')
    with pytest.raises(FileNotFoundError):
        slam.prepare(session, 'save', stationary=True)


def test_unique_map_per_session(setup):
    session, policy, _, _ = setup
    other = slam.initialize(session.parent, policy)
    assert slam.read(session / 'session.json')['address'] != slam.read(other / 'session.json')['address']


def test_desktop_session_is_rejected(setup):
    session, _, _, _ = setup
    meta = slam.read(session / 'session.json')
    assert meta['runtime_host'] == '192.168.123.164'
    assert meta['pcd_host'] == '192.168.123.161'
    meta['policy']['interface'] = 'enp129s0'
    slam.write(session / 'session.json', meta)
    with pytest.raises(ValueError, match='Desktop sessions'):
        slam.prepare(session, 'start')


def test_rpc_success_alone_never_passes_localization_check(setup):
    session, _, _, now = setup
    slam.write(session / 'relocate.result.json', {'rpc_code': 0,
        'response': {'succeed': True, 'errorCode': 0}, 'sent_ns': now})
    slam.write(session / 'relocate.attempt.json', {})
    with pytest.raises(ValueError):
        slam.check(session)


@pytest.mark.parametrize('fault', [None, 'wrong_map', 'pose_mismatch', 'frozen_pos_info', 'slam_error'])
def test_localization_requires_consistent_live_evidence(setup, fault):
    session, policy, samples, now = setup
    pose = slam.odom_pose(samples[-1]['payload'], policy)
    slam.write(session / 'relocate.result.json', {'rpc_code': 0,
        'response': {'succeed': True, 'errorCode': 0}, 'sent_ns': now - 4_000_000_000})
    slam.write(session / 'relocate.attempt.json', {'seed': {'pose': pose}})
    address = slam.read(session / 'session.json')['address']
    history = [{'received_ns': now - (1-i)*100_000_000, 'payload': {
        'type': 'pos_info', 'errorCode': 0, 'sec': 25, 'nanosec': i*100_000_000,
        'data': {'address': address, 'currentPose': dict(pose)}}} for i in range(2)]
    latest = history[-1]
    if fault == 'wrong_map':
        latest['payload']['data']['address'] = '/home/unitree/test1.pcd'
    if fault == 'pose_mismatch':
        latest['payload']['data']['currentPose']['x'] += 2
    if fault == 'frozen_pos_info':
        latest['payload']['nanosec'] = 0
    if fault == 'slam_error':
        latest['payload']['errorCode'] = 507
    data = slam.read(session / 'telemetry.json')
    data.update(relocation=samples, pos_history=history, info={slam.INFO + ':pos_info': latest})
    slam.write(session / 'telemetry.json', data)
    if fault:
        with pytest.raises(ValueError):
            slam.check(session)
    else:
        slam.check(session)
