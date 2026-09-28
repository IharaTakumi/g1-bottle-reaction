#!/usr/bin/env python3
"""Offline raw calibration JSONL measurements; no I/O to G1 or authorization."""
import argparse
import json
import math
from pathlib import Path

STATUSES = {"UNVERIFIED", "CANDIDATE", "VERIFIED_FOR_THIS_SESSION", "INVALID"}
PHASES = {"PRE", "MOVEMENT_ENABLE", "MOVING", "STOP_REQUESTED", "STOP_RPC_PREPARED",
          "STOP_RPC_CONFIRMED", "POST_STOP_OBSERVATION", "POST_STOP_OBSERVATION_COMPLETE",
          "CONTROLLED_TEARDOWN", "EXTERNAL_STATIONARY_MARK", "END"}
IDENTITIES = ("relay_epoch", "owner_session", "movement_generation", "stop_request_id")
RAW = ("odom_x_raw", "odom_y_raw", "odom_yaw_raw", "odom_stamp_raw",
       "lowstate_yaw_raw", "lowstate_tick_raw", "lowstate_tick_unwrapped")
STATUS_KEYS = ("odom_stamp_unit_status", "odom_clock_mapping_status", "lowstate_tick_status")


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate(record):
    """Validate recording structure, never the truth of a mapping or STOP claim."""
    if not isinstance(record, dict) or type(record.get("schema_version")) is not int or record["schema_version"] != 1:
        raise ValueError("schema_version must be integer 1")
    for key in ("session_id", "trial_id", "pc_clock_id", "stop_transaction_state"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError("missing string: " + key)
    if record.get("record_type") not in {"sample", "marker"} or record.get("phase") not in PHASES:
        raise ValueError("invalid record_type/phase")
    if not number(record.get("pc_receive_monotonic_s")) or record["pc_receive_monotonic_s"] < 0:
        raise ValueError("invalid PC receipt time")
    for key in IDENTITIES:
        if key not in record or (record[key] is not None and not isinstance(record[key], str)):
            raise ValueError("identity must be present, string or null: " + key)
    for key in ("transport", "operator_marker", "external_reference"):
        if not isinstance(record.get(key), dict):
            raise ValueError("missing object: " + key)
    if record["record_type"] == "marker":
        return
    for key in RAW:
        if key not in record or (record[key] is not None and not number(record[key])):
            raise ValueError("raw field must be numeric or null: " + key)
    for key in ("odom_stamp_raw", "lowstate_tick_raw", "lowstate_tick_unwrapped"):
        value = record[key]
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("raw identity must be nonnegative integer or null: " + key)
    tick = record["lowstate_tick_raw"]
    if tick is not None and tick >= 2**32:
        raise ValueError("tick outside uint32")
    gyro = record.get("lowstate_gyro_raw", "missing")
    if gyro is not None and (not isinstance(gyro, list) or len(gyro) != 3 or not all(number(v) for v in gyro)):
        raise ValueError("gyro must be a three-number array or null")
    for key in STATUS_KEYS:
        if record.get(key) not in STATUSES:
            raise ValueError("explicit status required: " + key)
    # A claim remains a claim. This prevents an undocumented VERIFIED label,
    # but cannot establish physical clock semantics from a JSON record.
    if any(record[k] == "VERIFIED_FOR_THIS_SESSION" for k in STATUS_KEYS):
        if not isinstance(record.get("verification_ref"), str) or not record["verification_ref"].strip():
            raise ValueError("VERIFIED claim requires a session review reference")
    normalized = record.get("normalized")
    if normalized is not None:
        if not isinstance(normalized, dict) or not isinstance(normalized.get("units_review_ref"), str) or not normalized["units_review_ref"].strip():
            raise ValueError("normalized values require units_review_ref")
        for key in ("odom_yaw_rad", "imu_yaw_rad"):
            if not number(normalized.get(key)) or abs(normalized[key]) > math.pi:
                raise ValueError("normalized yaw must be finite radians in [-pi, pi]")


def stats(values):
    values = sorted(values)
    if not values:
        return dict(count=0, min=None, p50=None, p95=None, max=None)
    def percentile(p):
        index = (len(values) - 1) * p
        low = int(index)
        high = min(low + 1, len(values) - 1)
        return values[low] + (values[high] - values[low]) * (index - low)
    return dict(count=len(values), min=values[0], p50=percentile(.5), p95=percentile(.95), max=values[-1])


def clock_metrics(samples, key):
    pairs = [(a, b) for a, b in zip(samples, samples[1:])
             if a[key] is not None and b[key] is not None]
    deltas = [b[key] - a[key] for a, b in pairs]
    rates = [(b[key] - a[key]) / (b["pc_receive_monotonic_s"] - a["pc_receive_monotonic_s"])
             for a, b in pairs if b[key] > a[key] and b["pc_receive_monotonic_s"] > a["pc_receive_monotonic_s"]]
    # Descriptive fit only for a complete, strictly advancing series. Never
    # unwrap, discard discontinuities, or estimate true source age from receipt.
    fit = None
    if len(samples) >= 2 and len(pairs) == len(samples)-1 and all(d > 0 for d in deltas):
        times = [s["pc_receive_monotonic_s"] for s in samples]
        if all(b > a for a, b in zip(times, times[1:])):
            scale = (samples[-1][key] - samples[0][key]) / (times[-1] - times[0])
            residuals = [(s[key] - samples[0][key])/scale - (t-times[0]) for s, t in zip(samples, times)]
            fit = dict(apparent_raw_units_per_receive_second=scale,
                       endpoint_fit_residual_s=stats(residuals),
                       residual_timeline_s=residuals,
                       interpretation="receipt-relative trend only; offset/one-way delay unidentified")
    return dict(raw_delta=stats(deltas), duplicates=sum(d == 0 for d in deltas),
                backwards_or_wrap_or_reset=sum(d < 0 for d in deltas),
                missing=sum(s[key] is None for s in samples),
                adjacent_apparent_rate=stats(rates), receipt_fit=fit)


def yaw_excursion(samples):
    if not samples or any(s.get("normalized") is None for s in samples):
        return None
    result = {}
    for key in ("odom_yaw_rad", "imu_yaw_rad"):
        angles = [s["normalized"][key] for s in samples]
        accumulated = [0.0]
        for a, b in zip(angles, angles[1:]):
            accumulated.append(accumulated[-1] + math.atan2(math.sin(b-a), math.cos(b-a)))
        result[key] = max(accumulated)-min(accumulated)
    return result


def summarize(records):
    groups = {}
    for line, record in enumerate(records, 1):
        try:
            validate(record)
        except ValueError as exc:
            raise ValueError(f"line {line}: {exc}") from exc
        key = (record["session_id"], record["trial_id"], record["pc_clock_id"])
        groups.setdefault(key, []).append(record)
    reports = []
    for (session, trial, clock), rows in groups.items():
        samples = [r for r in rows if r["record_type"] == "sample"]
        times = [r["pc_receive_monotonic_s"] for r in samples]
        intervals = [b-a for a, b in zip(times, times[1:])]
        markers = [r for r in rows if r["record_type"] == "marker"]
        confirms = [r for r in markers if r["phase"] == "STOP_RPC_CONFIRMED"
                    and r.get("event", "STOP_RPC_CONFIRMED") == "STOP_RPC_CONFIRMED"
                    and r["transport"].get("command_scope") not in {"initialization", "cleanup"}]
        # Multiple confirmations are ambiguous: no automatic choice of STOP.
        origin = confirms[0]["pc_receive_monotonic_s"] if len(confirms) == 1 else None
        xy = [(s["odom_x_raw"], s["odom_y_raw"]) for s in samples
              if s["odom_x_raw"] is not None and s["odom_y_raw"] is not None]
        norms = [math.hypot(*s["lowstate_gyro_raw"]) for s in samples if s["lowstate_gyro_raw"] is not None]
        timeline = []
        for s in samples:
            timeline.append(dict(received_at=s["pc_receive_monotonic_s"],
                relative_to_confirm_receipt_s=None if origin is None else s["pc_receive_monotonic_s"]-origin,
                phase=s["phase"], x_raw=s["odom_x_raw"], y_raw=s["odom_y_raw"],
                odom_yaw_raw=s["odom_yaw_raw"], imu_yaw_raw=s["lowstate_yaw_raw"],
                gyro_norm_raw=None if s["lowstate_gyro_raw"] is None else math.hypot(*s["lowstate_gyro_raw"])))
        reports.append(dict(session_id=session, trial_id=trial, pc_clock_id=clock,
            sample_count=len(samples), record_count=len(rows),
            recording_span_s=max(r["pc_receive_monotonic_s"] for r in rows)-min(r["pc_receive_monotonic_s"] for r in rows),
            receive_interval_s=stats(intervals), receive_backwards=sum(d < 0 for d in intervals),
            odom_clock=clock_metrics(samples, "odom_stamp_raw"),
            lowstate_clock=clock_metrics(samples, "lowstate_tick_raw"),
            planar_excursion_raw=None if not xy else max(math.hypot(x-xy[0][0], y-xy[0][1]) for x, y in xy),
            gyro_magnitude_raw=stats(norms),
            yaw_raw_values={k: [s[k] for s in samples] for k in ("odom_yaw_raw", "lowstate_yaw_raw")},
            yaw_excursion_rad=yaw_excursion(samples),
            stop_confirm_marker_count=len(confirms), timeline=timeline,
            markers=[dict(r, relative_to_confirm_receipt_s=None if origin is None else r["pc_receive_monotonic_s"]-origin) for r in markers],
            identity_timeline=[{k: s[k] for k in IDENTITIES + ("pc_receive_monotonic_s", "stop_transaction_state")} for s in samples],
            mapping_status_timeline=[{k: s[k] for k in STATUS_KEYS} for s in samples]))
    return dict(schema_version=1, measurement_only=True, clock_verification_performed=False,
                thresholds_recommended=False, production_authorization=False, trials=reports)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    args = parser.parse_args(argv)
    try:
        records = [json.loads(line) for line in args.recording.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not records:
            raise ValueError("empty recording")
        print(json.dumps(summarize(records), allow_nan=False, indent=2))
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
