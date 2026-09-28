# F04-B: offline stationary evidence

F04-A STOP transactions remain CLOSED. Observer logic is an offline component;
the physical stationary production gate remains OPEN, requiring real G1
calibration and a separately reviewed integration. Reaction still uses F04-A
only. No new production thresholds or recorder/launcher are provided.

## Verified telemetry inventory

Read-only SDK source: `65691c8a8bc53b98d3976dba4dbf9d5d20b2e7f5`.

| Signal | Actual source | Existing/new relay fields | What it proves |
| --- | --- | --- | --- |
| Odom x/y | `Odometry_.pose.pose.position` | existing `odom_x`, `odom_y` | estimator output |
| Odom yaw | pose quaternion via atan2 | existing `odom_yaw` | estimator rotation |
| IMU yaw | `LowState_.imu_state.rpy[2]` | existing `yaw` | rotational observation |
| Gyro | hg `IMUState_.gyroscope`, float32[3] | new `imu_gyro` | raw vector; deployed units/frame pending |
| LowState progress | hg `LowState_.tick`, uint32 | new `imu_tick` | source counter, wrap/rate semantics pending |
| Odom progress | `header.stamp.sec/nanosec` | new integer `odom_stamp_ns` | source stamp; no header sequence exists |
| Callback receipt | relay `time.monotonic()` | new `imu_received_monotonic`, `odom_received_monotonic` | callback receipt only |
| Source callback age | relay receipt to transmit | existing `imu_age`, `odom_age` | not source generation age |
| UDP receipt age | PC adapter monotonic receipt | existing `transport_age` | transport freshness only |
| Packet timestamp | relay `time.time()` | existing `timestamp` | transmit wall time, not source time |

Callback `count` and rates remain arrival counters, never source identities.
Missing/malformed raw evidence exports null. No fabricated tick/stamp is used.
New read-only packet identity fields are relay_epoch, owner_session,
movement_generation, stop_request_id, stop_rpc_status and relay_state. They label
the current relay state, not the age/origin of cached sensor data. Baselines and
post-commit source times must therefore still be checked separately.
Deploy the new pure `telemetry_evidence.py` beside the relay if using these fields.
Existing telemetry consumers preserve the extra fields; their safety gates and
thresholds are unchanged. These additions do not make current UDP an admissible
stationary sample automatically.

## Canonical offline schema

Use `StationaryEvidenceObserver(EvidenceConfig(...))`; every configuration field
is required. None/missing/invalid config rejects construction. Fields:
max_planar_excursion_m, max_yaw_excursion_rad, max_gyro_rad_s, required_hold_s,
minimum_samples, maximum_sample_gap_s, maximum_transport_age_s,
maximum_source_age_s. Numeric examples occur only in synthetic tests. No values
are installed into production YAML or automatically recommended.

`start(context)` requires:

- UUID strings: relay_epoch, owner_session, movement_generation, stop_request_id.
- stop_rpc_status=`STOP_RPC_CONFIRMED`, relay_state=`MOVEMENT_HELD`,
  owner_valid=true, control_fault=false.
- clock_id: identifier of the explicitly established common evaluation clock.
- commit_at: commit boundary in that clock, plus source baselines odom_stamp_ns
  and imu_tick at the boundary. An unavailable baseline cannot be invented.

Each sample includes those identity/control fields and clock_id, plus:

- received_at: transport receipt in the evaluation clock.
- odom_source_at / imu_source_at: source generation times mapped into that clock.
- odom_stamp_ns / imu_tick: original source identities, strictly advancing.
- x, y, odom_yaw, imu_yaw, gyro: SI-normalized pose and three-axis angular velocity.

Yaw must be radians in [-pi, pi]. Gyro is the Euclidean vector magnitude, not an
unverified body-axis assumption. Units/frame and estimator relationships require
real deployment verification; these observations are complementary, not proven
independent sensors. The schema's source_at values are explicit offline input
requirements, NOT fields currently proven available from G1. Never substitute
callback receipt or an incrementing local counter for source time/progression.
Unverified clock mapping/units must be represented as unavailable, which rejects
evidence. Cross-host monotonic clocks cannot be compared directly. No implicit
tick-to-seconds conversion or wall-clock synchronization is assumed here.

Both source times must be strictly after commit_at, no later than received_at,
and fresh relative to explicit `now`. Both raw identities must exceed the commit
baselines and previous sample. Repeated values (including packets with advancing
transport receipt) invalidate. Tick wrap/reset is conservatively invalid and
requires explicit re-establishment. A future ingestion layer must align genuinely
new observations from both sources; repeated asynchronous snapshots are not
additional evidence samples.

## Window, health and lifecycle

One explicit evaluation window; constant memory and no hidden rolling reset.
States: IDLE, COLLECTING, CONFIRMED, NOT_STATIONARY, INVALID_EVIDENCE.
Metrics use all eligible samples: maximum planar distance from first sample,
separate odom/IMU yaw ranges after shortest-delta unwrapping, maximum gyro norm,
sample count, overlapping source-window duration, maximum source/transport gap.
The hold interval starts at the first eligible sources, never at STOP send time
or from an old ring buffer. Endpoint return cannot erase an earlier excursion.
Yaw wrap crossing is handled; rotations exceeding pi between samples are
fundamentally aliased and must be excluded by future sampling/calibration work.

Motion yields NOT_STATIONARY, distinct from malformed/stale/identity-invalid
evidence. Motion and invalid evidence do not automatically recover. Explicit
start is required to clear a window. Health failures can invalidate a moving or
confirmed window. New Move, ownership loss, fault or identity change must be fed
to `evaluate(now, context)` or `invalidate(reason)` immediately by a future caller.
There is no background thread or automatic event subscription. Call evaluate
even during sample silence and before using evidence. `result()` is a historical
snapshot, not a liveness check. Invalid state is latched; no session auto-binding.

## Replay utility

`python scripts/analyze_stationary_telemetry.py recording.jsonl --config limits.json`

The config JSON contains all EvidenceConfig fields explicitly. Each JSONL line:

- `{"type":"start","context":{...}}`
- `{"type":"sample","now":...,"sample":{...}}`
- `{"type":"evaluate","now":...,"context":{...}}` (including silence intervals)
- `{"type":"invalidate","reason":"new Move"}`

Output reports state/reason and measurements for each input record, final snapshot,
and invalid-record count. It neither picks thresholds nor authorizes production.
Frozen/source-regression reasons appear on their first offending record; the
invalid-record count counts records in invalid state, not distinct anomalies.
Truncated recordings only describe the recorded interval. To assess later silence,
include a later evaluate record. No real recorder is launched by this tool.

## Limitations and next measurements

Advancing source stamps plus a stuck/wrong pose estimator, translating robot and
near-zero gyro can still appear stationary. This is not complete physical proof.
Post-commit relay survival, stopping distance and stationary persistence are not
guaranteed. Future production evidence must bind current epoch/generation and
fresh telemetry at use, independently of historical RPC success.

Before production integration, measure/verify:

1. Deployed SDK/firmware fields, tick units/rate/wrap and odom stamp clock semantics.
2. End-to-end source age, clock mapping uncertainty, packet loss/reorder and gaps.
3. Standing sway/noise, intended translations/rotations, move-and-return and
   estimator freezes; gyro units/frame and estimator correlations.
4. STOP-to-motion-decay timing and externally measured residual motion/distance.
5. Bounds, required duration/count and false positives/negatives from recorded
   evidence. Review/calibrate values explicitly before connecting Reaction.
