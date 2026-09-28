# G1 stationary calibration — first-session runbook, 2026-09-28

F04-A: CLOSED. F04-B observer logic: OFFLINE READY.
Physical stationary production gate: **OPEN — REAL G1 CALIBRATION REQUIRED**.
This preparation changes no production algorithm, threshold, protocol, lease, ownership,
watchdog or Reaction behavior; the controller adds only a public single-leg entrypoint.
All commands below are future operator steps;
no hardware connection was made while preparing this document.

## 0. Readiness and stop points

Use this document top to bottom. Record the operator, safety observer, date, site,
session ID, all deviations and GO/NO-GO decisions in `session-review.json`.
`scripts/run_stationary_calibration.py` is the dedicated calibration entrypoint.
It does not launch/import the Integrated Supervisor, Reaction, Vision, MotionDecode,
audio or Wander. Its movement worker calls `PatrolController.run_calibration_leg`,
which resumes under the existing F03 lease, executes exactly one existing forward
or turn leg, confirms its existing v4 STOP, and remains paused/HELD for observation.
The main thread alone renews the lease through POST_STOP observation and its flush.
The movement worker checks (never renews) that same lease while held, so a main
hang during observation still faults through F03. There is no independent heartbeat
thread. No second leg or resume is scheduled.
Standing constructs no command adapter/session and sends no claim, Move or STOP.

The recorder preserves exact received relay datagrams plus canonical fields, and
exact v4 client send/receive datagrams plus validated STOP event records. It does
not invent DDS samples between snapshots or reconstruct source values discarded
before relay serialization. It does not establish a source baseline at the relay's
atomic commit instant or a verified source-clock conversion. Those remain real
calibration questions, not software-side guarantees.

Review this harness before deployment. Do not substitute the production Integrated
Supervisor: it still starts Reaction and its behavior is unchanged. Do not use a
one-shot SDK writer, a dummy heartbeat loop, or keyboard timing to isolate a leg.
Actual motion readiness requires the deployment/physical checklist below.

## 1. Freeze and verify deployment before any motion

Required source pins (or explicitly reviewed descendants with unchanged relevant code):

| Component | Required SHA / contract |
| --- | --- |
| Integration/F04-B | `adc7fada59492c09cc518c449bb3e5da945e1032` |
| F04-A ancestry | `90fa3e75afd4fda617049a41d881c9bbd224d921`; client AND relay protocol v4 |
| Wander ownership implementation | `8650cedcb26b8836d1686d1397fcc50cb470f25e` |
| Wander launch contract ancestry | `66d14a6f4beee08d8ad979b5031e48e840f63563`; `--require-locomotion-ownership-v1` |
| MotionDecode resident/protocol/client | `0ee0e90bc4f42a40c00003be4d40caab03a6550a`; bound protocol v2 |
| SDK source reviewed offline | `65691c8a8bc53b98d3976dba4dbf9d5d20b2e7f5`; verify installed version separately |

MotionDecode `preflight_bound` / `execute_bound` must be the reviewed pair; legacy
execute is not a fallback. Keep MotionDecode and Vision-triggered Reaction OFF for
these trials. Wander is OFF as well; its pin is a deployment exclusion/compatibility
check, not a request to launch it. **forward-distance utilities: MUST NOT USE**.

`G1_STATIONARY_CALIBRATION_HASHES_20260928.json` contains SHA-256 of exact immutable
Git blob bytes for Patrol modules/configs, supervisor, integration adapters,
Wander ownership files and resident protocol files. It is not a complete runtime
dependency attestation. Record Python executable/version, SDK path/commit/dirty
status, firmware, imported module locations, dependencies and motion asset manifests
separately. Deploy LF bytes without rewriting. For standalone relay copies preserve
all sibling imports, including `telemetry_evidence.py`, `stop_rpc.py`,
`locomotion_protocol.py`, `ownership_lock.py`; hash the actually loaded files.

On each relevant host, from its local console, read-only commands (replace paths):

```bash
git -C "$INTEGRATION_ROOT" status --short
git -C "$INTEGRATION_ROOT" rev-parse HEAD
git -C "$INTEGRATION_ROOT" merge-base --is-ancestor adc7fada59492c09cc518c449bb3e5da945e1032 HEAD
"$PYTHON" --version
git -C "$SDK_ROOT" rev-parse HEAD
git -C "$SDK_ROOT" status --short
sha256sum "$RELAY_DIR/locomotion_relay.py" "$RELAY_DIR/locomotion_protocol.py" \
  "$RELAY_DIR/ownership_lock.py" "$RELAY_DIR/stop_rpc.py" "$RELAY_DIR/telemetry_evidence.py"
```

