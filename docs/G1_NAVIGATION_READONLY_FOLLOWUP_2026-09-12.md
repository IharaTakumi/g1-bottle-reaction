# G1 Navigation追加read-only切り分け — 2026-09-12

## 結論

- Odometry: **OD-B**。位置を取得できる別のlive topicは今回見つからなかった。
- LiDAR: **LD-B**。cloudだけでなくIMUも0件、rangeはpublisher自体なし。LiDAR stream全体の停止/disableを優先する。
- DDS: **DDS-B**。writer discoveryとmatchは成立したが、対象writerのRTPS DATA/DATA_FRAGが30秒間0件。
- G1へ送信したwrite/RPC/motion command: **NONE**。

Mapping開始条件は引き続き未達。サービス停止や故障までは断定しない。

## 実行条件と安全境界

- `enp129s0`、`192.168.123.200/24`、DDS domain 0。NIC・IP・routeを変更していない。
- 一つのDDS participantで20秒discovery後、同じparticipantのreaderを30.006秒維持。
- DataReaderはlive discoveryの型名が明示的allowlistと一致したtopicにだけ作成。
- `rt/utlidar/switch`にはreaderもwriterも作成していない。
- アプリケーションPublisher/DataWriter、Unitree ChannelPublisher、RPC client、RobotAdapter、
  NavigationCoordinator、SLAM操作コードは作成・起動していない。
- DDS discovery/ACK等の組込みprotocol通信は発生。pcap上の送信RTPS packet 528件はこの通信を含む。
  アプリケーションのwrite/RPC/motion commandではない。
- sudo、tcpdump権限付与、サービス操作、SSH、G1へのファイル転送、依存導入なし。
- traceとpcapはリポジトリ内`.runtime/navigation-readonly-followup-20260912/`だけに保存。

## Odometry / SportModeState

| Topic | live publication type | matched | samples | rate | 判定 |
| --- | --- | ---: | ---: | ---: | --- |
| `rt/dog_odom` | `nav_msgs::msg::dds_::Odometry_` | 1 | 0 | 0 Hz | SILENT |
| `rt/odommodestate` | `unitree_go::msg::dds_::SportModeState_` | 1 | 0 | 0 Hz | SILENT |
| `rt/lf/odommodestate` | `unitree_go::msg::dds_::SportModeState_` | 1 | 0 | 0 Hz | SILENT |
| `rt/sportmodestate` | `unitree_hg::msg::dds_::SportModeState_` | 1 | 0 | 0 Hz | SILENT |
| `rt/lf/sportmodestate` | `unitree_hg::msg::dds_::SportModeState_` | 1 | 0 | 0 Hz | SILENT |

`rt/lf/sportmodestate`には別participantの`unitree_go` subscriptionも発見したが、live writerは`unitree_hg`だけ。
readerはwriterと同じhg型を選択した。型名をtopic名から推測していない。

公式Unitree C++ SDKのhg `SportModeState_`は`fsm_id`、`fsm_mode`、`task_id`、`task_time`の4項目。
position、velocity、orientation/yaw、error_codeはこの型に存在しない。
go型の`odommodestate`にはposition/velocity/IMU rpy/mode/error_codeがあるが、今回はsampleがないため値を表示できない。

全writerはreader作成から約0.00006〜0.0075秒でmatch。first sampleは全て未到着。
QoS不一致、sample lost/rejected、deserialize errorはいずれも0。

よって質問1への回答は **NO**。現在位置を取得できる別のlive topicを、この観測では確認できなかった。
hg `sportmodestate`は仮にLIVEでも位置topicではない。`dog_odom`だけに固執していることが原因ではない。

## MID-360 / utlidar

| Topic | live状態 | type | matched | samples | rate |
| --- | --- | --- | ---: | ---: | ---: |
| `rt/utlidar/cloud_livox_mid360` | publisherあり | `sensor_msgs::msg::dds_::PointCloud2_` | 1 | 0 | 0 Hz |
| `rt/utlidar/imu_livox_mid360` | publisherあり | `sensor_msgs::msg::dds_::Imu_` | 1 | 0 | 0 Hz |
| `rt/utlidar/range_info` | remote subscriberのみ | `geometry_msgs::msg::dds_::PointStamped_` | 0 | 0 | 0 Hz |
| `rt/utlidar/switch` | endpointなし | — | — | — | — |

20秒discovery＋30秒観測で得た`rt/utlidar/*` endpointはcloud、IMU、rangeの3名称。
switchはlive DDS endpointとして現れなかった。
cloud/IMU publisher participantとSLAM側にある複数subscriberは発見。range_infoは既存remote subscriber 1件だけでwriterなし。

