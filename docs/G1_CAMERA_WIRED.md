# 有線G1 RGBカメラ: 2026-09-10実機確認

## 結果

Phase 1成功。有線でG1のRGB映像をUbuntuのOpenCV windowに連続表示しました。
移動・姿勢・関節・arm・sport命令は送っていません。移動はR3で行います。

| 項目 | 確認結果 |
| --- | --- |
| 有線NIC | enp129s0（当日のip address/routeから確認） |
| PC IPv4 | 192.168.123.200/24 |
| G1側IPv4 | 192.168.123.161 / .164に有線指定ping成功 |
| カメラ提供元 | 公式VideoClientのDDS service。応答元IPを直接指定するAPIではない |
| Resolution | 1920x1080 BGR |
| 連続表示 | 300.0秒、9,762フレーム、正常終了、クラッシュなし |
| FPS | 平均32.5（取得・表示ループのレート。カメラのユニークな露光回数ではない） |
| 一時取得失敗 | 2回、後続画像を取得して復帰 |
| 正常フレーム間の最大間隔 | 0.100秒 |
| JPEG診断 | 連続試験中にdecoderのcorrupt JPEG警告あり、表示継続 |
| 遅延 | ユーザーが「映像が動きに追従し、数秒の遅延はない」と確認。ms単位のend-to-end実測は未実施 |

GetImageSampleには撮影timestamp/sequence IDがないので、同じ撮影画像を複数回返しても
FPSに計上されます。FPSやフレーム間隔を撮影→表示遅延と解釈しないでください。

## 実行

G1とPCを有線接続し、Ubuntuのデスクトップ端末で実行します。

```bash
cd /home/ubuntu/dev/g1-bottle-reaction
/home/ubuntu/.venvs/g1-game-vision/bin/python -B tools/g1_camera_minimal.py
```

接続済みの物理有線NICでIPv4を持つ候補が一つなら自動選択します。複数候補・リンク未確立・
IPv4なしの場合は停止します。Wi-Fiは選びません。IP設定は変更しません。
NICの選択だけでは接続機器がG1であることを証明しないため、配線も確認してください。

候補確認:

```bash
/home/ubuntu/.venvs/g1-game-vision/bin/python -B tools/g1_camera_minimal.py --list-interfaces
ip -br addr
ip route
```

当日の確認済み構成を明示する場合:

```bash
/home/ubuntu/.venvs/g1-game-vision/bin/python -B tools/g1_camera_minimal.py \
  --network-interface enp129s0 --g1-ip 192.168.123.164
```

`--g1-ip`は選択NICのsubnet・direct route検証用です。VideoClientへの宛先指定や
G1 identity照合ではありません。IP/NICが変わったら現在値を使います。
`--duration 300`で5分試験、`--headless --duration 5`でGUIなし取得試験ができます。
`q`/`Esc`/window閉じるで終了、`f`でfullscreen切替、端末Ctrl+Cでも終了します。

## 最小構成と既知クラッシュ

`G1CameraSource`の既存GetImageSample/JPEG decodeを再利用しています。
SDK直接importは既存`adapters/g1_robot.py`の境界に限定。
カメラ専用loaderはChannelFactoryInitializeとVideoClientだけを渡します。
YOLO、PyTorch、RealSense bindingは読み込みません。

有線でも公式`ChannelFactoryInitialize(0, NIC)`の設定trace有効時にSIGABRTを再現。
gdbでは`do_print_uint32_bitset -> pf_tracemask -> print_configitems -> rtps_config_prep`
の中で`__snprintf_chk`がbuffer overflowを検出しました。
Wi-Fiだけの問題とは判断できません。

最小viewerでは初期化中だけSDKのin-memory XMLからTracing要素を除き、finallyで復元します。
明示したNIC名とdomain 0は維持します。SDK checkout、system library、G1設定は変更しません。
既存の`.runtime/cyclonedds/lib/libddsc.so`を子プロセスのCYCLONEDDS_HOMEで選びます。
継承されたCYCLONEDDS_URIは子プロセスから外し、他NICへの設定混入を避けます。
これらの環境変数は親shellに残りません。

この回避は最小viewer専用です。既存Game Vision CLIの通常DDS初期化は今回変更していません。
実機RGBの起動には上の最小viewerコマンドを使用してください。

親プロセスが子のnative abort/初期化のハングを検出します。画像のRPC timeoutは最大0.5秒。
読み取り失敗時は古い画像を黒い警告画面へ置き換え、既定5秒画像を得られなければ終了します。
Python側処理そのものがハングした場合は親も監視し、若干の猶予後にそのローカル子だけを停止します。
G1側のサービスや歩行制御を停止する機能はありません。

