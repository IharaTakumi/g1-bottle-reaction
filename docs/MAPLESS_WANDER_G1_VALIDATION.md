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

Replay uses one JSON object per line:

```json
{"timestamp":1.0,"odom":{"x":0,"y":0,"yaw":0},"obstacle_snapshot":{"left":4,"front_left":4,"front":2,"front_right":4,"right":4}}
```

`points: [[x,y,z], ...]` may replace `obstacle_snapshot`. These commands reject
real-robot and real-navigation enable flags and never instantiate a robot
adapter.

## STEP 3: ONE-SHOT LOCOMOTION

Only after STEP 2 passes, use a separately reviewed, explicitly armed motion
path in a wide controlled area. Verify one command at a time: very-low-speed
forward, short left turn, short right turn, then stop. Record command direction,
latency, and actual stopping behavior. The daytime shadow runtime does not
provide this command path.

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
