# Mapless Wander v1: G1 validation order

The Windows implementation is a decision-only shadow runtime. Its default
axis, height, self-mask, speed, and clearance values are tuning placeholders,
not verified G1 safety facts. Do not skip a step. If any check is abnormal,
stop at that step.

## STEP 1: READ ONLY

Do not move G1. Receive the MID-360 point-cloud topic and `rt/dog_odom` only.
Confirm timestamp freshness, the actual forward/left/right/up axes, the
LiDAR-to-base relationship, and where G1 appears in its own cloud. Update the
YAML axis signs, usable height band, minimum range, and self mask from those
observations. Confirm each physical obstacle lands in the matching shadow
sector.

On PC2, from the repository and its existing robot virtual environment:

```bash
source .venv-robot/bin/activate
python scripts/g1-wander-live-source.py \
  --interface eth0 \
  --seconds 20 \
  --debug-axis
```

This process creates DataReaders only for `rt/utlidar/cloud_livox_mid360` and
`rt/dog_odom`. It has no application DataWriter, RPC client, or locomotion
client. The checked-in `config/wander_live_pc2.json` values are unverified
placeholders. Inspect `raw_xyz_min/max`, sector counts, and nearest xyz before
changing them. Do not infer the real axes from their default names.

## STEP 2: LIVE SHADOW

Still do not move G1. Place a person, chair, and wall in turn at the front,
left, and right. Observe only `FORWARD`, `TURN_LEFT`, `TURN_RIGHT`, or `STOP`,
the five clearances, sensor freshness, and the decision reason. Invalid,
missing, or stale data must produce `STOP`.

Offline commands available before wiring a read-only live source:

```powershell
python -m g1_bottle_reaction.main --wander-shadow --wander-seed 11
python -m g1_bottle_reaction.main --wander-replay .runtime\wander.jsonl --wander-seed 11
```

For the actual read-only pipe, replace `<pc2-host>` and the repository path for
the venue environment; do not put passwords in the repository:

```powershell
ssh <pc2-host> "cd ~/dev/g1-bottle-reaction && source .venv-robot/bin/activate && python scripts/g1-wander-live-source.py --interface eth0 --debug-axis" |
  python -m g1_bottle_reaction.main --wander-live --wander-seed 11 --wander-live-debug --wander-record .runtime\wander_live_20260917.jsonl
```

The desktop prints cloud/odometry age, all five clearances, safety state,
action, and reason. Malformed input, missing data, stale data, an unexpected
exception, or SSH EOF is shown as `ACTION=STOP`. Stop here unless the physical
front/left/right placements agree with their sectors and every failure test is
fail-closed. The record is directly accepted by `--wander-replay`.

Replay uses one JSON object per line:

```json
{"timestamp":1.0,"odom":{"x":0,"y":0,"yaw":0},"obstacle_snapshot":{"left":4,"front_left":4,"front":2,"front_right":4,"right":4}}
```

`points: [[x,y,z], ...]` may replace `obstacle_snapshot`. These commands reject
real-robot and real-navigation enable flags and never instantiate a robot
adapter.

## STEP 3: ONE-SHOT LOCOMOTION

Only after STEP 2 passes, use the independent one-shot harness in a wide,
controlled area. First copy/paste these dry runs; they initialize no SDK and
send no command:

```bash
python scripts/g1-wander-loco-once.py --action forward --speed 0.05 --duration 0.30
python scripts/g1-wander-loco-once.py --action turn-left --speed 0.05 --duration 0.30
python scripts/g1-wander-loco-once.py --action turn-right --speed 0.05 --duration 0.30
python scripts/g1-wander-loco-once.py --action stop
```

Each must print `DRY RUN` and `NO G1 COMMAND SENT`. For a real single command,
all four gates are required. Add them only after the operator has cleared the
area and selected exactly one action:

```bash
python scripts/g1-wander-loco-once.py \
  --robot g1 \
  --enable-real-robot \
  --execute-real-g1 \
  --i-understand-this-will-move-the-robot \
  --action forward \
  --speed 0.05 \
  --duration 0.30
```

Repeat as separate processes for `turn-left`, `turn-right`, then `stop`. Never
paste all real commands as a batch. The allowlist has no backward, side-step,
continuous, loop, follow, or combined translation/rotation action. Hard limits
are 0.10 m/s linear, 0.25 rad/s angular, and 0.50 s duration; out-of-range CLI
values are rejected. One invocation sends at most one `SetVelocity` or
`StopMove` mutation and never retries, including RPC timeout 3104.

The duration field and an RPC return are not proof of physical stop. Observe
actual direction, start latency, the explicit `stop` action, physical stopping
latency, and stopping distance. Stop the validation if any is uncertain. The
one-shot harness is deliberately not connected to `WanderDecision`.

## STEP 4: REACTIVE AVOIDANCE

Disable randomness. Verify only: forward, obstacle, stop, turn toward the clear
side, then re-check. Do not continue if stop latency or direction is uncertain.

## STEP 5: WANDER

Enable random bias, short trail penalty, and soft leash only after the reactive
sequence is reliable. Tune distances at low speed in the actual venue; do not
treat the YAML defaults as certified limits.

## STEP 6: GAME INTEGRATION

Finally verify `WANDER -> person -> STOP/PAUSE -> Reaction -> resume WANDER`.
Keep continuous tracking outside the Reaction Engine and retain the existing
NavigationCoordinator pause-ownership checks.

## Record during validation

- MID-360 axes and transform evidence
- observed G1 self-reflection bounds
- point-cloud and odometry timestamp behavior
- one-shot direction and stop latency
- lowest safe speed and observed clearance margin
- the exact YAML values used for the successful run