Compare each result with the manifest, including PC client files, not just relay.
Record path, expected hash, observed hash, reviewer and result. A SHA string alone
does not prove the running process uses that checkout. Failures mean NO-GO; do not
repair deployment by launching an old checkout or downgrading v4.

### Process and cooperative lock inventory

Read-only commands on PC and robot host, BEFORE starting the intended writer:

```bash
ps -eo user,pid,ppid,lstart,args --ww
pgrep -af 'locomotion_relay|run_patrol|run_integrated_demo|g1-wander|wander_reactive|wander_forward|walk_forward_real|g1_robot|resident_worker|run_motiondecode|g1_dual_camera|g1-teleop'
ss -lupn
ss -lxpn
lslocks
stat -Lc '%d:%i %U:%G %a %n' /tmp/g1-project-locomotion.lock
```

For each candidate PID, with `PID` set to that numeric PID:

```bash
readlink -f "/proc/$PID/exe"
readlink -f "/proc/$PID/cwd"
tr '\0' ' ' < "/proc/$PID/cmdline"
ls -l "/proc/$PID/fd"
readlink "/proc/$PID/ns/mnt"
```

Patterns come from current entrypoints and older Wander branch utilities:
`scripts/g1-wander-reactive-mvp.py`, `g1-wander-forward-distance.py`,
`g1-wander-loco-once.py`, `robot_side/wander_locomotion.py`,
`robot_side/wander_forward_distance.py`, direct adapter writers and old standalone
relay copies. Process-name search cannot prove the absence of renamed SDK writers;
also inspect the complete inventory, services/containers and operator controllers.
If permissions hide a process, resolve that visibility before GO. Do not kill unknown
processes or stop `videohub_pc4`. Do not unlink/replace the canonical lock inode.

Verify same host/mount namespace and the same `/tmp/g1-project-locomotion.lock`
device/inode across cooperating writers; inspect the intended writer's open descriptor
after authorized initialization. No competing or residual finite command may remain.
Lock release/process exit is not physical STOP. Confirm protocol v4 pair and healthy
F03 Control Lease through the reviewed procedure without bypass or autonomous
heartbeat sender. Preserve existing guard/watchdog settings. Clear area and an
operator emergency-stop procedure are mandatory independently of RPC confirmation.

## 2. Prepare files and independent physical reference

Directory: `calibration/<session_id>/<trial_id>/` with unique IDs such as
`20260928-site01/A01`. Retain `raw.jsonl`, original packet/callback/event logs,
`external.mp4`, `markers.json`, `review.json`, hashes and `measurement.json`.
Do not overwrite files or reuse IDs for a retrial; use A01r1 etc.

Use a fixed smartphone/camera, preferably >=60 fps, landscape, tripod/rigid support.
Place it outside the cleared swept area, roughly side-on for forward travel; include
the entire G1, both feet, floor markers and full potential stopping region throughout.
For turns choose an oblique/high position where both feet and torso heading remain
visible; a second fixed view is useful if one view occludes a foot. Do not use G1
camera acquisition or Vision Reaction. Lock focus/exposure if available.

Lay measured tape marks at 0.10 m spacing along the motion plane, with 0.50 m
major marks and a visible orientation line for turns. These are measurement aids,
not stopping-distance or clearance limits. Record the measured spacing and uncertainty.
Use a visible trial-ID slate and a marker visible in the video and timestamped in
the acquisition record at start/end. A manual marker has unknown human latency;
record an uncertainty interval, never equate it to the RPC processing instant.
Filename: `<session_id>_<trial_id>_external_cam01.mp4`; keep actual frame timestamps,
frame rate/drop information and video hash. Record external stationary observations
as video frame/time intervals with a reviewer, not solely an operator button press.

60 fps gives nominal 16.7 ms frame spacing, NOT 16.7 ms STOP synchronization accuracy.
Report spatial resolution from tape-to-pixel calibration at the feet and annotate
parallax/occlusion; do not promise a millimetre accuracy. External reference is
independent of odom/IMU but finite-resolution video cannot prove zero velocity.

