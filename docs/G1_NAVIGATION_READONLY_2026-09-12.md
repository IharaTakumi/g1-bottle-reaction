# G1 SLAM read-only check — 2026-09-12

追加切り分け結果: [G1_NAVIGATION_READONLY_FOLLOWUP_2026-09-12.md](G1_NAVIGATION_READONLY_FOLLOWUP_2026-09-12.md)。

総合 **FAIL（Odometry・LiDARの実データ受信が未成立）**。
NetworkとSLAM状態配信はPASS。機器故障・サービス停止を断定する結果ではない。
Mapping試験へ進む条件は未達。今回G1へ送信したwrite/RPC/motion command: **NONE**。

## 実行範囲

- 開始時は `main`、未コミット変更なし。
- 指定された6文書、`g1-slam-session.py`、既存受信診断・SDK型loader・関連テストを静的に確認。
- Ubuntuの既存 `.venv-g1`（Python 3.12、CycloneDDS 0.10.2、Unitree SDK 1.0.1）を利用。
- 今回の依頼に従い既存Desktop Ethernet受信診断を使用。将来の操作用PC2 runtimeの配備・実行を検証したものではない。
- `g1-slam-session.py`、RobotAdapter、NavigationCoordinator、本番RPC clientは実機診断で起動していない。
- アプリケーションDataWriter/Publisherなし。終了時の制御・cleanup RPCもなし。
- ping、DDS discovery/購読に必要なプロトコル通信、multicast加入に伴うOS通信は発生し得る。
- SSH、G1へのファイル転送、サービス操作、ネットワーク設定変更、依存導入、sudoは実施していない。
- ローカルログ・テストキャッシュはリポジトリ配下。既存venv・vendor SDK・リアクション・YOLO・音声のコードは変更していない。

## Network

- NIC: `enp129s0`、UP/LOWER_UP、Ubuntu IPv4: `192.168.123.200/24`。
- 過去の `.99` と異なるが同一サブネットで到達可能。IPを復元・変更する必要はなかった。
- `.161`: ping 5/5、損失0%、RTT平均0.200 ms。
- `.164`: ping 5/5、損失0%、RTT平均0.211 ms。
- DDS domain 0の参加・発見に成功。補助の `rt/lowstate` は10秒で801件、異なるtick 801個。
- 終了時もNIC状態・IPv4アドレス・経路は開始時と一致。NICの累積RX/TX errors/droppedは0。
- 短時間の到達性とDDSデータ受信経路は確認できた。長時間の安定性や無受信topicの原因までは保証しない。

## 最終観測（discovery 20秒＋購読15.0149秒）

| Topic | matched writer | 受信数 | 観測受信レート | 結果 |
| --- | ---: | ---: | ---: | --- |
| `rt/dog_odom` | 1 | 0 | 0 Hz | FAIL: pose/position/orientation/yaw/ドリフトは測定不可 |
| `rt/odommodestate` | 1 | 0 | 0 Hz | 代替odomも未受信 |
| `rt/lf/odommodestate` | 1 | 0 | 0 Hz | 代替odomも未受信 |
| `rt/utlidar/cloud_livox_mid360` | 1 | 0 | 0 Hz | FAIL: points/frameは測定不可 |
| `rt/slam_info` | 5 | 80 | 5.33 Hz | PASS: 状態配信の受信成立 |
| `rt/slam_key_info` | 1 | 0 | 0 Hz | この観測窓でイベント未受信 |
| `rt/unitree/slam_mapping/odom` | 1 | 0 | 0 Hz | 未確認。開始APIは送っていない |
| `rt/unitree/slam_relocation/odom` | 1 | 0 | 0 Hz | 未確認。開始APIは送っていない |
| `rt/unitree/slam_mapping/points` | 1 | 0 | 0 Hz | 未受信 |
| `rt/unitree/slam_relocation/points` | 1 | 0 | 0 Hz | 未受信 |
| `rt/unitree_slam/waypoints` | 1 | 0 | 0 Hz | 購読のみ。送信なし |

全購読でQoS不一致callbackは0。型はlive discoveryとSDK定義が一致。
レートはこの診断がtakeした件数／観測秒数であり、送信側のpublish周波数ではない。
特に0 Hzはこの受信機での観測値であり、送信側が0 Hzである証明ではない。

`slam_info` は `ctrl_info` 72件、`robot_data` 8件。両type内でsource timestampは単調増加。
最大受信間隔はctrl_info約0.403秒、robot_data約2.019秒。両typeのerrorCodeは全件0。

```text
state = ready
info = not init
ctrName = not init
errorCode = 0
isOpenPlan = false
isPause = false
is_arrived = false
```

