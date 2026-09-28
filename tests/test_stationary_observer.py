"""All limits here are synthetic test values, not G1 calibration."""
from dataclasses import asdict, replace
import ast
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace as NS
import uuid

import pytest

from patrol.stationary_observer import EvidenceConfig, StationaryEvidenceObserver, IDENTITY
from patrol.telemetry_evidence import lowstate_evidence, odom_evidence


def config():
    return EvidenceConfig(.05, .1, .2, .3, 4, .2, .15, .15)


def context():
    return dict(**{k: str(uuid.uuid4()) for k in IDENTITY}, clock_id="synthetic-clock",
                commit_at=10., odom_stamp_ns=1000000000, imu_tick=1,
                stop_rpc_status="STOP_RPC_CONFIRMED", relay_state="MOVEMENT_HELD",
                owner_valid=True, control_fault=False)


def sample(ctx, n, **changes):
    stamp = 10.1 + .1 * n
    return dict(ctx, **dict(received_at=stamp, odom_source_at=stamp,
                           imu_source_at=stamp, odom_stamp_ns=1000000001+n,
                           imu_tick=2+n, x=0., y=0., odom_yaw=0., imu_yaw=0.,
                           gyro=[0., 0., 0.], **changes))


def setup():
    ctx = context()
    observer = StationaryEvidenceObserver(config())
    assert observer.start(ctx)["state"] == "COLLECTING"
    return observer, ctx


def feed(observer, ctx, changes=None):
    result = None
    for n in range(5):
        s = sample(ctx, n)
        s.update((changes or {}).get(n, {}))
        result = observer.observe(s, s["received_at"])
    return result


def test_noise_confirmed_metrics_and_no_physical_claim():
    o, c = setup()
    r = feed(o, c, {n: dict(x=.001*n, y=-.001*n, odom_yaw=.002*n,
                          imu_yaw=.001*n, gyro=[.001, -.002, .003]) for n in range(5)})
    assert r["state"] == "CONFIRMED" and r["sample_count"] == 5
    assert r["metrics"]["duration_s"] == pytest.approx(.4)
    assert r["metrics"]["max_planar_excursion_m"] == pytest.approx(math.hypot(.004, .004))
    assert r["physical_stationary_proven"] is False


@pytest.mark.parametrize("change", [dict(x=.1), dict(y=.1), dict(odom_yaw=.2),
                                      dict(imu_yaw=.2), dict(gyro=[.3, 0., 0.]),
                                      dict(gyro=[.15, .15, 0.])])
def test_motion_then_return_is_not_stationary(change):
    o, c = setup()
    assert feed(o, c, {1: change})["state"] == "NOT_STATIONARY"


def test_yaw_wrap_and_multiple_laps_not_endpoint_only():
    o, c = setup()
    r = feed(o, c, {n: dict(odom_yaw=math.radians(y), imu_yaw=math.radians(y))
                    for n, y in enumerate([179, -179, 179.5, -179.5, 179])})
    assert r["state"] == "CONFIRMED"
    assert r["metrics"]["odom_yaw_excursion_rad"] == pytest.approx(math.radians(2))
    o, c = setup()
    assert feed(o, c, {n: dict(odom_yaw=y) for n, y in enumerate([0, 2, -2, 0, 0])})["state"] == "NOT_STATIONARY"


@pytest.mark.parametrize("key", ["odom_stamp_ns", "imu_tick"])
@pytest.mark.parametrize("backwards", [False, True])
def test_frozen_or_backwards_source_with_fresh_transport_invalid(key, backwards):
    o, c = setup()
    first = sample(c, 0)
    o.observe(first, first["received_at"])
    next_sample = sample(c, 1)
    next_sample[key] = first[key] - int(backwards)
    assert o.observe(next_sample, next_sample["received_at"])["state"] == "INVALID_EVIDENCE"
    assert feed(o, c)["state"] == "INVALID_EVIDENCE"