## 3. Canonical raw recording schema v1

UTF-8 JSONL, one immutable record per sample/event in capture order. Never sort the
original file, interpolate missing records, forward-fill a source identity, or
substitute receipt time for source time. `null` means unavailable; malformed raw
packets remain in an immutable sidecar referenced by byte offset/hash. Snapshot
telemetry may repeat source values; keep those duplicates for analysis. Do not treat
them as new DDS samples. Document whether `odom_stamp_raw` came from the relay's
integer `odom_stamp_ns` or the original `header.stamp` sec/nanosec sidecar.

Every record has:

| Field | Type / meaning |
| --- | --- |
| schema_version | integer 1 |
| session_id, trial_id, pc_clock_id | nonempty strings; change clock ID after PC boot/clock reset |
| record_type | `sample` or `marker` |
| phase | PRE, MOVEMENT_ENABLE, MOVING, STOP_REQUESTED, STOP_RPC_PREPARED, STOP_RPC_CONFIRMED, POST_STOP_OBSERVATION, POST_STOP_OBSERVATION_COMPLETE, CONTROLLED_TEARDOWN, EXTERNAL_STATIONARY_MARK, END |
| pc_receive_monotonic_s | finite local PC receipt/event-log time; preserve clock provenance |
| relay_epoch, owner_session, movement_generation, stop_request_id | actual UUID strings, or null when unobserved; never invent IDs |
| stop_transaction_state | observed state, or `UNOBSERVED`; never infer confirmed from a button press |
| transport | object: endpoint, packet index, receive clock, original packet sidecar reference/hash; optional relay callback receipts and packet wall time labeled separately |
| operator_marker | object: operator, action, marker ID, note, event clock/uncertainty when applicable; otherwise {} |
| external_reference | object: video filename/hash, frame/time interval, reviewer, synchronization uncertainty; otherwise {} |

Each `sample` additionally requires:

| Field | Type / meaning |
| --- | --- |
| odom_x_raw, odom_y_raw, odom_yaw_raw | finite source numbers or null; no unit assumption |
| odom_stamp_raw | nonnegative integer or null, preserving integer precision |
| odom_stamp_unit_status, odom_clock_mapping_status | explicit status below |
| lowstate_yaw_raw | finite raw number or null |
| lowstate_gyro_raw | three finite raw numbers or null; original axis order |
| lowstate_tick_raw | original uint32 or null |
| lowstate_tick_unwrapped | nonnegative derived integer or null; initially null |
| lowstate_tick_status | explicit status below |

Optional `normalized` stores derived `odom_yaw_rad`, `imu_yaw_rad` in [-pi, pi]
plus `units_review_ref`. It never replaces raw yaw. Optional verified/derived
objects may contain converted positions/gyro/source times with method and review
references; the raw analyzer does not use them for authorization. Preserve complete
original SDK fields in sidecars if relay extraction rejected them to null.
Marker records do not require sample fields. For B/C keep one target STOP per trial;
initialization/cleanup STOPs are separately scoped and excluded from the primary
STOP origin. If multiple primary confirmed
markers remain, the analyzer refuses to choose a timeline origin automatically.

Clock status values: `UNVERIFIED`, `CANDIDATE`, `VERIFIED_FOR_THIS_SESSION`, `INVALID`.
Initial status is UNVERIFIED. Any VERIFIED claim requires `verification_ref` pointing
to the session review. This structural requirement does NOT verify that review.
The schema intentionally accepts unavailable data for measurement; it does not
authorize feeding that data to the observer.

## 4. Acquisition rehearsal before G1 time

Use synthetic samples/markers to exercise recording, filenames, original-packet
preservation, video marker linkage and offline analysis. Run:

```bash
python -B -m pytest -q -p no:cacheprovider tests/test_stationary_calibration.py tests/test_stationary_observer.py
python -B scripts/analyze_stationary_calibration.py /path/to/trial/raw.jsonl > /path/to/trial/measurement.json
```

The second command only reads a file; redirection creates an analysis artifact.
No socket, SSH, SDK, device, launcher, or threshold selection is used by the analyzer.
The dedicated harness records telemetry at PC receipt and protocol datagrams at the
existing session socket boundary. PREPARED/CONFIRMED events are emitted only after
the existing session validates replies. These timestamps are PC observations, not
relay RPC processing timestamps. Missing/failed events remain unconfirmed.
Confirm data completeness and video synchronization before progressing beyond pilot.

