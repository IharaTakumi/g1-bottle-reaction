# G1 static SLAM mapping test — 2026-09-12

Overall: **PARTIAL**

Classification: **MAP-B**. API 1801 started mapping and mapping odometry became live,
but `rt/unitree/slam_mapping/points` and both observed MID-360 streams remained silent.

## Before 1801

- `rt/slam_info`: 40 samples / 8.0 s, approximately 5 Hz. `ctrl_info` was
  `state=ready`, `info=not init`, `ctrName=not init`, `errorCode=0`.
- `rt/unitree/slam_mapping/odom`: writer matched, 0 samples.
- `rt/unitree/slam_mapping/points`: writer matched, 0 samples.
- `rt/utlidar/cloud_livox_mid360`: writer matched, 0 samples.
- `rt/utlidar/imu_livox_mid360`: writer matched, 0 samples in the same live reader before 1801.
- `rt/dog_odom`: writer matched, 0 samples.

The PC2 recorder was started before the RPC. A Desktop recorder used one participant,
performed 20 seconds of discovery, created all allowlisted readers, and was receiving
`slam_info` before 1801.

## 1801

- Service: `slam_operate`, version `1.0.0.1`, timeout 10.0 s.
- Request: `{"data":{"slam_type":"indoor"}}`.
- Response: `rpc_code=0`, `succeed=true`, `errorCode=0`,
  `info="Successfully started mapping."`.
- RPC elapsed time: 0.326776 s.
- Retry: **NONE**.

## After 1801

- `rt/slam_info`: `type=mapping_info`, `errorCode=0` became live after 0.407453 s.
- `rt/unitree/slam_mapping/odom`: 795 samples between 1801 and 1802, 9.97 Hz,
  first sample latency 0.406959 s, `frame_id=map`, `child_frame_id=base_link`.
- First position: `[-0.000702, 0.000167, -0.000227]` m; last position:
  `[-0.004781, 0.003335, 0.001609]` m.
- Maximum displacement from the first pose: 0.01186 m. Maximum yaw difference:
  0.01259 rad. Twist values observed in Desktop summaries were zero.
- `rt/unitree/slam_mapping/points`: writer matched, 0 samples during the 40 s
  Desktop post-start observation coverage.
- `rt/utlidar/cloud_livox_mid360`: writer matched, 0 samples.
- `rt/utlidar/imu_livox_mid360`: writer matched, 0 samples.
- `rt/dog_odom`: writer matched, 0 samples.

The start-to-save interval was 80.11 s. This exceeded the requested 20–30 s window;
the reader and robot remained in the same stationary test condition, and no RPC was
repeated or added.

Physical motion observed: **NONE reported**. The telemetry stayed within 1.19 cm and
0.72 degrees of the initial mapping pose. The agent did not have an independent camera
view of the robot.

## Mapping shutdown

- 1802 request:
  `{"data":{"address":"/home/unitree/g1_20260912T120249Z_5ace6ff3c9844d63b66c9805e76ebc08.pcd"}}`.
- Response: `rpc_code=0`, `succeed=true`, `errorCode=0`,
  `info="Save pcd successfully."`.
- RPC elapsed time: 0.097670 s.
- Retry: **NONE**.
- Existing map overwritten: **NO**. The request used a UUID-derived new path. The
  server API has no atomic no-clobber option, so prior nonexistence was not independently
  provable from PC2.
- After 1802, mapping odometry and `mapping_info` both produced 0 further samples.
- Final `ctrl_info`: `state=ready`, `info=not init`, `ctrName=not init`, `errorCode=0`.

## Interpretation

1801 is the trigger that activates the mapping odometry producer on this G1. Raw
`dog_odom` is not required to obtain a live mapping pose once a mapping session starts.
The mapping point output and MID-360 cloud/IMU output did not activate, so the mapping
pipeline is only partially confirmed. This run did not distinguish a silent point-cloud
publisher from a post-start large-sample DDS delivery problem because no post-start pcap
was taken. The contents and size of the saved PCD on the `.161` server were also not read.

## Recommended next action

Establish read-only shell or vendor diagnostic access to `192.168.123.161` and inspect
the `lidar_driver`/SLAM point-cloud producer state and logs without starting another
mapping session or publishing to `rt/utlidar/switch`.

## Evidence

Local logs are under `.runtime/static-slam-mapping-20260912/`. `results.json` contains
the derived metrics; `pc2-session/start.*`, `save.*`, and `telemetry.jsonl` are the
unaltered PC2 evidence copied before the temporary `/dev/shm` runtime was removed.

## Commands sent to G1

- RPC 1801: **exactly 1**
- RPC 1802: **exactly 1**
- Navigation command: **NONE**
- Motion command: **NONE**
- Joint command: **NONE**
- LiDAR switch publish: **NONE**