IMU readerはlive型名`sensor_msgs::msg::dds_::Imu_`との一致を確認し、ROS 2標準field順と手元SDKの
Header/Quaternion/Vector3 schemaから受信用schemaを構成。rangeは手元SDKの生成済み`PointStamped_`。
いずれもwriter型と異なるPython型を決め打ちしていない。

質問2への回答は **LiDAR stream全体が現在停止/disableされている可能性が高い**。
cloudだけOFFというLD-Aの観測ではない。endpoint公開が残っているため、driver processの完全不存在を意味しない。

## RTPS packet / reader切り分け

CycloneDDS内蔵PacketCaptureFileで50秒全体を取得し、discoveryのwriter endpoint GUIDと
RTPS DATA/DATA_FRAG内writer GUIDを照合した。point payloadはデコードしていない。

- pcap: 176,182,348 bytes、107,405 packets。
- RTPS: 107,401 packets。受信106,873、送信528。
- user DATA submessage: 106,709件、110 writer GUID。ネットワーク上のDDS DATA受信自体は活発。
- DATA_FRAG: 全writer合計0件。
- 今回の8対象writer: DATA 0、DATA_FRAG 0、wire bytes 0。

大きなPointCloud2のfragmentだけが落ちた症状ではない。小さいOdometry、go/hg SportModeState、
LiDAR IMUでも対象writerからDATAがない。CycloneDDS readerに到達後のdeserialize失敗でもない。
このため **DDS-B**: discovery endpointだけあり、対象DATA自体がこのreaderへ来ていない、と分類する。

テキストtraceはまずfinest、次にカテゴリ限定を試したが、このローカルCycloneDDS 0.10.2が
participant作成中に`*** buffer overflow detected ***`でabortした。これは過去資料にも記録された
設定出力処理の既知症状。いずれもreader作成・アプリケーション送信前に停止し、core dumpは無効だった。
OS設定やライブラリを変更せず、最終観測は`Verbosity=none`＋内蔵pcapとreader status callbackで実施した。

## `rt/utlidar/switch` 静的調査

今回のlive discoveryではtopic endpoint、writer、readerのいずれも0件。
一方、手元の公式Unitree SDK2 Python checkout（commit
`65691c8a8bc53b98d3976dba4dbf9d5d20b2e7f5`）の
`example/go2/high_level/go2_utlidar_switch.py`に実装がある。

```text
LiDARをenableする場合に必要になるcommand:
topic: rt/utlidar/switch
type: std_msgs::msg::dds_::String_
payload: String_.data = "ON"
根拠: 公式Unitree SDK2 Python exampleが同topic/typeへ"ON"/"OFF"をWriteする
```

これはGo2ディレクトリのexampleで、接続中G1での対応や副作用は実行確認していない。
今回はexampleをimport・実行せず、Publisherも生成せず、`ON`/`OFF`とも送っていない。
exampleのmainは`OFF`を送るため、read-only調査としてそのまま起動してはいけない。

## Recommended next action

既存の許可された管理consoleから、`.161`上のLiDAR driver/enable状態とhigh-level odometry生成processの
状態・直近ログを**読み取りだけで**確認する。restart・switch publish・設定変更はまだ行わない。
対象writerがいずれもDATAを出していないため、Desktop readerの追加調整より生成側状態の確認が次の一手になる。

## 変更・証拠

- `scripts/g1-navigation-readonly-check.py`: live型選択、1 participant、match/sample時刻、reader status。
- `scripts/analyze-g1-dds-pcap.py`: endpoint GUIDとDATA/DATA_FRAGのoffline相関。
- `UnitreeSdkRuntime.load_readonly_navigation_probe_types()`: hg SportModeStateと標準Imuの受信用schema。
- 対応するoffline safety/type/packet parserテスト。

検証結果は専用テスト21 passed、全テスト385 passed / 1 skipped。

主な証拠:

- `.runtime/navigation-readonly-followup-20260912/telemetry-pcap.jsonl`
- `.runtime/navigation-readonly-followup-20260912/dds-pcap/cyclonedds.pcap`
- `.runtime/navigation-readonly-followup-20260912/pcap-analysis.json`
- `.runtime/navigation-readonly-followup-20260912/telemetry-pcap.err`（空）
- 失敗trace試行のstderrと限定ログ（原因記録用）

G1へ送信したwrite/RPC/motion command: **NONE**