## 5. Experiment matrix and per-trial procedure

No Vision-triggered Reaction, automatic MotionDecode, speaker output or Wander.
Only existing validated motion -> F04-A STOP -> telemetry. F03/F05 and all existing
guards remain active. No reverse, multiple loops, new avoidance, or new motion data.
Operator and observer reapprove each finite trial; do not auto-repeat after failure.

| Trial | Planned count | Capture duration / purpose |
| --- | --- | --- |
| A standing baseline | 5 x approximately 10 s | no commanded walking; repeated noise/drift/rate samples reveal variability, not statistical safety proof |
| B forward -> STOP | 1 pilot, inspect it, then at most 2 more (3 total) | 3 s PRE + existing finite forward condition + at least 10 s POST; extend observation if still settling |
| C turn -> STOP | 1 pilot after B review, then at most 2 more (3 total) | 3 s PRE + existing finite turn condition + at least 10 s POST |

Recording durations are calibration logistics, **not `required_hold_s`** or automatic
stationary criteria. If unexpected movement/fault occurs use the site's emergency
procedure; do not wait out the planned recording interval. Continue passive recording
only if safe. After prolonged settling mark the trial unresolved and do not resume.

Validated-condition references: `patrol/README.md`, `patrol/patrol_controller.py`,
`docs/G1_INTEGRATED_DEMO_20260926.md`. Historical validated sequence is
2 m -> turn -> 4 m -> turn -> 2 m, forward 0.30 m/s; turn command fast/slow
0.50/0.25 rad/s, existing 150-degree slow transition and 177-degree stop condition
for the nominal 180-degree turn. These are existing settings, not newly certified
safe values. Do not change them here or infer that every lower speed/new distance
is validated. The harness uses this exact existing `PatrolConfig()` profile without
motion parameter overrides. B invokes only the first forward leg; C only the turn
leg. Neither invokes the complete cycle. Review the displayed/manifest profile
before approving either trial.

For each trial, execute the following checklist through the approved procedure:

1. Verify deployment/process/lock/lease evidence and clear area; record approvals,
   exact finite command and existing settings; check physical emergency stop readiness.
2. Start independent external video and acquisition; show trial slate. Confirm changing
   raw identities and usable capture without initiating a motion as a “probe”.
3. Mark PRE. For A remain standing, end after the recording interval; MOVING and STOP
   phases are not applicable unless an actual STOP occurs. Do not invent those events.
4. For approved B/C only, execute one finite validated trial. Mark MOVING from observed
   control events; annotate physical onset separately from video.
5. Preserve actual STOP_REQUESTED, STOP_RPC_PREPARED and STOP_RPC_CONFIRMED identities,
   messages and timestamp domains. A requested STOP with lost/failed ACK is UNCONFIRMED;
   no automatic retry/reclaim/resume. Record RPC code and control fault independently.
6. Mark POST_STOP_OBSERVATION; no new Move. Record settling plus steady-state noise.
   Mark external cessation as an interval during video review, not RPC-derived truth.
7. Mark END, archive/hashes, review completeness, faults and physical footage before
   another trial. New epoch/session/generation invalidates the prior observer window.

## 6. Clock and signal questions to answer

Odom: preserve `header.stamp` and integer stamp, determine actual unit and clock
domain from deployed SDK/producer definitions and measurements; inspect deltas,
monotonicity, update rate, duplicate/backwards behavior, estimator freeze, reset and
restart behavior. Nominal nanosecond encoding does not establish a monotonic clock
or synchronization with PC. No forced restart while moving for this experiment.

LowState: retain uint32; inspect increment pattern/rate, units, duplication and
relation to arrival jitter. Negative deltas are wrap OR reset/reorder candidates,
not automatic unwrapping. Record theoretical modulus 2^32; period is 2^32 / measured
ticks-per-second only if that rate/unit is established. Do not wait for or claim an
unobserved wrap. Reboot/reset observations belong to a separately authorized stationary
session boundary; new clock mapping and observer start are required afterward.

Gyro: verify deployed source definition, unit, axis/frame and norm interpretation.
Compare standing noise/body sway, forward STOP and turn STOP. Do not infer rad/s from
the variable name. Odom yaw and LowState signals are complementary, not proven
independent. Measure x/y/yaw drift, gaps, jumps/freeze, post-STOP settling and external
residual translation/rotation. Never deliberately induce an unsafe slide/freeze.

