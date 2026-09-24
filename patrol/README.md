# G1 Patrol（実機完成版）

G1内蔵SLAM odometryで直進距離を閉ループ制御し、LowState IMU yawで
180°旋回を閉ループ制御する、前進専用Patrolです。操作PCとG1側PCの間は
LiDAR Guardとlocomotionを軽量UDP relayで接続します。

## 本番既定値

- forward speed: `0.30 m/s`（上限 `0.30 m/s`）
- TURN fast / slow: `0.50 / 0.25 rad/s`（上限 `0.50 rad/s`）
- TURN slow切替: `150°`、停止: `177°`、timeout: `20 s`
- distance tolerance: `0.05 m`、leg settle: `0.20 s`
- lateral fail-safe: `0.80 m`
- locomotion watchdog: `0.40 s`
- reverse禁止、無限loop禁止
- 実機目安: 約1分 / loop

直進距離とloop数はCLIで変更できます。FORWARD中はFront Guardの
`BLOCKED`で停止し、`CLEAR`で残距離を再開します。TURN中はFront
`BLOCKED`を適用しませんが、LiDAR / IMU / odometry stale、watchdog、
例外では常に停止します。十分なコース横幅と旋回範囲を確保してください。

## 最短起動手順

G1側PCでLiDAR relayを起動します。

```bash
cd /home/unitree/g1-bottle-reaction-wander
.venv-wander/bin/python -B /home/unitree/g1-patrol-relays/lidar_guard_relay.py \
  --interface eth0 --destination 10.42.0.1 --port 47621 --seconds 3600 \
  --guard-path /home/unitree/g1-patrol-relays/lidar_guard.py
```

別端末でG1側locomotion relayを起動します。

```bash
cd /home/unitree/g1-bottle-reaction-wander
.venv-wander/bin/python -B /home/unitree/g1-patrol-relays/locomotion_relay.py \
  --bind 10.42.0.76 --port 47622 --interface eth0 \
  --watchdog-timeout 0.40 \
  --telemetry-host 10.42.0.1 --telemetry-port 47623 \
  --arm --start-mapping-if-needed
```

操作PCで、明示承認済みの有限loopだけを開始します（例は3 loop）。

```bash
python patrol/run_patrol.py \
  --mode real --lidar-source relay \
  --relay-bind 10.42.0.1 --relay-port 47621 \
  --locomotion-relay-host 10.42.0.76 --locomotion-relay-port 47622 \
  --forward-distance 2.0 --return-distance 4.0 --home-distance 2.0 \
  --forward-speed 0.30 --turn-yaw-rate 0.50 \
  --max-lateral-drift 0.80 --heading-hold \
  --loops 3 --arm --one-cycle --operator-approved-one-cycle
```

終了時はPatrolが`StopMove()`を送ります。Patrol後はG1側relayも終了し、
このrelayが開始したSLAM sessionだけを閉じてください。
