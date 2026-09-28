"""Local files and synthetic records only; no SDK, socket or robot."""
import copy
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_stationary_calibration.py"
spec = importlib.util.spec_from_file_location("calibration", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def sample(n=0):
    return dict(schema_version=1, session_id="test", trial_id="A01", pc_clock_id="pc-boot-1",
        record_type="sample", phase="PRE", pc_receive_monotonic_s=10.+n*.1,
        relay_epoch=None, owner_session=None, movement_generation=None, stop_request_id=None,
        stop_transaction_state="UNOBSERVED", transport={"record_index": n},
        operator_marker={}, external_reference={}, odom_x_raw=0., odom_y_raw=0.,
        odom_yaw_raw=0., odom_stamp_raw=10**18+n*100000000,
        odom_stamp_unit_status="UNVERIFIED", odom_clock_mapping_status="UNVERIFIED",
        lowstate_yaw_raw=0., lowstate_gyro_raw=[0., 0., 0.],
        lowstate_tick_raw=100+n, lowstate_tick_unwrapped=None, lowstate_tick_status="UNVERIFIED")


def marker(phase, at):
    r = sample()
    r.update(record_type="marker", phase=phase, pc_receive_monotonic_s=at)
    return r


def test_raw_roundtrip_metrics_and_no_authorization():
    records = [sample(n) for n in range(3)]
    records[1].update(odom_x_raw=.2, lowstate_gyro_raw=[3., 4., 0.])
    records += [marker("STOP_RPC_CONFIRMED", 10.1), marker("EXTERNAL_STATIONARY_MARK", 10.2)]
    original = copy.deepcopy(records)
    report = module.summarize(json.loads(json.dumps(records)))
    trial = report["trials"][0]
    assert records == original
    assert trial["planar_excursion_raw"] == .2
    assert trial["gyro_magnitude_raw"]["max"] == 5.
    assert trial["receive_interval_s"]["p95"] == pytest.approx(.1)
    assert trial["odom_clock"]["raw_delta"]["min"] == 100000000
    assert trial["odom_clock"]["receipt_fit"]["apparent_raw_units_per_receive_second"] == pytest.approx(1e9)
    assert trial["markers"][-1]["relative_to_confirm_receipt_s"] == pytest.approx(.1)
    assert trial["timeline"][0]["relative_to_confirm_receipt_s"] == pytest.approx(-.1)
    assert trial["yaw_excursion_rad"] is None
    assert report["clock_verification_performed"] is False
    assert report["thresholds_recommended"] is report["production_authorization"] is False


@pytest.mark.parametrize("key", module.STATUS_KEYS)
def test_unknown_status_and_unsubstantiated_verified_rejected(key):
    r = sample(); r[key] = "guessed"
    with pytest.raises(ValueError): module.summarize([r])
    r[key] = "VERIFIED_FOR_THIS_SESSION"
    with pytest.raises(ValueError): module.summarize([r])
    r["verification_ref"] = "session-review.json#clock"
    assert module.summarize([r])["clock_verification_performed"] is False


@pytest.mark.parametrize("key,value", [("odom_stamp_raw", True), ("lowstate_tick_raw", 2**32),
    ("lowstate_gyro_raw", [0, float("nan"), 0]), ("pc_receive_monotonic_s", float("inf")),
    ("schema_version", True), ("lowstate_tick_unwrapped", -1)])
def test_invalid_schema(key, value):
    r = sample(); r[key] = value
    with pytest.raises(ValueError): module.summarize([r])


def test_missing_field_rejected_but_explicit_unavailable_preserved():
    r = sample(); del r["odom_stamp_raw"]
    with pytest.raises(ValueError): module.summarize([r])
    r["odom_stamp_raw"] = None
    assert module.summarize([r])["trials"][0]["odom_clock"]["missing"] == 1


def test_duplicate_wrap_backwards_not_hidden_by_fit_or_unwrap():
    rows = [sample(n) for n in range(4)]
    for r, t in zip(rows, [2**32-2, 2**32-2, 0, 1]): r["lowstate_tick_raw"] = t
    report = module.summarize(rows)["trials"][0]["lowstate_clock"]
    assert report["duplicates"] == report["backwards_or_wrap_or_reset"] == 1
    assert report["receipt_fit"] is None
    assert all(r["lowstate_tick_unwrapped"] is None for r in rows)


def test_yaw_wrap_with_explicit_normalization_and_multiple_laps():
    rows = [sample(n) for n in range(5)]
    for r, yaw in zip(rows, [179, -179, 179, -179, 179]):
        r["normalized"] = dict(units_review_ref="units.json", odom_yaw_rad=math.radians(yaw), imu_yaw_rad=0.)
    assert module.summarize(rows)["trials"][0]["yaw_excursion_rad"]["odom_yaw_rad"] == pytest.approx(math.radians(2))
    for r, yaw in zip(rows, [0, 2, -2, 0, 2]): r["normalized"]["odom_yaw_rad"] = yaw
    assert module.summarize(rows)["trials"][0]["yaw_excursion_rad"]["odom_yaw_rad"] > 2*math.pi


def test_trials_and_clock_resets_not_merged_ambiguous_stop_no_origin():
    a, b = sample(), sample(1); b["pc_clock_id"] = "new-boot"
    assert len(module.summarize([a, b])["trials"]) == 2
    rows = [a, marker("STOP_RPC_CONFIRMED", 10), marker("STOP_RPC_CONFIRMED", 11)]
    assert module.summarize(rows)["trials"][0]["timeline"][0]["relative_to_confirm_receipt_s"] is None


def test_cli_local_only(tmp_path):
    path = tmp_path / "trial.jsonl"
    path.write_text(json.dumps(sample()) + "\n", encoding="utf-8")
    before = path.read_bytes()
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), str(path)], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["measurement_only"] is True
    assert path.read_bytes() == before
    path.write_text("{}\n", encoding="utf-8")
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), str(path)], capture_output=True)
    assert result.returncode != 0