Compare source deltas with PC receive-monotonic deltas offline. Analyze apparent
scale, receive-relative offset trend, residual jitter, missing/duplicate/backwards
steps and discontinuities. Analyze continuous segments separately, retaining segment
boundaries and excluded data. Endpoint fits in the tool are descriptive; one-way
network delay and true clock offset are not identifiable from receive times alone.

### Manual VERIFIED_FOR_THIS_SESSION decision

Require a named reviewer and immutable evidence reference covering:

- producer/SDK/firmware unit and clock semantics, identity progression/wrap/reset rules;
- a justified mapping method into the explicit evaluation clock, including independent
  synchronization/bounded latency evidence, uncertainty budget and valid time interval;
- observed residuals/jitter/drop/reorder, absence or segmentation of discontinuities,
  and limits of extrapolation; apparent delta agreement alone is insufficient;
- STOP boundary and source baseline acquisition with clock-domain provenance;
- invalidation on restart/clock jump/identity change and unavailable data handling.

No tool automatically sets VERIFIED. `clock_id` plus plausible numbers is not
verification. If source age cannot be bounded, leave mapping UNVERIFIED/INVALID;
collect measurements but do not run a confirming observer evaluation. Production
uncertainty tolerances are not selected in this preparation.

## 7. Outputs and observer replay

Raw analyzer output includes sample/record count, capture span, receive interval
min/p50/p95/max, raw odom/tick delta statistics, duplicates/backwards/missing,
apparent source-rate and receipt-fit residual timeline, first-sample planar excursion
in raw units, raw gyro norm distribution, raw yaw series and normalized yaw excursion
only when explicit reviewed radians are supplied. Shortest-delta yaw can alias >pi
per sample; sparse/invalid series need manual segmentation and cannot prove motion absence.
STOP timelines use **PC confirmation receipt**, not the unobserved relay commit instant.
External marker relative times include their uncertainty/provenance. Whole-trial
metrics include PRE/MOVING/POST; compare phases using the labeled timeline, not a
whole-trial maximum as a stationary threshold. Missing values remain visible and
no measurement is called safe/recommended. Full raw files remain the source of truth.

The acquisition envelope above and the existing observer replay are two layers:

1. Archive raw envelope/sidecars unchanged, including unknown units/clocks.
2. After manual verification, create a NEW derived observer JSONL plus conversion
   manifest (input hashes, review ref, method, source-to-evaluation mapping, uncertainty,
   valid interval, units/frame, selected record indices, STOP baseline provenance).
3. Map actual STOP context to observer `start`: four UUIDs, STOP_RPC_CONFIRMED,
   MOVEMENT_HELD, actual owner_valid/control_fault, clock_id, verified commit_at and
   raw source baselines. Do not derive owner_valid solely from a nonempty session ID.
4. Map verified normalized x/y/yaw/gyro and actual raw stamp/tick to each `sample`;
   `received_at` is PC receive time, `odom_source_at`/`imu_source_at` are separately
   verified generation times. Require both after commit and strictly advancing IDs.
   Align genuinely new observations from both sources; raw snapshot duplicates are
   not extra evidence. Do not use unwrapped tick to conceal a wrap from the observer;
   wrap/reset requires a new explicitly established window.
5. Preserve silence as `evaluate` events and movement/fault/identity change as
   `invalidate` or changed context. Historical `result()` is not a liveness check.
6. Only with complete verified input and explicit reviewer-supplied config run:

```bash
python -B scripts/analyze_stationary_telemetry.py /path/to/derived-observer.jsonl --config /path/to/explicit-candidate.json
```

Do not copy raw UDP or the acquisition envelope directly into that evaluator. Unknown
source times must remain unavailable and result in INVALID_EVIDENCE, not guessed
monotonic numbers. No automated converter is supplied because source semantics are
not yet verified. Both analyzers are file-only and never write a production config.

## 8. After-session decisions (in order)

1. Can odom clock semantics/mapping/uncertainty be established? If NO, no integration.
2. Can tick and gyro semantics be established? If NO, record evidence gaps; review
   alternative signals/design separately, never silently omit a required observer input.
3. Compare standing vs moving/settling distributions, including transient maxima and
   sample loss; three trials are an initial survey, not sufficient safety statistics.