カメラ応答の取得はDDS discovery/ACKとimage serviceへの要求を伴います。
「通信を全く送らない」処理ではありませんが、ロボットの動作commandはありません。
画像停止時には操作者がR3で停止してください。視野外を確認できる監視者も置いてください。

## Phase 2: Depthの現状と候補

現在動作確認できたVideoClient経路では**Depth取得不可**です。公式SDK checkout
`65691c8a8bc53b98d3976dba4dbf9d5d20b2e7f5` のVideoClientは
`GetImageSample()`による画像binaryだけを公開しています。
Depth、aligned depth、intrinsics、depth scale、RGB/Depth同期IDのAPIはありません。

リポジトリの過去記録ではD435iとvideohub_pc4が記載され、camera競合も報告されています。
今回は`ssh -o BatchMode=yes -o StrictHostKeyChecking=yes unitree@192.168.123.164`が
認証拒否だったため、現在のPC2のUSB機器一覧・サービス・device占有状態・binding/firmwareを
直接確認できていません。**G1全体でDepthが不可能と断定したわけではありません。**

| 候補 | 現状・注意点 | 今日の判断 |
| --- | --- | --- |
| stock VideoClient | 実機RGB成功。Depth/校正情報を返さない | RGBゲーム性検証に使用 |
| PC2でD435i直接取得 | USB機器の存在と所有権を確認後、RealSense align(color)、intrinsics、depth unitsを取得する経路が候補 | 最優先調査。videohubとの競合未確認のため未実行 |
| 現在のG1側Depth配信サービス | SDKのVideoClientには存在しない。他サービスの有無はPC2監査が必要 | 未確認。サービス名やAPIを推測しない |
| RGB単眼Depthモデル | 画像から推定。モデルによって相対Depth/metric出力が異なり、2m境界には実機校正・精度確認が必要。処理負荷も増える | 実測Depthの代替としては未採用 |
| YOLO bboxサイズから距離 | カメラ校正、実身長、姿勢、遮蔽に左右される。bboxだけではmetric距離は確定しない | 簡易ゲーム案に限る。2m判定の本実装にはしない |
| 既存synthetic RGBD | 実機不要で距離マスクの見た目を確認できる。実際のG1映像ではない | 実機Depth取得までの表示ルール検証に利用可能 |

最小の代替は、今日は動作確認済みRGBで一人称ゲーム性を確認し、別途PC2へのSSHアクセスを
確認してからDepth共存経路を調査することです。Depthのためにvideohubをkillしません。
実機Depthを取得できた段階で、2.0mを初期値とするpixel clippingと
person bbox中央30〜50%のvalid depth median判定を追加します。

今回、`--visibility depth/person-depth`、`--max-distance`やYOLOは追加していません。
未取得DepthをRGBからあるように見せるfallbackもありません。
既存Game Visionのsynthetic/RealSense filterはそのままです。

## 依存・検証・復元

この作業で追加インストールはありません。既存の専用venv
`/home/ubuntu/.venvs/g1-game-vision`にあるPython 3.12.3、NumPy 2.5.3、
OpenCV 4.14.0.94、PyYAML 6.0.3、CycloneDDS 0.10.2、Unitree SDK 1.0.1、pytestを使用。
system/conda/既存別venv/driver/firmwareへの変更なし。
native libraryも既存のプロジェクトローカル版を参照するだけです。

```bash
/home/ubuntu/.venvs/g1-game-vision/bin/python -B -m pytest \
  -o cache_dir=.runtime/cache/pytest-camera-full
```

追加テストはCLI、wired NIC選択とdirect route、JPEG正常/不正、初回/切断後timeout、
GUI終了・fullscreen、SDK XML復元、native abort表示を実機なしで検証します。
既存のdistance/invalid depthテストも実行対象です。person-depth未実装のため、その判定テストは未追加です。
全体テスト結果: **291 passed, 1 skipped**。`--simulate --robot mock --speech console`も正常終了。

復元する場合はviewerを終了し、新規の`tools/g1_camera_minimal.py`、
`tests/test_g1_camera_minimal.py`、この文書を削除します。
`adapters/g1_robot.py`の追加method `load_camera_symbols`と、`G1_GAME_VISION.md`冒頭の
今回の追記だけを取り除けばコードは元に戻ります。他の変更をまとめてresetしないでください。
専用venvと`.runtime/cyclonedds`は今回以前からあるため、今回の復元では削除不要です。
テストcacheは`.runtime/cache/pytest-camera*`にあります。削除しても実行に影響しません。
今回の診断で既存`/tmp/cdds.LOG`にSDKの設定traceが書かれた可能性がありますが、
最終版はTracingを無効化しており、今後この共有ログには書きません。core dumpも無効化しています。
