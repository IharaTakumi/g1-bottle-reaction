# G1 static zero-pose localization test — 2026-09-12

Overall: **PASS**

## Safety prerequisite

- Same static map: **YES**
- G1 confirmed unmoved since mapping: **YES**, confirmed by the operator immediately before the call
- Navigation commands enabled: **NO**
- Read-only recorders were running before API 1804.
- The dedicated command path rejects a second 1804 attempt, including attempts through
  the normal mapping-seed relocation path.

## Input map

PCD:

`/home/unitree/g1_20260912T120249Z_5ace6ff3c9844d63b66c9805e76ebc08.pcd`

Initial pose strategy: **Unitree example origin pose**, limited to this same-place,
same-orientation static test.

```text
x=0 y=0 z=0
qx=0 qy=0 qz=0 qw=1
```

The prior mapping was made without moving the G1 and its observed mapping pose remained
near the origin. The operator confirmed that the G1 had not subsequently moved or
rotated. The old mapping-odometry seed was deliberately not used, and its normal
300-second freshness limit was not weakened.

## Before 1804

The continuous PC2 recorder and a separate Desktop reader observed:

- `rt/slam_info`: `state=ready`, `info=not init`, `ctrName=not init`, `errorCode=0`
- `rt/unitree/slam_relocation/odom`: 0 samples
- `rt/slam_info` `pos_info`: 0 samples
- `rt/unitree/slam_relocation/points`: matched writer, 0 samples
- `rt/unitree/slam_relocation/global_map`: matched writer, 0 samples

The PC2 boot ID matched the mapping session, recorder telemetry was fresh, and there
were zero pre-existing relocation attempt files.

## 1804

Service: `slam_operate`

Version: `1.0.0.1`

Timeout: 10 seconds
Send count: **exactly 1**

```json
{
  "api_id": 1804,
  "parameter": {
    "data": {
      "address": "/home/unitree/g1_20260912T120249Z_5ace6ff3c9844d63b66c9805e76ebc08.pcd",
      "x": 0.0,
      "y": 0.0,
      "z": 0.0,
      "q_x": 0.0,
      "q_y": 0.0,
      "q_z": 0.0,
      "q_w": 1.0
    }
  }
}
```

Response:

```json
{
  "rpc_code": 0,
  "response": {
    "succeed": true,
    "errorCode": 0,
    "info": "Successfully started re-location.",
    "data": {}
  }
}
```

RPC elapsed time: 0.538 seconds. Retry: **NONE**.

One `relocate-static-zero.attempt.json` and one matching result file exist. The command
guard makes the normal and static relocation attempt paths mutually exclusive.

## After 1804

PC2 observed for 48.43 seconds after the send timestamp:

- Relocation odometry: 475 samples over a 47.53-second live span, **9.972 Hz**
- First relocation odometry latency: **0.900 seconds**
- `slam_info` `pos_info`: 475 samples; first latency **0.901 seconds**
- `slam_info` errors after 1804: **0**
- Odometry frames: `map` -> `base_link`
- Maximum position drift from the first pose: **0.00971 m**
- Maximum yaw drift from the first pose: **0.00217 rad**

First odometry pose:

```text
position=(0.005787, -0.021979, 0.046854)
quaternion=(-0.001168, 0.012937, -0.001461, 0.999915)
yaw=-0.002952 rad
```

Last odometry pose:

```text
position=(0.009354, -0.020917, 0.044372)
quaternion=(-0.001356, 0.012623, -0.001290, 0.999919)
yaw=-0.002614 rad
```

Every captured `pos_info` identified the expected map. The first one contained:

```text
pcdName=g1_20260912T120249Z_5ace6ff3c9844d63b66c9805e76ebc08
address=/home/unitree/g1_20260912T120249Z_5ace6ff3c9844d63b66c9805e76ebc08.pcd
errorCode=0
```

The `ctrl_info` state-machine text remained `state=ready`, `info=not init`, and
`ctrName=not init`. The positive state change was the appearance of continuous
`pos_info` and relocation odometry tied to the requested PCD; this firmware did not
rename the `ctrl_info` state during the observation.

The Desktop reader independently counted 338 relocation odometry samples during the
portion of its fixed 60-second window after 1804. Both point-cloud topics had a matched
writer but remained silent:

- `rt/unitree/slam_relocation/points`: 0 samples
- `rt/unitree/slam_relocation/global_map`: 0 samples

No navigation, motion, joint, or LiDAR command was sent. Telemetry was consistent with
a stationary robot: all reported odometry twists were zero and pose drift stayed below
1 cm. Direct visual observation is available only to the local operator.

## Map usability

PCD accepted by SLAM service: **YES**

The service explicitly returned success, and both continuous relocation odometry and
continuous `pos_info` began about 0.9 seconds later. `pos_info` named the exact PCD path.
This is strong evidence that the saved PCD was present, readable, and usable by the
SLAM service despite silent outward point-cloud topics.

## Classification

**LOC-A — Static Localization succeeded.**

API 1804 succeeded, `slam_info` began publishing map-specific `pos_info`, and
relocation odometry remained live at about 10 Hz. The unchanged `ctrl_info` labels are
a firmware behavior observed alongside the successful localization outputs.

## Interpretation

Confirmed:

```text
1801 Mapping
-> mapping odom
-> 1802 Save
-> 1804 Load/Localization
-> relocation odom + pos_info
```

Still unknown: localization tracking while the robot is manually moved, and why the
relocation point-cloud publications remain silent.

Localization session remains active because a verified session-specific shutdown API
was not available. API 1901 was not sent because the documented implementation closes
the complete SLAM node and is not established as a relocation-session-only shutdown.

## Recommended next action

With at least two people present, perform one controlled, low-speed manual movement by
controller while recording whether relocation odometry and `pos_info` track the motion.

## Commands sent to G1

- 1804: **exactly 1**
- Other SLAM RPC: **NONE**
- Navigation: **NONE**
- Motion: **NONE**
- Joint: **NONE**
- LiDAR switch: **NONE**
