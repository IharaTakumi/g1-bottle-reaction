# G1 SLAM / Navigation READ ONLY調査（2026-09-05）

> 過去のDesktop Ethernet/DDS調査記録です。現在の構成・再開手順は[G1_PC2_RUNTIME.md](G1_PC2_RUNTIME.md)を参照してください。以下の未確認事項や結果は調査時点のものです。

## 結論

追補: 現行の公式G1仕様と配布exampleを取得し、API IDと初期化経路を特定しました。
[操作・副作用一覧](G1_SLAM_OPERATIONS.md)を参照。以下のAPI未確定という記述は前回調査時点の記録です。

この接続機では、robot odometryとMID-360 point cloudを実際に受信できました。
SLAM状態配信も更新されていますが、SLAM mapping/relocation poseは未取得です。
`ready`という文字列だけでnavigation可能とは判断しません。
制御命令、RPC request、pause/stop、初期化、モード変更は一切実行していません。

## 再実行

```bash
cd ~/dev/g1-bottle-reaction
source scripts/activate-g1-env.sh
# 5秒のdiscovery後、15秒間購読。stateはpoint cloudを含まない。
python scripts/read-g1-navigation-state.py --group state --seconds 15
python scripts/read-g1-navigation-state.py --group cloud --seconds 15
# 今回の実測条件
python scripts/read-g1-navigation-state.py --group all --seconds 15
```

専用スクリプトはenp129s0/domain 0に固定。許可リスト内かつ実際に公開されたSDK型と
一致するtopicだけにDataReaderを作ります。request/command topicは購読対象にもしません。
DDS discovery/ACKは送受信しますが、アプリケーションDataWriter/Publisher/RPCはありません。
SDK型importは `UnitreeSdkRuntime.load_readonly_navigation_types()` に隔離。
既存G1 adapter初期化、NavigationCoordinator、RemoteNavigationAdapterは起動しません。

購読はBestEffort/Volatile/KeepLast(1)。大量点群を保持・出力せず、ヘッダーとレイアウトを
出力します。受信件数はこの診断がtakeした件数であり、送信周波数の厳密な測定ではありません。
最大60秒＋discovery約5秒。sample出力はtopic/JSON typeごとに約2秒間隔。
末尾summaryは総受信数、matched writer数、QoS不一致数、最終sampleを含みます。
`stamp_changes` は初回のstamp観測を1と数え、異なるstampへの変化を加算します。
ゼロstampの連続受信は1のままで、時刻更新を意味しません。
終了コード0は何かの受信成功、2は全対象未受信。全機能正常の判定ではありません。

## 実測（5秒discovery＋15秒受信）

すべて下表の購読は読むだけです。全対象でQoS不一致callbackは0件でした。

| Topic | SDK型 | 受信数 | matched writer | 結果 |
| --- | --- | ---: | ---: | --- |
| `rt/odommodestate` | unitree_go SportModeState_ | 692 | 1 | position/velocity/rpy受信。stampは常に0、frame不明 |
| `rt/lf/odommodestate` | 同上 | 300 | 1 | 同上 |
| `rt/dog_odom` | nav_msgs Odometry_ | 691 | 1 | `odom` → `robot_center`、stamp更新、pose/twist受信 |
| `rt/unitree/slam_mapping/odom` | nav_msgs Odometry_ | 0 | 1 | 接続成立、データ未受信 |
| `rt/unitree/slam_relocation/odom` | 同上 | 0 | 1 | 接続成立、データ未受信 |
| `rt/slam_info` | std_msgs String_ | 83 | 5 | `ctrl_info` / `robot_data`受信、stamp更新 |
| `rt/slam_key_info` | 同上 | 0 | 1 | 接続成立、未受信 |
| `rt/unitree_slam/waypoints` | 同上 | 0 | 1 | 接続成立、未受信。waypoint送信はしていない |
| `rt/utlidar/cloud_livox_mid360` | sensor_msgs PointCloud2_ | 150 | 1 | 点群データ受信、stamp更新、`livox_frame` |
| `rt/unitree/slam_mapping/points` | 同上 | 0 | 1 | 接続成立、未受信 |
| `rt/unitree/slam_relocation/points` | 同上 | 0 | 1 | 接続成立、未受信 |

