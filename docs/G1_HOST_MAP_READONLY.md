# G1のSLAM提供host・map・初期pose調査

> 過去のDesktop Ethernet/DDS調査記録です。現在の構成・再開手順は[G1_PC2_RUNTIME.md](G1_PC2_RUNTIME.md)を参照してください。以下の未確認事項や結果は調査時点のものです。

2026-09-05。今回もslam_operate RPC、実機motion、サービス変更、SSH接続は未実行。

## hostの特定結果

**SLAMのDDS提供元は192.168.123.161と確認しました。**
実プロセスのPID/実行ファイル/コンテナ/保存mountまでは未確認です。

| IP | 公式構成 | 今回の実測 | 判定の限界 |
| --- | --- | --- | --- |
| 192.168.123.161 | PC1。公式G1構成は運動制御計算機と開発計算機を区別 | SLAM server participant、dog_odom、lowstate、MID-360点群のRTPS送信元 | DDSのネットワーク提供hostは確定。relay/NAT/コンテナの背後やPIDは外部情報だけでは確定不可 |
| 192.168.123.164 | PC2、開発用Jetson Orin NX。公式はJetsonへUnitreeサービスを配備しないと説明 | neighbor entryあり。既往ping成功。今回25秒の対象multicast観測では.164のprefixなし | 現物SKU/OS/追加ユーザープロセスは未調査。DDS未観測は停止の証明ではない |