4. Compare metrics and STOP timing to independent physical video with uncertainty.
   Include potential slow slide/frozen-estimator ambiguity and observable resolution.
5. Only now may a human reviewer propose candidate thresholds, with documented rationale.
6. Validate candidates on separate held-out trials/replays, including failures; if
   needed plan more authorized acquisition rather than tuning to the same samples.
7. Only after that separately review Reaction gate integration and current-evidence
   lifecycle. Physical stationary production gate remains OPEN until then.

If slow slide, plausible estimator freeze and actual stationary cannot be distinguished:
**DO NOT PICK A THRESHOLD**. Investigate an additional independent signal or alternate
strategy. Advancing stamps plus plausible frozen pose/low gyro is not physical proof.

## 9. TEST HYGIENE TODO (separate task)

`test_audio_and_motion_wait_for_patrol_stop_confirmation` fails intermittently at
the AUDIO-before-motion append ordering assertion. Reproduced on base `90fa3e7`;
F04-B only changed a timeout log string on that production path. Hypothesis: independent
audio/motion workers can append in either order once the common STOP barrier releases.
Minimum test-only candidate: retain blocked-STOP assertions, synchronize worker
completion with Events, assert STOP confirmation precedes BOTH starts and both occur;
do not impose AUDIO-before-motion unless it is an actual contract. No sleep inflation,
production serialization or removal of STOP-barrier coverage. Not changed here.

## 10. Session GO sheet

- [ ] Expected source pins and module hashes verified on running deployment.
- [ ] Old/unknown writers absent; canonical lock inode/mount and owner checked.
- [ ] v4 client/relay and F03 lease healthy; emergency procedure and clear area ready.
- [ ] Reaction/MotionDecode/Wander OFF; reviewed finite single-trial control procedure
      preserves those layers (exact command/hash recorded); harness review complete.
- [ ] Capture rehearsal passes; raw provenance, protocol events and independent video
      available; operator approvals recorded for this trial only.

An unchecked box means no first motion. This runbook is preparation, not hardware
approval. Offline safety architecture work stops here; verify the actual deployed
pair, captured fields and physical environment before scheduling motion calibration.

## 11. Dedicated harness command skeletons (future authorized session only)

Do NOT execute these as part of offline development. Replace every placeholder
with operator-verified values; the harness does not deploy/start the G1 relay,
LiDAR relay, SDK or any other process. Relay telemetry must be directed to the
exclusive PC receiver endpoint. Do not run another consumer on that bind/port.

Standing capture (no locomotion socket/session, no robot command):

```bash
"$PYTHON" -B scripts/run_stationary_calibration.py \
  --mode standing --execute --session-id "$SESSION_ID" --trial-id A01 \
  --output "$EXTERNAL_DATA_ROOT" \
  --telemetry-bind "$PC_IPV4" --telemetry-port "$TELEMETRY_PORT" --telemetry-peer "$RELAY_IPV4" \
  --pre-seconds 5 --post-seconds 5 --trial-timeout 20 \
  --external-video "$VIDEO_FILE" --stdin-markers
```

One forward trial (replace forward-stop with turn-stop and a new ID for C):

```bash
G1_ALLOW_REAL_ACTION=1 "$PYTHON" -B scripts/run_stationary_calibration.py \
  --mode forward-stop --execute --enable-real-robot --confirm-site-ready \
  --operator-approved-calibration --confirm-external-recording \
  --session-id "$SESSION_ID" --trial-id B01 --output "$EXTERNAL_DATA_ROOT" \
  --telemetry-bind "$PC_IPV4" --telemetry-port "$TELEMETRY_PORT" --telemetry-peer "$RELAY_IPV4" \
  --relay-host "$RELAY_IPV4" --relay-port "$COMMAND_PORT" \
  --lidar-bind "$PC_IPV4" --lidar-port "$LIDAR_PORT" \
  --pre-seconds 3 --post-seconds 10 --trial-timeout "$APPROVED_TOTAL_TIMEOUT_SECONDS" \
  --external-video "$VIDEO_FILE" --stdin-markers
```

The total deadline is an explicitly approved finite recording/trial duration,
not a replacement STOP latency guarantee. It must exceed PRE + POST and allow the
existing finite movement. No speed/distance/yaw CLI overrides exist. `--loops` only
accepts 1; `--reverse` is rejected. No approvals are synthesized. All explicit
motion approvals, video confirmation and environment gate are required before I/O.