robot odometry最終sampleのpositionは約 `(0.00592, 0.00441, 0.48685)`。
`rt/dog_odom`はframeとsource timestampがあるため将来のPose2D候補ですが、
地図frameとの変換、原点、ドリフト、単位・軸、orientation、G1とPCの時刻同期は未検証です。
共分散0を精度保証として扱いません。odomとSLAM map上のlocalizationを区別します。
`odommodestate`は型一致してもframeと時刻の不足があるため直接Pose2Dへ接続しません。

MID-360最終sampleは20,064点、441,408 bytes、point_step=22、height=1。
fieldsはx/y/z/intensity/ring/time。row_stepとデータ長の整合性を確認しました。
生データは受信しますが保存しません。点座標の幾何精度やLiDAR-to-base外部校正は未検証です。
トピック名はMID-360を示していますが、ハードウェア型番の実物確認はしていません。

`ctrl_info`の観測値:

- `info="not init"`、`errorCode=0`
- `stateMachine.state="ready"`、`ctrName="not init"`
- `isOpenPlan=false`、`isPause=false`、`is_arrived=false`
- currentPose/startPose/targetPoseの位置・角度は0

`robot_data`にはmotorTemp/motorError、battery、CPU関連の生値もありました。
単位未確認の値は換算しません。

SLAM状態を送るparticipantと、`slam_operate` request受信/response公開のparticipantが一致しました。
したがって、関連DDS participantと状態配信経路の稼働は確認できています。
ただしRPC応答能力、プロセス名/PID、systemd状態、搭載SDK/firmwareバージョンは未確認です。
SSHやサービス管理APIは使っていません。matched成立＋データ0は「サービスなし」ではなく、
この時間窓ではサンプルが得られなかったという意味です。`not init`から未初期化状態が
示唆されますが、原因の断定やSLAM起動操作はしていません。

## 発見したservice/APIと操作区分

| 対象 | 根拠・利用可能性 | 区分／今回の扱い |
| --- | --- | --- |
| `slam_operate` | request subscription / response publicationを同一participantに発見 | discoveryは読むだけ。RPCは未送信。対応API ID/versionは未確定 |
| SLAM pause/stop | このvendor SDKにslam_operate client/ID定義なし | 制御を伴う。存在・仕様をこの接続機で確定できず、未実行 |
| 旧 `rt/qt_command` / pause command 13 | 公式unitree_slamのpause_nav.cpp等に定義 | 制御publish。現機のdiscoveryではqt系未観測。現行slam_operateへ流用不可 |
| 旧 `rt/qt_notice` / `rt/lio_sam_ros2/mapping/re_location_odometry` | 同公式exampleのSubscriber対象 | 購読は読むだけだが現機では未観測、未購読 |
| G1 `sport` / `GetFsmId` (7001) | vendor g1/loco clientに定義、sport endpointあり | 意味上は照会だがRPC request送信が必要。今回は未実行 |
| G1 `StopMove` | vendorではSetVelocity(0,0,0)、API 7105に到達 | 制御。navigation停止APIと同義ではない。未実行 |
| G1 Move/SetVelocity/SetFsmId/Start/Sit | vendor g1/loco clientに定義 | 制御。未実行 |
| `robot_state` / ServiceList (1003) | go2/b2 clientに定義、現機にrobot_state endpointあり | 意味上は照会だがrequest publishが必要。G1互換性未確認、未実行 |
| `robot_state` / ServiceSwitch (1001), report frequency (1002) | 同vendor client | 状態変更。未実行 |
| `rt/utlidar/imu_livox_mid360` | sensor_msgs Imu_ publicationを発見 | 購読は読むだけ。vendorにはImu_定義がないため未購読 |
| `rt/sportmodestate`, `rt/lf/sportmodestate` | unitree_hg SportModeState_ publicationを発見 | 購読は読むだけ。vendor Pythonのunitree_go型と異なるため未購読 |

