# G1 SLAM初期化・操作仕様調査（未実行）

調査日: 2026-09-05。以下は送信すべき操作を実行する手順書ではなく、将来の承認判断用の仕様表です。
今回、G1へのDDS参加・Publisher生成・API call・SSH接続は行っていません。
既存の観測ログ、vendor SDK、公式G1ページと公式配布exampleの静的調査のみです。

## 何を送ると何が起きるか

根拠は[公式G1 SLAM and Navigation Services Interface](https://support.unitree.com/home/en/G1_developer/slam_navigation_services_interface)
（画面の更新日時2026-07-20 15:33:08）と同ページの配布example。
service=`slam_operate`、文書上のversion=`1.0.0.1`。
API IDはこの公式仕様を特定したもので、接続機の応答による対応version確認はまだしていません。

RPCは `rt/api/slam_operate/request` の `unitree_api::msg::dds_::Request_` を送信し、
`rt/api/slam_operate/response` の `Response_` を受ける経路です。
下表のpayloadはRequest.parameter内のJSONであり、裸のString_をrequest topicへ送る方式ではありません。

| 操作・API ID | parameterの内容／効果 | READ ONLY | SLAM状態だけか | 機体への影響・移動可能性 | map変更 | 元に戻せるか |
| --- | --- | --- | --- | --- | --- | --- |
| state/odom購読 | slam_info、slam_key_info、relocation/odomを受信 | はい | 変更なし | 命令なし | なし | 購読終了で完了 |
| mapファイル一覧・ヘッダー読み取り | 正しいSLAM実行hostでlist/stat/read | はい（内容。OSのatime/access log更新はあり得る） | 変更なし | 命令なし | 内容は変更しない | 復元不要 |
| 1801 start mapping | data.slam_type=`indoor`。mappingセッション開始 | いいえ | 目的はSLAM変更。不動保証は未記載 | example関数には移動callなし。ただし稼働中タスクとの干渉・副作用は未保証 | 作成中map状態を変える。既存保存PCDへの即時上書きは未記載 | 旧セッションへのundoは未記載。未保存データ保全が必要 |
| 1802 end mapping | data.address=保存先PCD絶対パス。mapping終了・保存 | いいえ | SLAM＋ファイル変更 | 移動callは示されていない。終了による稼働系への影響は未保証 | 保存・同名上書きの可能性あり。公式は同名ファイル上書き運用に言及 | バックアップなしの上書きは復元保証なし |
| 1804 initialize pose / start relocation | dataにx/y/z/q_x/q_y/q_z/q_w/address。既存PCDと初期poseを渡す | いいえ | 目的はSLAM/自己位置変更。不動保証は未記載 | exampleは1102を同時callしない。ただし既存タスクへの影響は未保証 | PCDは読込。ファイル書換えの記載なし | 再初期化は候補だが、旧状態への自動undo仕様なし |
| 1102 pose navigation | data.targetPose=位置＋quaternion、data.mode=1 | いいえ | いいえ、navigation目標変更 | **移動する** | PCD保存の記載なし | pause等でも実移動の履歴は元に戻らない |
| 1201 pause navigation | data={}。navigation一時停止 | いいえ | いいえ、走行制御への影響あり | 実行中移動を変える。停止時間・制動・保持は未規定 | PCD変更の記載なし | 1202が再開操作。ただし位置のundoではない |
| 1202 resume navigation | data={}。navigation再開 | いいえ | いいえ | **移動再開の可能性あり** | PCD変更の記載なし | 再pauseは可能な操作だが実移動はundo不可 |
| 1901 close slam | data={}。SLAM subsystem終了 | いいえ | 終了操作。制御系への波及未保証 | **安全停止APIとは断定不可**。下流制御への影響未確認 | 保存PCD削除の記載なし。未保存状態の損失可能性は未保証 | 再起動/再localizationが必要になり得る。自動復元仕様なし |
| 独立stop/cancel API | 公開7 APIに該当する独立定義なし | — | — | 未確認 | 未確認 | 未確認 |

「移動callなし」はexample内の事実であり「ロボットが絶対に動かない」というserver側保証ではありません。
「書換え記載なし」も全副作用がないとの証明ではありません。上表の変更操作はすべて未実行です。
旧unitree_slamのqt_command番号13/14等は別protocolのため使いません。

## 正式な初期化経路と二つのmode

公式はまずAppで `unitree_slam` と `lidar_driver` が起動済みか確認するよう指定しています。
AppのnavigationとSDK navigationは同時利用しない前提です。今回Appスイッチ操作はしていません。
現状は状態配信とLiDARは生きていますが、serverのmode、map選択、firmwareは確定していません。
共通の「InitSLAM」という単独APIは公開表にありません。用途に応じて次の枝に分かれます。

### 既存mapを使う場合（将来の操作順、今回は禁止）

1. 実行hostでmapの存在、PCDの内容、取得場所、座標原点、初期poseを読み取り確認。
2. **1804** にPCD絶対パスと、そのmap上での現在の初期poseを渡す。
   公式ページ名はinitialize pose、example定数名はSTART_RELOCATION_PL。
   一つのcallが「map読込＋初期pose設定＋relocation開始」の公開経路で、別の1803等は記載なし。
3. relocation/odomの時刻更新と有効な姿勢、slam_infoのpos_info/errorCode/map identity、
   slam_key_infoのエラーを確認。RPC succeedだけでlocalization収束とは判定しない。
4. navigation目標を送るには別途1102が必要。**1804後に1102を自動実行しない。**

公式exampleの初期poseは0/0/0・単位quaternionですがサンプル値です。
現場でその位置にいる根拠がない限り流用しません。初期poseを自動探索する別APIの公開定義は見つかっていません。

### mapがない場合（将来の操作順、今回は禁止）

1. **1801** でindoor mappingを開始。
2. mapping/odomとmapping/pointsの出力を確認し、環境を観測してmapを蓄積。
   mapping開始は巡回動作開始ではありません。静止したままでは観測範囲が限定され、
   4地点巡回に必要な地図を完成できるとは限りません。観測範囲を広げる実機移動は別承認です。
3. **1802** に未使用の保存先PCDパスを指定し、終了・保存。
   server側のパスです。PC上のリポジトリパスをそのまま渡してはいけません。
   公式の `/home/unitree/test.pcd` は例であり、既存ファイル有無を調べず使わないこと。
4. 保存結果とファイルを確認後、既存mapの枝の**1804**へ。

mappingはmapを生成する段階、relocation/localizationは既存mapに現在のscanを照合して
そのmap上のposeを推定・更新する段階です。どちらも目標への歩行navigationとは別機能です。
公式SLAMの適用条件は特徴の多い静的な屋内平地、X/Y各45m未満。
1102には現在位置から10m以内、直線移動、障害物高さ50cm以上という制約が記載されています。
任意経路計画や回り込み、安全停止距離を保証するAPIとは扱いません。

## 既存mapの有無を読む方法

**現時点でmapの有無は未確定。not init・topic無受信から「保存mapなし」とは言えません。**

| 方法 | 読む対象 | 分かること／限界 |
| --- | --- | --- |
| 既存受信ログ・slam_info購読 | type=pos_info / mapping_infoのdata.pcdName/address/currentPose | 選択mapの識別情報。未初期化なら出ない可能性があり、保存map一覧ではない |
| 既に確立したアクセスでserverファイルを読む | 配置設定から得たmapディレクトリ、または判明済みPCDパスのls/stat/PCD header | ファイル存在・サイズ・更新時刻・PCD形式。存在だけでは現環境に適合したmapと断定できない |
| relocation/global_mapを購読 | PointCloud2_ | 公式ではrelocation開始後に一度だけ送信。後から未受信でもmapなしとは言えない。確認のために1804を送らない |
| 公開map一覧API | 今回の7 APIには存在しない | 推測したAPI IDや旧query_nodeを送信しない |

受信済みnavigation-telemetry.jsonlにはctrl_info/robot_dataのみで、map addressは得られていません。
G1上のfilesystemには今回接続していません。次のREAD ONLY調査で必要なのは、
**SLAMが実行されているhostと、そのhostへの既存の読取アクセス／map保存ディレクトリ**です。
NXが.164というだけで、SLAMの保存パスが.164上にあると決めつけません。
公式初期パスや第三者記事の固定ディレクトリを現在の配備の事実と扱いません。

## slam_infoの解釈

| field/type | 公式記述・exampleの扱い | 今回の判断 |
| --- | --- | --- |
| type=pos_info | relocation情報。currentPoseは位置とquaternion、pcdName/addressを含む。exampleはerrorCode=0のこのtypeだけでcurPoseを更新 | 有効pose候補。未受信 |
| type=mapping_info | mapping情報としてpos_infoと同じ例に言及 | 地図作成中pose。巡回用localizationと区別 |
| type=ctrl_info | startPose/targetPose、stateMachine、is_arrived、progress等のcontrol情報 | controller status。pose成功の証明ではない |
| stateMachine.state | 例はfollow。readyを含む完全なenumや遷移条件は公開されていない | ready→NavigationState.IDLEへの直結不可 |
| stateMachine.ctrName | 例はpid。controller選択名と読むのが自然だが、正式な値一覧・not init条件は記載なし | 未初期化controllerを示唆する観測。mapファイル不存在の証明ではない |
| stateMachine.isPause | 公式が一時停止フラグと説明 | falseはnavigation有効/移動可能という意味ではない |
| ctrl_info内currentPose | 現機にはあるが、公開ctrl_info例にはない | 未検証の追加field。全0は有効な原点poseとみなさない |
| info / errorCode | 説明文字列／エラー番号。0はエラーなし | not initとerrorCode=0は両立。ready/0だけでlocalization成功にしない |
| slam_key_info/task_result | targetNodeName/is_arrivedによるtask結果 | 無受信はイベントがないだけの可能性。RPC受付と到着完了は別 |

controller実装本体は今回のvendorにも配布exampleにもありません。
ctrName/stateMachineの完全なserver側意味は未確定です。現在の結果は
「有効localizationをまだ確認できていない」と結論し、「mapがない」とは断定しません。

## NavigationCoordinatorへの接続判断

巡回用poseの第一候補は **rt/unitree/slam_relocation/odom (Odometry_)**。
直接CoordinatorへDDSを入れず、将来のUbuntu bridge/NavigationTransportを介し、
RemoteNavigationAdapter.pose()がPose2Dへ変換する構成を維持します。

採用前にheader.frame_id、child_frame_id、source timestamp、quaternion妥当性、
更新・鮮度、map identity、localizationエラーの確認が必要です。
公式はSLAM point cloud/positioningの原点をMid360-IMU座標系に結びつけ、X前方・Z上方と説明しています。
生のSLAM poseをrobot_centerと同一視せず、観測frameとセンサー外部変換、mapの初期原点を検証します。

statusはslam_info/pos_info・ctrl_infoとslam_key_info/task_resultを併用。
公式exampleはpos_infoで目標記録用poseを更新、task_result/is_arrivedで到着を判定しています。
ただしexampleの相関確認は弱く、そのまま本番bridgeにはしません。
map、task ID、pause所有権、disconnect、stale、未知stateを区別する契約が必要です。

- mapping/odom: map作成診断用。巡回時にrelocation/odomの代用品にしない。
- dog_odom: 通常odometryの診断・補助用。map localization消失時に黙ってfallbackしない。
- pos_info/currentPose: 補助照合候補。JSON単独ではframeの明示性が弱い。
- ctrl_info/currentPoseの全0: 不採用。

既存Coordinatorはreaction時pause/resume、close時stopを呼び得ます。
公開仕様に独立stopがない以上、NavigationAdapter.stopを1901へ仮割当しません。
1201の停止保証・1202の再開条件・server-side lease watchdogが検証されるまでは実接続しません。

## pause / resume / stop / waypointの仕様上の限界

- pause=1201、resume=1202、payloadはそれぞれdata={}。
- 独立したcancel/stop-navigation APIは公開7本にない。1901はclose SLAMであり非常停止保証ではない。
- G1 LocoClient.StopMoveは別service sportの速度0 command。SLAM task取消と同義ではない。
- waypoint移動相当は1102の単一targetPose＋mode=1。
- 公式exampleのposeListはクライアント内vector。記録キーはローカルpush_back、実行キーが1102を順次callする。
  serverにrouteを登録する公開APIの証拠ではない。rt/unitree_slam/waypointsのpublicationだけでも登録APIは特定できない。
- exampleのspeedパラメータはコメントアウト。低速指定がサポートされるとは断定しない。
- 共通feedbackはsucceed/errorCode/info/data。RPC return codeとpayload成功の両方を確認し、
  さらにtelemetryで実際の状態を確認する必要がある。timeout後の自動再送はしない。

## 監査した公式exampleと記録

[現行公式example ZIP](https://oss-global-cdn.unitree.com/static/dd442abf94ec44f599095cb15c3e298b.zip)
を `.vendor/unitree_slam_current/official-example.zip` に保存し、sourceだけ展開しました。
SHA256: `3a34d7f194c65a3ba304a83ee6e33decffe21e71294cfbd0fce3981cec933d02`。
ビルド、install、実行なし。既存SDK・venv・OS設定も変更なし。

ローカル根拠は `.vendor/unitree_slam_current/example/src/keyDemo.cpp`:

- 52行: service/version/API定数。
- 126行: destructorがstopNodeFunを呼ぶ。未知キーも1901を呼ぶためREAD ONLY起動不可。
- 153行: taskLoopFunが1102を送信。繰返し往復を含むので単一waypoint診断としてそのまま使えない。
- 210行: pos_infoだけをcurrent poseに採用。
- 222行: task_resultによる到着確認。
- 250行以降: 1901/1801/1802/1804/1201/1202のparameterとcall。

前回調査の「現行API不明」は、この公式ページ＋配布exampleの取得により**文書上の仕様が判明**しました。
実機で各APIの実行結果を確認したという意味ではありません。

実機提供hostの追補調査: [G1_HOST_MAP_READONLY.md](G1_HOST_MAP_READONLY.md)。DDS提供元は.161と照合済み、map保存先は未確定です。
