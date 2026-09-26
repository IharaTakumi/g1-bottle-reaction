# 2026-09-26 Integrated Demo

## Architecture

- G1 camera / YOLO -> Reaction Engine
- Patrol -> odometry, IMU, LiDAR guard, locomotion relay
- MotionDecode resident worker -> stationary upper-body reaction
- Patrol and Reaction remain separate processes; wander is off.

## Validated on G1

- One complete `2 m -> 180 deg -> 4 m -> 180 deg -> 2 m` Patrol loop.
- Odometry closed-loop forward legs and IMU yaw closed-loop turns.
- Front LiDAR stop during forward motion.
- Detection -> immediate audio and Patrol stop -> MotionDecode -> q0 return / weight 0 -> resume from the remaining distance or angle.
- Reaction interruption during both forward and turn stages.
- Transport-only telemetry stale -> immediate stop -> bounded recovery (maximum 1.0 s) while preserving progress. In the completed loop, 14 of 14 gaps recovered.
- Low-latency person FOUND and plushie SURPRISE paths on the real robot.

## Reaction Mapping

```text
person  -> FOUND    / motiondecode:found
banana  -> SURPRISE / motiondecode:surprise
plushie -> SURPRISE / motiondecode:surprise
```

## Safety Invariants

- MotionDecode's arms-stationary gate is unchanged.
- IMU or odometry source stale is a final stop.
- Transport-only stale stops locomotion immediately and permits at most 1.0 s for recovery.
- MotionDecode must complete, return to q0, and release to weight 0 before Patrol resumes.
- The locomotion relay watchdog remains 0.40 s.
- LiDAR, IMU, odometry thresholds and Patrol speeds remain unchanged.
- Reverse is disabled and wander is off.

## Operational Notes

- After a G1 reboot, `g1-teleop-client` did not automatically reconnect. Existing wired access was used and `nmcli connection up g1-teleop-client` restored `10.42.0.76`; no new NetworkManager profile was created.
- The built-in camera depends on RealSense device enumeration and `videohub_pc4`. Verify `/dev/video4`, `videohub_pc4`, and changing G1-local `VideoClient.GetImageSample()` hashes after reboot.
- The working production camera path is G1-local `videohub_pc4` -> G1-local `VideoClient` -> SSH/JPEG -> Ubuntu. Ubuntu direct VideoClient may return 3102 and is not the production path.
- A later plushie SURPRISE run validated audio, stop, upper-body motion, q0 return, weight 0, and resume. That run subsequently fail-closed during a 4 m leg when odometry transport did not recover within 1.0 s; no automatic retry was performed.