現機でさらにSLAM global_map/web_points、planner_map/global_map/gridmap、
grid/ele/collision/safe/pre_collision/pre_safe/warning/no_warning cloudsの公開を発見。
これらは状態・地図・点群を読む候補ですが、今回は個々のsampleは検証していません。
公開情報の詳細は末尾のtopic一覧とraw discoveryログを参照してください。

## 公式ソースとの照合

- [Unitree SDK2 Python](https://github.com/unitreerobotics/unitree_sdk2_python/tree/65691c8a8bc53b98d3976dba4dbf9d5d20b2e7f5):
  vendorと調査時masterが同じcommit。nav_msgs Odometry_/sensor_msgs PointCloud2_/std_msgs String_と
  unitree_go SportModeState_はあるが、SLAM client、sensor_msgs Imu_、unitree_hg SportModeState_はない。
- [公式G1 LocoClient](https://github.com/unitreerobotics/unitree_sdk2_python/blob/65691c8a8bc53b98d3976dba4dbf9d5d20b2e7f5/unitree_sdk2py/g1/loco/g1_loco_client.py):
  StopMoveも速度commandを送るため呼び出していない。
- [公式unitree_slam](https://github.com/unitreerobotics/unitree_slam/tree/1e49bfa4dca2c992a566ee7d1d8480d4102149b8):
  `.vendor/unitree_slam`に閲覧用cloneのみ。ビルド・install・example実行なし。
  qt_command/qt_notice方式で、今回観測したslam_operate方式との互換性は確認できない。
  query_node/query_edgeもPublisherを使うため「query」という名前だけでは実行不可。
- [公式SDK2 C++](https://github.com/unitreerobotics/unitree_sdk2/tree/9754cd153af3da471b0fe5f3aa535e426fb11db3):
  GitHub treeでhg/SportModeState_.hppの存在を確認。未導入、Python型への手動移植もしていない。
- [公式G1ドキュメント](https://support.unitree.com/home/en/G1_developer):
  今回のweb取得では本文を取得できず、現行slam_operateのAPI ID/versionの根拠には使っていない。
  第三者のGo2記事や旧SLAMのcommand番号から現機のAPIを推測しない。

## 既存Navigation設計との関係

`NavigationCoordinator`はPATROLLING時のreaction前pause、所有pauseの確認、完了後resume、
close時のPATROLLING/PAUSEDに対するstopを行います。今回の無送信調査には使いません。
`RemoteNavigationAdapter`はtransport注入境界だけで、本番transportは未実装。
start/resumeはopt-inがありますがpause/stopはそのgate外、heartbeatもrequestです。
ユーザーの今回の禁止範囲は既存アダプタのgateより厳しく、診断を独立させています。

将来の接続には、map poseのframe/timestamp/鮮度検証、SLAM状態からNavigationStatusへの
検証済みmapping、route ID/arrival/paused/stopの意味、server側lease watchdogとcommand重複排除が必要。
今回の`ready`をIDLE、`isPause=false`を移動可能、odomをmap poseとして扱う変更は加えていません。

## 変更と検証

追加: `scripts/read-g1-navigation-state.py`、offline診断テスト、本レポート。
変更: `UnitreeSdkRuntime`にschemaだけをlazy importする関数、ドキュメント相互リンク。
システムパッケージ・Python依存・ネットワーク設定変更なし。すべてリポジトリ配下。
通常/G1両venvでpip check正常、pytestは178 passed, 1 skipped。
追加4テストは点群ペイロード省略・レイアウト不整合、JSON/時刻処理、送信entrypoint混入防止。
静的な送信entrypointテストは補助であり、実行経路のコード監査を代替するものではありません。

ログ: `.runtime/navigation-discovery.jsonl`（10秒、284 endpoints）、
`.runtime/navigation-telemetry.jsonl`（5秒discovery＋15秒受信、QoSとsummary込み）、
`.runtime/navigation-telemetry.err`、`.runtime/navigation-pytest-{dev,g1}.log`。
診断は終了済みで常駐購読は残していません。

## 関連topic公開一覧（discoveryのみを含む）

P/Sは外部participantのpublication/subscriptionを示し、この診断からの送信ではありません。
requestへの送信は制御を含むRPCです。それ以外の表中topicは購読のみならREAD ONLYです。

| Topic | Type | 観測方向 |
| --- | --- | --- |
| `rt/api/slam_operate/request` | `unitree_api::msg::dds_::Request_` | publication / subscription |
| `rt/api/slam_operate/response` | `unitree_api::msg::dds_::Response_` | publication / subscription |
| `rt/collision_clouds` | `sensor_msgs::msg::dds_::PointCloud2_` | publication |
| `rt/dog_odom` | `nav_msgs::msg::dds_::Odometry_` | publication / subscription |
| `rt/ele_clouds` | `sensor_msgs::msg::dds_::PointCloud2_` | publication |
| `rt/global_map` | `nav_msgs::msg::dds_::OccupancyGrid_` | publication |
| `rt/grid_clouds` | `sensor_msgs::msg::dds_::PointCloud2_` | publication |
| `rt/gridmap` | `grid_map_msgs::msg::dds_::GridMap_` | publication |
| `rt/lf/odommodestate` | `unitree_go::msg::dds_::SportModeState_` | publication |
| `rt/lf/sportmodestate` | `unitree_go::msg::dds_::SportModeState_` | subscription |
| `rt/lf/sportmodestate` | `unitree_hg::msg::dds_::SportModeState_` | publication / subscription |
| `rt/no_warning_clouds` | `sensor_msgs::msg::dds_::PointCloud2_` | publication |
| `rt/odommodestate` | `unitree_go::msg::dds_::SportModeState_` | publication / subscription |
| `rt/planner_map` | `grid_map_msgs::msg::dds_::GridMap_` | publication |
| `rt/pre_collision_clouds` | `sensor_msgs::msg::dds_::PointCloud2_` | publication |
| `rt/pre_safe_clouds` | `sensor_msgs::msg::dds_::PointCloud2_` | publication |
| `rt/safe_clouds` | `sensor_msgs::msg::dds_::PointCloud2_` | publication |
| `rt/slam_info` | `std_msgs::msg::dds_::String_` | publication / subscription |
| `rt/slam_key_info` | `std_msgs::msg::dds_::String_` | publication / subscription |
| `rt/sportmodestate` | `unitree_hg::msg::dds_::SportModeState_` | publication / subscription |
| `rt/unitree/slam_mapping/odom` | `nav_msgs::msg::dds_::Odometry_` | publication / subscription |
| `rt/unitree/slam_mapping/points` | `sensor_msgs::msg::dds_::PointCloud2_` | publication / subscription |
| `rt/unitree/slam_relocation/global_map` | `sensor_msgs::msg::dds_::PointCloud2_` | publication |
| `rt/unitree/slam_relocation/odom` | `nav_msgs::msg::dds_::Odometry_` | publication / subscription |
| `rt/unitree/slam_relocation/points` | `sensor_msgs::msg::dds_::PointCloud2_` | publication / subscription |
| `rt/unitree/slam_relocation/web_points` | `sensor_msgs::msg::dds_::PointCloud2_` | publication |
| `rt/unitree_slam/waypoints` | `std_msgs::msg::dds_::String_` | publication |
| `rt/utlidar/cloud_livox_mid360` | `sensor_msgs::msg::dds_::PointCloud2_` | publication / subscription |
| `rt/utlidar/imu_livox_mid360` | `sensor_msgs::msg::dds_::Imu_` | publication / subscription |
| `rt/utlidar/range_info` | `geometry_msgs::msg::dds_::PointStamped_` | subscription |
| `rt/warning_clouds` | `sensor_msgs::msg::dds_::PointCloud2_` | publication |