While running, type `m` then Enter to append an
`OPERATOR_EXTERNAL_STATIONARY_MARK` timestamp. This local input cannot send robot
commands. It is an operator observation, not physical proof; it does not end POST
recording or authorize movement. Link its timing uncertainty to video afterward.
EOF, unavailable stdin/terminal or input parsing exceptions disable only marker input.
An OPERATOR_MARKER_STATUS record reports `operator_marker_status=unavailable` and
`operator_marker_error`. The trial continues on its unchanged control path; marker
loss does not change the external-camera requirement or any operator approval.
Actual output/queue failure is still recorder-fatal, including if discovered while
writing a marker diagnostic. The input thread never renews or mutates the lease.
SIGINT/SIGTERM abort the trial through the existing STOP path; no automatic retry.

Output: `$EXTERNAL_DATA_ROOT/$SESSION_ID/$TRIAL_ID/` containing `canonical.jsonl`,
`manifest.json`, `operator_notes.txt`. Existing trial directories are rejected.
Keep output outside Git. Manifest records runtime HEAD/branch, Python, protocol v4,
ownership contract v1, full existing profile and CLI, clock ID, video path and blank
deployment-evidence fields for later SDK/hash/process evidence. Initial clock status
is UNVERIFIED and no observer evaluation occurs.

Canonical `event` adds TRIAL_START, PRE, TRIAL_ACTIVE, MOVEMENT_ENABLE, MOVING, STOP_REQUESTED,
STOP_RPC_PREPARED, STOP_RPC_CONFIRMED, POST_STOP_OBSERVATION,
POST_STOP_OBSERVATION_COMPLETE, CONTROLLED_TEARDOWN,
OPERATOR_EXTERNAL_STATIONARY_MARK and TRIAL_END. Initial resume's required STOP uses
`command_scope=initialization`; the analyzer excludes it when choosing the primary
STOP timeline origin. The normal lifecycle is primary STOP_RPC_CONFIRMED ->
POST_STOP_OBSERVATION (lease active, same owner, HELD, no Move) ->
POST_STOP_OBSERVATION_COMPLETE -> flush -> CONTROLLED_TEARDOWN -> quiesce the
lease-checking worker -> ordinary controller.stop() -> close control/receiver
resources -> TRIAL_END -> final recorder close. Cleanup STOP uses
`command_scope=cleanup` and is also excluded from the primary timeline origin.
Normal motion trials therefore have initial, primary and cleanup transactions;
cleanup occurs only after required observation completes. Expected shutdown does
not use lease expiry. The final TRIAL_END outcome is CONTROL_CLOSED, not a physical
stationary claim or a guarantee that the subsequent file close cannot fail; any
final close exception still returns trial failure. Cleanup attempts are independent
so an adapter/receiver close error cannot skip recorder closure.
Raw send/receive events retain even rejected replies; only validated replies produce
PREPARED/CONFIRMED. MOVING means first command attempt, not physical motion onset.
An interrupted/failed trial produces TRIAL_FAILURE when the file remains writable.

The bounded recorder queue preserves datagrams as base64 plus canonical values.
Malformed packets have TELEMETRY_INVALID records with original bytes. No unwrap,
unit conversion or clock verification is performed. The main supervisor checks
flush acknowledgements before renewing its lease. Open/schema/initial flush failure
prevents all sockets; queue overflow, write failure, flush timeout or receiver failure
latches recording failure, inhibits future Move/enable, and attempts existing STOP.
STOP recording failure cannot suppress the actual STOP transaction. Some records
may be lost after fatal disk failure; that trial fails and no completeness claim is
made. OS/process death, blocked SDK, physical stopping distance and source-clock
truth are not newly guaranteed. The relay's existing watchdog remains unchanged.

Before first motion explicitly sign off: deployed SHA/hash (including new harness,
recorder and controller public entrypoint), old writer absence, ownership lock
availability, Control Lease health, v4 STOP pair, recorder running, external camera
recording, Reaction OFF, single finite parameters and operator approval. The original
F04-B hash manifest remains a historical baseline; controller now has an added public
entrypoint, so generate/verify a deployment manifest from the reviewed harness commit
rather than treating its historical controller hash as the new deployed hash.