ctrl_infoのcurrentPoseは全0だが、有効なodomやmap poseの代用にしていない。
SLAMのPASSは状態配信経路の成立を指す。RPC応答能力・localization成立の確認ではない。

初回の5秒discoveryでは117外部endpointしか発見せず、SLAM/odom publisherを取りこぼした。
初回の15秒購読は全対象0件。別の20秒discoveryでは291外部endpoint（並行する診断のreaderを含み得る）を観測。
そこで最終診断の発見窓を20秒に延長すると対象全topicを発見し、SLAM状態を受信できた。
途中のLiDAR単独再確認は5秒の発見窓でMID-360 publisherを取りこぼしたため、その無受信を接続後の測定として扱わない。
発見結果が時間窓によって変わる原因は未確定。最終診断でもdog_odomとMID-360はmatched成立後に0件だった。

## 提供host

通常UDP受信で40秒間、`239.255.0.1:7400` のRTPS header GUID prefixとsource IPを採取。
同時期のDDS endpoint participant GUIDの先頭12 bytesと照合した。

- SLAM状態publisher / `slam_operate` request subscriber・response publisher: **192.168.123.161**。
- `dog_odom` と代替odomのpublisher participant: **192.168.123.161**。
- MID-360 cloud/IMU publisher participant: **192.168.123.161**。
- `.161`由来24 prefix、`.164`由来1 prefixを観測。

odom/LiDARは実データ未受信のため、ここで確認したのはpublisher participantのネットワーク提供元。
remote PID・実行ファイル・ドライバー内部状態は未確認。GUIDの数値からIPを推測していない。

## 判定と次回

A: **Mappingへ進めない**。OdometryとMID-360実データが不足している。
ネットワーク/DDS基本通信とSLAM状態配信は成立したが、必要センサー入力の正常性は証明できていない。

B: 二人以上で行う次回試験は、まず同じ静止read-only診断でodomと点群の連続受信を成立させる。
未受信なら既存の許可された管理画面/consoleでSLAM・LiDAR・odom生成元の状態を読む。
必要に応じてPC2側でも受信を比較し、生成側かDesktop受信経路かを切り分ける。
サービス起動・再起動・設定変更が必要なら、その具体的操作を別途判断する。

受信成立およびPC2 runtime準備確認後、別セッションで以下を段階ごとに実施する。
コントローラー担当とPC/記録担当を分け、各RPC結果とtelemetryを確認して次へ進む。

1. `1801` → mapping odomの更新確認 → 人間のコントローラー操作で会場内を移動・記録。
2. 静止してposeを固定 → `1802`で新規map保存 → 保存結果を確認。
3. 保存から位置・向き・頭部姿勢を維持 → 初期poseの根拠を確認 → `1804` → relocation odomとpos_info/map identity/実位置の整合を確認。

これらのAPI対応・保存成功・初期pose座標契約・localization収束は今回未確認。
1102をこの手順へ自動連結しない。timeout時に自動再送しない。

## 変更と再実行

新しい統合診断は作らず、既存read-onlyスクリプトだけを拡張した。

- `read-g1-navigation-state.py`: `--discovery-seconds`、`--sample-period`、単調時計の観測時刻・実観測秒数。
- `read-g1-dds-hosts.py`: `--local-ip`（既に割り当てられたIPを指定。OS設定操作なし）。
- 対応テスト: 不正なIP/待ち時間を通信開始前に拒否。既存の送信entrypoint混入防止テストも通過。

今回のUbuntu/NIC状態で使用した受信コマンド:

```bash
source scripts/activate-g1-env.sh
python scripts/read-g1-navigation-state.py --group all --discovery-seconds 20 --seconds 15 --sample-period 0
python scripts/read-g1-dds-hosts.py --local-ip 192.168.123.200 --seconds 40
```

host照合用の最終実測は二つを同時期に実行した。次回は既存IPv4を再確認して指定する。
`--sample-period 0` は全takeサンプルの要約を出すが、点群のbulk payloadは保存しない。
終了コード0だけでは全項目PASSにならない。最終実測の終了コードもSLAM受信により0だった。

変更後の専用テストはG1 venvで **17 passed**。
全テストはUnitree SDKのない既存開発venvで **381 passed, 1 skipped**。
テストのRPCはfakeであり実機送信なし。全診断プロセスは終了済み。

証拠は `.runtime/navigation-readonly-20260912/`:
`telemetry-final.jsonl`、`hosts-final.jsonl`、`results.json`、初回/再確認ログ、
開始/終了のnetwork JSON、`pytest.log`。診断stderrは空。

今回G1へ送信したwrite/RPC/motion command: **NONE**