@pytest.mark.parametrize("key", list(IDENTITY) + ["clock_id", "relay_state", "owner_valid", "control_fault", "stop_rpc_status"])
def test_identity_or_control_change_invalidates_confirmed(key):
    o, c = setup()
    assert feed(o, c)["state"] == "CONFIRMED"
    changed = dict(c)
    changed[key] = {"owner_valid": False, "control_fault": True,
                    "relay_state": "MOVEMENT_ENABLED", "stop_rpc_status": "STOP_RPC_PREPARED"}.get(key, "changed")
    assert o.evaluate(10.51, changed)["state"] == "INVALID_EVIDENCE"


@pytest.mark.parametrize("key", ["x", "y", "odom_yaw", "imu_yaw", "received_at", "odom_source_at", "imu_source_at"])
@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True])
def test_nonfinite_wrong_type_or_missing_invalid(key, value):
    o, c = setup()
    s = sample(c, 0)
    if value is None:
        del s[key]
    else:
        s[key] = value
    assert o.observe(s, 10.1)["state"] == "INVALID_EVIDENCE"


@pytest.mark.parametrize("gyro", [None, [], [0, 0], [0, 0, float("nan")], [0, 0, float("inf")], [False, 0, 0]])
def test_gyro_schema_invalid(gyro):
    o, c = setup()
    s = sample(c, 0); s["gyro"] = gyro
    assert o.observe(s, 10.1)["state"] == "INVALID_EVIDENCE"


@pytest.mark.parametrize("key", ["odom_source_at", "imu_source_at"])
def test_pre_stop_source_with_new_transport_is_not_counted(key):
    o, c = setup()
    s = sample(c, 0); s[key] = c["commit_at"]
    r = o.observe(s, 10.1)
    assert r["state"] == "INVALID_EVIDENCE" and r["sample_count"] == 0


def test_source_stale_separate_from_transport_and_gap_and_silence():
    o, c = setup()
    s = sample(c, 0); s["received_at"] = 10.3
    assert "source stale" in o.observe(s, 10.3)["reason"]
    o, c = setup()
    assert "transport stale" in o.observe(sample(c, 0), 10.4)["reason"]
    o, c = setup()
    o.observe(sample(c, 0), 10.1)
    assert "gap" in o.observe(sample(c, 4), 10.5)["reason"]
    o, c = setup()
    assert feed(o, c)["state"] == "CONFIRMED"
    assert o.evaluate(11., c)["state"] == "INVALID_EVIDENCE"


@pytest.mark.parametrize("key", ["received_at", "odom_source_at", "imu_source_at"])
def test_clock_backwards_invalid(key):
    o, c = setup()
    o.observe(sample(c, 1), 10.2)
    s = sample(c, 2); s[key] = 10.15
    assert o.observe(s, 10.3)["state"] == "INVALID_EVIDENCE"


def test_config_required_invalid_settings_and_prepared_start():
    with pytest.raises(ValueError):
        StationaryEvidenceObserver(None)
    for key in asdict(config()):
        for value in (None, -1, float("nan"), True):
            with pytest.raises(ValueError):
                replace(config(), **{key: value})
    o, c = setup()
    c["stop_rpc_status"] = "STOP_RPC_PREPARED"
    assert o.start(c)["state"] == "INVALID_EVIDENCE"


def test_insufficient_duration_or_count_no_rpc_only_confirmation():
    o, c = setup()
    assert o.evaluate(10.1, c)["state"] == "COLLECTING"
    for n in range(3):
        assert o.observe(sample(c, n), 10.1+.1*n)["state"] == "COLLECTING"
    assert o.invalidate("new Move")["state"] == "INVALID_EVIDENCE"
    assert o.start(c)["sample_count"] == 0


