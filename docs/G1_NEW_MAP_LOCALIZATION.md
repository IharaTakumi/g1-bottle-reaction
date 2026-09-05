# 新規mapからlocalizationへ（実機未実行）

2026-09-05。G1搭載Jetson PC2（192.168.123.164）用。DesktopからのEthernet/DDS実行は廃止。旧mapの調査・再利用は行わない。

Desktop → G1-APのWi-Fi/SSH → PC2上の専用venv/runtime → eth0 DDS/RPC → PC1(.161)のSLAM、という構成。
eth0/unitree1やDDSシステム設定は変更しない。PCDはPC1(.161)のserver側パスに保存され、PC2ではセッション記録だけを保持する。
接続が切れているため、PC2実環境での依存・購読確認とvenv作成・配備は未実施。[準備とREAD ONLY診断](G1_PC2_RUNTIME.md)を先に行う。

## mapping/odomを1804の初期poseに利用できるか

**同じmap・同じ物理poseを保つ場合の初期推定値として採用する。ただし公開資料だけでは、無変換での一致を実機保証できない。**

2026-09-05に[公式G1仕様](https://support.unitree.com/home/en/G1_developer/slam_navigation_services_interface)
（更新表示2026-07-20）を再確認した。SLAM出力の点群・位置情報はMid360-IMU由来の座標系として説明される。
1804は保存PCDとXYZ・quaternionを受け取る。同一ページはmapping/odomとrelocation/odomをそれぞれの位置出力として列挙する。
一方、1804入力の基準点とmapping/odomのchild frameの明示的等式、1802保存時の座標再基準化有無、
最適化後PCDと最終online poseの厳密一致は記載されていない。

保存済み公式example `.vendor/unitree_slam_current/example/src/keyDemo.cpp` の
`relocationPlFun` は手入力の初期値を直接渡す。mapping/odomから初期値を作る実例ではない。
`slamInfoHandler` はpos_info.currentPoseを無変換で読むが、これもmapping→1804の直接証明ではない。
既存コード・既知の実測frameはmap→base_link。追加の固定変換を指定する根拠はない。

今回の実装は数値をそのまま初期推定に使い、`--accept-mapping-pose-seed` でこの未検証条件を明示する。
**このflagは座標系を検証したことにはならない。実機で1804後の収束を確認するための初期推定値の採用である。**
変換を推測して加えたり、単位quaternionへ置き換えたり、XYZだけ使ったりしない。
もし実際の入力基準が別frame I、odom childがBなら必要なのは `T_map_I = T_map_B × T_B_I`。
保存時にmap frame MからM'へ変わる実装なら、さらに `T_M'_M` が必要。現在どちらの数値も特定できていない。
このため未知frame・大きな不整合は拒否し、固定オフセットで帳尻を合わせない。

## コマンドの性質

`scripts/g1-slam-session.py` のstart/save/relocateは各一つのRPCのみ。
デフォルトはpreview。実送信は `--execute --enable-real-robot` の両方が必要。
1102、pause/resume、close、連続フロー、終了時RPCは実装していない。
SDK直接importは軽量なrobot_side/adapters/g1_robot.pyのUnitreeSdkRuntime内だけ。Python 3.8+用でDesktopアプリ本体をimportしない。subscriberはSDK型＋CycloneDDS DataReaderのみで、RPC clientを生成しない。
recordはPC2側にログを書くが、ロボットにはapplication publishしない。通常のDDS discovery通信はある。

各sessionは日時＋UUIDの一意な `/home/unitree/g1_… .pcd`（実際は空白なし）を生成する。
これはSLAMサービス側のパスでありPC2のパス確認はしない。同じsessionからsaveを再送しない。
1802には公開no-clobberオプションがないため、server上の原子的な不存在保証はできない。
UUIDと送信試行の再利用禁止で既存名の上書きを避ける設計。test1等の任意既存名は指定できない。

全操作は送信前にattemptをディスクへ保存。timeout・例外・失敗後も同じ操作を再実行しない。
送信されなかった可能性があってもattemptを削除して再試行しない。telemetryと結果を読んで個別判断する。
複数sessionでstartを重ねることも避け、現場で一つだけ運用する。別PC/Appからの操作はこのローカルlockでは防げない。

## 手順（各コードブロックを別々に実行）

以下は実行例であり、この実装作業では送信していない。
会場内で停止保持でき、App navigationと競合せず、SLAM/LiDARが稼働している状態で始める。

以下はすべてPC2にSSHした端末で実行する。専用環境の準備・READ ONLY確認完了後、新規sessionを用意する（initはDDS通信なし）。

```bash
cd /home/unitree/g1-slam-runtime
export PYTHONDONTWRITEBYTECODE=1
export G1_SDK_PATH=/home/unitree/unitree_sdk2_python
PY=/home/unitree/g1-slam-runtime/.venv-robot/bin/python
SESSION="$($PY scripts/g1-slam-session.py init)"
echo "$SESSION"
```

別のPC2 SSH端末では同じ作業ディレクトリ・環境変数・表示されたSESSION絶対パスを設定する。Desktop側のシェルでは実行しない。
subscriberを先に起動し、以後終了させずに残す。topicがまだ配信されていなくてもreaderを作る。

```bash
$PY scripts/g1-slam-session.py record --session "$SESSION"
```

元の端末で開始。previewを先に見たい場合はexecute/enableの二つを外す。

```bash
$PY scripts/g1-slam-session.py start --session "$SESSION" --execute --enable-real-robot
```

start.result.jsonのrpc_code=0/succeed=true/errorCode=0とmapping odomの到着を確認してから、
人間が純正コントローラーで一周する。recordは全受信odomとslam_info/slam_key_infoをJSONLに継続保存する。
生LiDAR/bagは今回の最短経路では記録しない。

**一周後はsaveより前に静止し、少なくとも3秒待つ。saveからrelocate・check終了まで位置・向き・頭部姿勢を保つ。**
最後のpose取得後にまだ歩いている設計にはしない。保存は別コマンド。

```bash
$PY scripts/g1-slam-session.py save --session "$SESSION" --confirm-stationary --execute --enable-real-robot
```

saveはstart成功後のodomだけを使い、source stamp更新、受信鮮度、2秒以上の位置・回転安定性を検査。
SDK準備後に最終poseを再取得しsave.attempt.jsonへ固定してから1802を一度送る。
save.result.jsonの成功を確認する。成功しても1804は送らない。

```bash
$PY scripts/g1-slam-session.py relocate --session "$SESSION" --confirm-stationary --accept-mapping-pose-seed --execute --enable-real-robot
```

同じsessionの保存成功と固定poseを使う。原点へ置き換えない。固定poseから5分以上経過した場合は拒否する。
人間の静止確認は必須で、1802後にmapping odomが停止した場合の実際の不動をソフトだけで証明はできない。

relocate後も静止し、数秒のデータを待って独立したREAD ONLY判定を実行する。

```bash
$PY scripts/g1-slam-session.py check --session "$SESSION"
```

checkは1804の成功に加え、送信後のrelocation odomの継続更新・鮮度・安定性、
pos_infoのerrorCode=0・address一致、pose間の整合性とSLAMエラーを検査する。
これはtelemetry上の成立候補。実際の会場位置・向きと合うことを人間が確認してlocalization成立とする。
同名map frameというだけで完全一致を保証しない。失敗時の別RPCや自動再送はない。
1 waypoint移動用1102は次段階で別途実装・実行判断する。

## 保存データと判定閾値

PC2のプロジェクト配下 `.runtime/g1_<UTC>_<UUID>/` にsession.json、telemetry.jsonl（全受信）、telemetry.json（最新window）、
各操作のattempt/result.jsonを保存。save.attempt.jsonのseedにはframe/source stamp/受信時刻/7成分を残す。
sessionは同一PC2ホスト・同一bootで使用する。recorder再起動は拒否し、欠落を隠さない。

閾値はconfig/g1-slam-session.yamlに置き、init時にsessionへ固定する。静止閾値5 cm/0.10 rad、
初期推定とlocalizationの照合閾値0.5 m/0.35 radは現場確認用の暫定値であり、メーカー精度保証ではない。
odomの位置・姿勢だけでは人や環境の動き、誤った場所への収束を排除できない。
実機送信、移動、localization成立、PCD保存成功はこの実装作業では検証していない。

SSH/Wi-Fi切断でrecordが終了してもSLAMへの終了RPCは送らない。再接続時は既存recorderの存否を確認する。
recordをSSHから独立して維持するにはPC2に既に導入済みのtmux等を利用する（この準備では導入や起動をしない）。
DesktopにJSONログを回収して閲覧するのは可能だが、monotonic時刻を含むsessionのコマンドはPC2上だけで続ける。