公式根拠:
[About G1](https://support.unitree.com/home/en/G1_developer/about_G1)、
[SLAM interfaceのNetwork configuration](https://support.unitree.com/home/en/G1_developer/slam_navigation_services_interface)。
公式は運動制御計算機を一般開発用に公開していないと説明しています。
したがって.161へのログインを当然のアクセス方法とは扱いません。

### 対応付けの証拠

通常UDPソケットでenp129s0の239.255.0.1:7400を25秒受信し、RTPS headerのGUID prefixと
UDP source IPを記録しました。GUIDのバイト列からIPを推測したものではありません。
同時期の10秒DDS discoveryからendpointのparticipant GUIDを取得し、先頭12 bytesで照合しています。

SLAM participant:
`<REDACTED_PARTICIPANT_GUID>`

RTPS prefix: `<REDACTED_RTPS_PREFIX>` → source `192.168.123.161:<SOURCE_PORT>`（7 packets）。
このparticipantは次を同時に持ちます。

- publication: rt/slam_info、rt/api/slam_operate/response、rt/dog_odom
- subscription: rt/api/slam_operate/request、LiDAR点群、lowstate

LiDAR点群publication participant:
`<REDACTED_PARTICIPANT_GUID>` → `.161:<SOURCE_PORT>`（9 packets）。
lowstate publicationも別prefixで`.161`へ一致しました。計25 prefixを観測。
ポートやGUIDは再起動で変わるため固定設定には使いません。
.161のrequest publicationも観測しましたが、それは他の既存participantのendpoint情報です。
本診断がRPCを送信したという意味でも、既存clientがその場でcommandを送った証拠でもありません。

受信スクリプト: `scripts/read-g1-dds-hosts.py`。
send/sendto/SDK clientを使用せず、RTPS payloadは保存しません。
OSのmulticast加入/IGMPなどは発生し得ます。DDS discovery診断は通常の発見通信を行います。
raw socketの利用は権限不足でしたが、sudo・capability付与・apt追加はしていません。

証拠ファイル:

- `.runtime/g1-dds-hosts.jsonl` / `.err`
- `.runtime/g1-host-discovery.jsonl` / `.err`
- `.runtime/g1-host-correlation.json`

## 既存アクセスと、分かっていないこと

- private key、password、agent credentialsは読み取っていません。
- 非標準ポート、他ツールの保存session、管理者専用consoleまで網羅したという意味ではありません。
- プロジェクトに実機process/service設定やmap保存先を示すデータは見つかっていません。
- 公式exampleの`/home/unitree/test.pcd`はsave/load時に渡すサンプル値です。
  **現在の保存ディレクトリや実在mapの根拠ではありません。**
- 今回の既存受信ログでは`pos_info`/`mapping_info`のmap addressは得られていません。

よって、現時点では**既存PCDの有無・filename・path・size・mtime・headerはすべて未確認**です。
mapがないとは結論しません。directoryを推測で決めず、.164のホームを探索して.161のmap確認の代わりにもしません。

## 次に行うREAD ONLY確認（人間の既存アクセス経由）

.161の管理権限を持つ担当者またはUnitreeが認める管理consoleで、まず以下を提示してもらいます。
これらのコマンドは**今回実行していません**。SSH接続やサービス操作を含みません。

1. 実行hostのhostname、IPとSLAM process/serviceの名前を確認する。
   process一覧はPID/commから絞り、実在するserviceに対してunit情報を読む。
2. 実在unitのMainPID/ExecStart/FragmentPath/WorkingDirectory/RootDirectoryを確認。
   systemdがない場合は既知PIDの`/proc/<PID>/cmdline`、`cwd`、`root`、必要なmount情報を確認する。
3. ExecStart等で参照されるlaunch/YAML/JSON/unitファイルだけを読み、map path、保存先設定、
   読込map、パラメータ名、serviceのnamespaceを得る。共有credentialなど無関係な設定は収集しない。
4. コンテナやmountがある場合は「serviceから見た絶対パス」と「host上のパス」の対応を確認。
5. 根拠のあるmap directory内だけをlist/statし、候補のPCD headerを読む。
   書き換え、サービス再起動、map load API、再mappingによる探索はしない。

READ ONLYコマンドの種類: `ps`、`systemctl show/cat`、`readlink`、設定ファイルの限定read、
確認済みdirectoryの`ls/stat`。unit名やPID、pathは実データで決め、仮の値では実行しません。
設定にdefault map pathがなくAPIごとのaddressで決まる実装なら、既存の保存履歴/操作記録/配備manifestを根拠にします。
それもなければディレクトリは引き続き不明です。

## PCDから5項目だけを読む方法

`scripts/inspect-pcd-header.py`を追加しました。ネットワーク機能はありません。
**確認済みPCDがすでに読み取り可能な場所にある場合だけ**、その絶対パスを引数に指定します。

出力はfilename、absolute_path、size_bytes、modified_time_utc、pcd_headerのみ。
regular fileだけを対象とし、最大64 KiBのheaderをDATA行まで読み、point payloadは読みません。
PCDを新たにrobotへコピーしたり、scriptをrobotへ配備したりする処理はありません。
remoteにscriptを配置する行為はWRITEなので、この調査には含めません。
担当者が既存consoleで同等のread処理を行い、この5項目だけ共有する方法でも構いません。
ファイル内容は変更しませんが、OSのatime/audit log更新はあり得ます。

PCD headerのFIELDS/SIZE/TYPE/COUNT/WIDTH/HEIGHT/POINTS/DATAから形式の整合性を確認。
**VIEWPOINTがあっても、それは現在のロボットのmap上poseではありません。**
ファイル存在やPCD形式だけで、そのmapが現在地に対応する・有効なSLAM mapであるとは断定しません。
point dataの解析やhash算出は今回の「headerだけ」の範囲に含めません。

## 現在位置から1804の初期poseを決める条件

1804はmap frameにおける初期translationとquaternionを要求します。
`dog_odom`のposition/yawにはmapへの変換がまだなく、その値を1804へコピーできません。
0,0,0やidentity quaternionも例から流用しません。

必要な根拠:

1. 選択mapの作成場所・map原点・軸・単位・版を特定。
2. map上の既知ランドマークと現在の実機位置・向きを対応付ける。
   過去の測量済み基準位置/姿勢の記録、または既存mapと現在scanのoffline位置合わせ等を用いる。
   offline registrationも重なり・残差・別解を確認し、人間が結果を承認する。今回はmap未入手のため未実施。
3. 1804のpose基準点を確認。公式SLAMはMid360-IMUとの関係を説明しているため、
   robot_centerの測定をそのままセンサーposeとして扱わない。
   例えば必要なのがIMU poseなら `T_map_imu = T_map_base × T_base_imu`。
   `T_base_imu`は検証済み校正から得る。頭部・センサー取付姿勢も考慮する。
4. roll/pitch/zも確認し、平地というだけで0に固定しない。
   quaternionは(x,y,z,w)順、有限・単位長を確認し、degree/radian混同を防ぐ。
5. robotが測定位置・向きから変わっていないことを実行直前に確認する。

承認資料に必要な値はmap identity、service側absolute path、x/y/z/q_x/q_y/q_z/q_w、
その測定方法・時刻・不確かさです。現在はその数値を根拠付きで埋められません。

## relocation/odomを主poseにする検証項目

| 項目 | 採用条件 | 不成立時 |
| --- | --- | --- |
| frame_id / child_frame_id | 実データを記録し、map・sensor・robot_centerとの関係と右手系/単位を検証。未知frameや変更を検出 | pose unavailable。dog_odomへ自動fallbackしない |
| timestamp | 非ゼロ、source時間が更新、逆行/巻戻り/大きな跳躍を検出。source clockとPCのoffsetを確認 | source freshness判定不可を明示 |
| quaternion | 全成分有限、norm非ゼロ・許容範囲内、順序確認。qと-qを同一回転として扱う。yawへ射影できる姿勢か検証 | pose拒否。無条件正規化で異常を隠さない |
| freshness | source stampとローカルmonotonic受信時刻を別々に保持。実測周期/jitterからYAMLでtimeoutを設定。DDS matchedだけでは採用しない | stale/disconnectedとして移動禁止 |
| map identity | pos_infoのpcdName/addressと人間が確認したmap版/配備manifestを一致させる。path/size/mtimeだけでは同一内容を完全証明しない | map不明・変更でpose無効化 |
| localization validity | pos_info/errorCode、relocation/odomの更新、位置連続性・初期poseとの差を照合。RPC succeedやctrl_info readyだけで成功にしない | 未localizeとして保持 |

mapが途中で切り替わった場合は同じframe文字列でもposeを継続利用しません。
橋渡しはDDS → Ubuntu側の読取bridge → NavigationTransport/RemoteNavigationAdapter.pose() → Pose2D。
statusは別途ctrl_info/task_resultを照合し、未確認のreadyをIDLEに写像しません。
Coordinatorのclose時stopやreaction時pause/resumeを現在の診断へ接続しません。

## 最初に人間が承認すべきWRITE

**今は承認可能な具体的WRITE条件が揃っていません。次の実作業は上記のmap/config READ ONLY確認です。**

既存mapと初期poseを特定できた場合、最初の実機WRITE候補は
**1804の単発call（選択PCD＋検証済み初期poseによるrelocation開始）**です。
承認対象はservice/version、host、map path/identity、7個のpose数値、周囲/現在姿勢、
既存navigation taskなし、App navigationとの競合なし、timeout後に再送しない条件を含めます。
1102・1201・1202・1901等を前処理/後処理として自動連結しません。
成功判定はrelocation/odomとpos_infoの有効化。失敗時は記録して止まり、別WRITEを自動実行しません。
不動がserver仕様で保証されていない点、初期化・稼働taskへの影響を承認者に明示します。

mapがないことを管理者が確認した場合は、別枝として**1801の単発mapping開始**が候補です。
未保存mapping状態のリセット影響を確認してから承認し、1802の保存/上書きや実機移動は別承認。
map不明を「mapなし」と扱って1801を選びません。

1804/1801のどちらも本調査では実行していません。SSH許可やサービス起動許可も推定していません。

## ローカル検証

追加したRTPS header解析とPCD header限定読取をofflineテストで検証しました。
通常/G1両venvの最終pytestは183 passed, 1 skipped。
作業中に別作業のreaction_generator関連変更も現れましたが、本調査では編集していません。
本調査の追加はhost受信・PCD headerスクリプト、対応テスト、本レポートと既存レポートのリンクのみです。