def test_read_only_sdk_fields_and_missing_not_synthetic():
    raw = NS(tick=99, imu_state=NS(gyroscope=[.01, .02, .03]))
    assert lowstate_evidence(raw) == dict(imu_tick=99, imu_gyro=(.01, .02, .03))
    assert odom_evidence(NS(header=NS(stamp=NS(sec=2, nanosec=3)))) == dict(odom_stamp_ns=2000000003)
    assert lowstate_evidence(NS())["imu_tick"] is None
    assert odom_evidence(NS())["odom_stamp_ns"] is None
    assert odom_evidence(NS(header=NS(stamp=NS(sec=0, nanosec=0))))["odom_stamp_ns"] is None
    assert lowstate_evidence(NS(tick=True, imu_state=raw.imu_state))["imu_tick"] is None


def test_production_callbacks_preserve_source_identity_on_repeated_delivery():
    # Execute only production callback bodies, not SDK imports/runtime init.
    path = Path(__file__).resolve().parents[1] / "patrol/locomotion_relay.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and n.name in {"lowstate", "mapping_odom"}]
    times = iter([10., 11., 12., 13.])
    env = dict(lock=threading.Lock(), imu=dict(count=0), odom=dict(count=0),
               math=math, time=NS(monotonic=lambda: next(times)),
               lowstate_evidence=lowstate_evidence, odom_evidence=odom_evidence)
    exec(compile(ast.Module(body=functions, type_ignores=[]), "callbacks", "exec"), env)
    imu = NS(tick=99, imu_state=NS(rpy=[0., 0., .2], gyroscope=[.1, .2, .3]))
    odom = NS(header=NS(stamp=NS(sec=3, nanosec=7)), pose=NS(pose=NS(
        position=NS(x=1., y=2.), orientation=NS(w=1., x=0., y=0., z=0.))))
    for _ in range(2):
        env["lowstate"](imu); env["mapping_odom"](odom)
    assert env["imu"]["count"] == env["odom"]["count"] == 2
    assert env["imu"]["imu_tick"] == 99
    assert env["odom"]["odom_stamp_ns"] == 3000000007
    assert env["imu"]["received"] == 12. and env["odom"]["received"] == 13.
    assert env["imu"]["imu_gyro"] == (.1, .2, .3)


def test_tick_wrap_unknown_clock_and_overflow_fail_closed():
    o, c = setup()
    c["imu_tick"] = 2**32 - 1
    o.start(c)
    s = sample(c, 0); s["imu_tick"] = 0
    assert o.observe(s, 10.1)["state"] == "INVALID_EVIDENCE"
    o, c = setup()
    s = sample(c, 0); s["odom_source_at"] = None
    assert o.observe(s, 10.1)["state"] == "INVALID_EVIDENCE"
    o, c = setup()
    s = sample(c, 0); s["x"] = 10**1000
    assert o.observe(s, 10.1)["state"] == "INVALID_EVIDENCE"
    o, c = setup()
    o.evaluate(10.1, c)
    assert o.evaluate(10.05, c)["state"] == "INVALID_EVIDENCE"


def test_replay_measurement_report_and_cli(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/analyze_stationary_telemetry.py"
    spec = importlib.util.spec_from_file_location("stationary_analyzer", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    c = context()
    records = [dict(type="start", context=c)] + [dict(type="sample", sample=sample(c, n), now=10.1+.1*n) for n in range(5)]
    result = module.analyze(records, config())
    assert result["final"]["state"] == "CONFIRMED"
    assert not result["thresholds_recommended"] and not result["production_authorization"]
    replay = tmp_path / "samples.jsonl"; settings = tmp_path / "limits.json"
    replay.write_text("\n".join(json.dumps(r) for r in records))
    settings.write_text(json.dumps(asdict(config())))
    output = subprocess.run([sys.executable, "-B", str(path), str(replay), "--config", str(settings)], capture_output=True, text=True)
    assert output.returncode == 0 and json.loads(output.stdout) == result
    records.append(records[-1])
    assert module.analyze(records, config())["final"]["state"] == "INVALID_EVIDENCE"
    records.append(dict(type="invalid"))
    assert module.analyze(records, config())["invalid_evidence_records"] == 2
    output = subprocess.run([sys.executable, "-B", str(path), str(replay)], capture_output=True)
    assert output.returncode != 0
