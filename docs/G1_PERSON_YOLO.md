# G1内蔵カメラ限定・COCO person + banana実験

2026-09-12にCOCO `banana`（class 46）を追加しました。person（class 0）のconfidence 0.25は維持し、
bananaのconfidenceはユーザーの最新指定によりpersonと同じ0.25です。`config/yolo_objects.yaml`で既定値を管理し、
`--banana-confidence`で一時変更できます。bananaは枠・confidence・console遷移を表示します。
2026-09-12にユーザー提供音声を追加し、person優先でbanana検出音声も1回だけ再生します。
bananaによるG1動作やゲームイベントはまだ追加していません。

実装と実機人物検出を確認済み（2026-09-10）。距離・身体部位別の系統的な検出可否は未測定です。
USBには推論しません。ロボット制御、距離推定、ゲーム判定、追跡は追加していません。

## 構成

従来のG1/USB取得processと最新frame slotを維持。G1最新frameだけを別のYOLO processに渡します。
推論送受信は専用thread、in-flightは1枚のみです。推論中のframeは蓄積せず、終了後に最新を選びます。
camera取得・GUIは推論を待ちません。native crashも別process内に隔離します。
500ms以上古い結果やカメラLOST時には枠と検出判定を無効化（STALE）します。
枠は過去の推論frame由来で、最新映像と厳密同期していません。追跡・予測補間はしません。

モデル: COCO pretrained YOLO11n、imgsz=640、person class=0、banana class=46、
各confidence=.25、推論上限15 FPS。
G1取得は1920x1080のまま、従来のローカルpipe画像960x540から推論します。
CUDA warmup失敗時だけCPU fallback。モデルは事前配置が必要で、viewer内では自動downloadしません。

キー: 1=G1 / 2=USB / 3=dual / f=fullscreen / q,Esc=終了、y=YOLO ON/OFF、b=枠ON/OFF。
OFFで実行中の1枚は完了することがありますが、結果は破棄します。USBは180度回転を維持。
PERSON DETECTED / LOSTは状態変化時だけ出力。性能統計は5秒間隔。

## PC環境

既存venv `/home/ubuntu/.venvs/g1-game-vision` のみに追加。
主要追加: torch 2.10.0+cu128、torchvision 0.25.0+cu128、ultralytics 8.4.146。
依存としてCUDA runtime/cuDNN等のPython wheels、triton、pillow、matplotlib、scipy不要の
Ultralytics依存群（polars、requests、psutil、ultralytics-thop/platform等）を同venvへ追加しました。
system CUDA toolkit / driver / system Python / apt / conda / G1ファイル変更はありません。
既存NumPy 2.5.3、OpenCV 4.14.0.94、DDS等はconstraintsで固定して維持しました。
追加前バージョン: `.runtime/yolo-existing-constraints.txt`。pip check成功。
追加packageの全バージョンは [G1_PERSON_YOLO_PACKAGES.txt](G1_PERSON_YOLO_PACKAGES.txt) に記録しました。

GPU: RTX 5060 Ti、driver 595.84。torch.cuda.is_available()=True、CUDA行列演算成功。
空画像30回のYOLO予備測定: 平均5.31ms（5.11〜5.65ms）。実機人物画像の性能ではありません。
GPUモデル読込・warmup成功。実機性能は次節参照。

## 復帰後の実機結果

ログ `.runtime/yolo-live-recovered-210s.log`。ユーザーから長時間試験は不要との連絡があり、追加の連続試験は行いません。
設定済みの210秒試験は終了しており、両映像の連続LIVE区間は207.7秒でした。

| 指標 | 結果 |
| --- | --- |
| model / device / confidence | YOLO11n / CUDA RTX 5060 Ti / .25 |
| inference | 3050回、平均14.6 FPS、平均5.4ms |
| G1 camera | 1920x1080、8826 frames、平均取得42.2 FPS |
| USB camera | 1280x720、6122 frames、平均29.5 FPS、既存stream維持 YES |
| display | loop約60 FPS、12482回。unique frame数やmonitor refresh数ではない |
| display skips | G1 44、USB 76（latest slot上書き、通信欠損ではない） |
| USB RTP lost packets | 0 |
| person | 実機検出成功、検出開始confidenceの例 .64 / .67 |
| additional latency | 厳密な撮影→表示遅延の増分は未測定。ユーザーが追従良好と確認 |

YOLO result ageはPC取得完了から表示ループまでであり、推論だけの時間や撮影からの遅延とは違います。
推論結果が古くても映像自体を待たせず、500ms超の結果は無効化します。
今回の結果は実験ツールが動くことの確認で、ステルスゲームの成立や各距離での検出率の保証ではありません。

モデル: `.runtime/models/yolo11n.pt`（5,613,764 bytes）
SHA256: `0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1`
[公式重み](https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt)、
[YOLO11公式説明](https://docs.ultralytics.com/models/yolo11/)、
[predict引数](https://docs.ultralytics.com/modes/predict/)、
[PyTorch公式wheel組合せ](https://pytorch.org/get-started/previous-versions/)。
設定/cacheはプロジェクトの `.runtime/yolo/` 内、Ultralytics syncはOFF。

## バッテリー交換後の阻害要因

交換前の2カメラ試験結果は `G1_USB_DUAL.md` 参照。
交換後、USBは1280x720/約29.5 FPSで復帰しましたが内蔵映像は0 frames。
YOLOなしの従来 `g1_camera_minimal.py` でも3102/3104 timeoutを再現しました。

読み取り確認した事実:

- 公式設定 `/unitree/etc/master_service/service/video_hub_pc4` は `/dev/video4` を固定指定。
- 交換前のRealSense RGB相当index0はvideo4、交換後はvideo6。
- 追加USBがvideo6からvideo0になり、RealSenseはvideo2〜7へ移動。
- `videohub_pc4` processは不在。chestのみ稼働。
- master_serviceログで `monitor start child service error ... ret:30008` と
  `current service is no longer protected ... protect count:30` を確認。

USB接続状態での起動によるデバイス番号変更が、標準内蔵サービスの起動失敗原因と考えられます。
サービスの停止・起動・設定編集は行っていません。
次はユーザーに、安全な電源操作で追加USBを抜いた状態の起動を依頼し、
内蔵映像復帰を確認してから追加USBを挿す方法で切り分けます。

追記: 追加USBを外して再起動した後、RealSense index0は `/dev/video4` に戻り、
`videohub_pc4` PID1777の稼働を確認しました。従来の最小viewerで1920x1080、
8.0秒/407 frames、平均取得50.7 FPS、errors=0、max gap=46ms。内蔵映像は復帰しました。
追加USBの接続後、USBはvideo6となり、内蔵video4と標準service PID1777は維持されました。
2画面＋CUDA YOLOが動作し、G1映像でPERSON DETECTED（例confidence=.64）を取得しました。
ユーザー目視結果: 「全て出ています。少しでも画面内に入るるとわかります」。
追従・y/b切替の確認依頼への回答: 「いけてます」。
今回の試行で一部が画角に入った段階で検出できたという報告です。
どの身体部位・何mで成功したかや、任意の小さな断片で検出できることを保証する結果ではありません。

## 起動（内蔵映像復帰後）

認証済み一時SSH socketが必要です。バッテリー交換で切れた場合は再認証が必要です。
パスワードを保存・コマンド引数化しません。

```bash
/home/ubuntu/.venvs/g1-game-vision/bin/python -B /home/ubuntu/dev/g1-bottle-reaction/tools/g1_dual_camera.py --usb-bind 192.168.123.200 --usb-host 192.168.123.164 --start-usb-sender --ssh-control /home/ubuntu/dev/g1-bottle-reaction/.runtime/usb-camera-ssh/control --usb-rotate 180 --windowed --yolo --yolo-confidence 0.25
```

## 手動測定表

距離を画像から推測しません。静止したG1と安全監視者のもとで床の距離を手動測定します。
R3動作中の遅延確認は別途実施し、映像停止・遅延時は歩行を止めてください。

| 手動距離 | 画角内の部位 | person YES/NO | confidence |
| --- | --- | --- | --- |
| 5m | 未測定 | 未測定 | — |
| 4m | 未測定 | 未測定 | — |
| 3m | 未測定 | 未測定 | — |
| 2m | 未測定 | 未測定 | — |
| 1m | 未測定 | 未測定 | — |
| 直前 | 未測定 | 未測定 | — |

全身・腰から下・膝から下・脚のみ、すべて未評価。未検出を安全や人物不在の根拠にしません。

## 今回の変更

- 追加: `src/g1_bottle_reaction/game_vision/person_yolo.py`
- 追加: `tests/test_person_yolo.py`、`docs/G1_PERSON_YOLO.md`、`docs/G1_PERSON_YOLO_PACKAGES.txt`
- 変更: `src/g1_bottle_reaction/game_vision/dual.py`（G1限定推論hook/overlay/キー/統計）
- 変更: `src/g1_bottle_reaction/game_vision/app.py`（明示的opt-in CLI）
- 案内追記: `docs/G1_GAME_VISION.md`、`docs/G1_USB_DUAL.md`

既存取得helper、G1CameraSource、Unitree adapter、USB senderには今回変更なし。
自動テストは模型workerでON/OFF、class/confidence filtering、複数人・なし、描画、
USB不変、遅い推論で中間frameを蓄積しないこと、推論process障害を確認します。
自動テスト: 322 passed / 1 skipped、pip check成功、git diff --check成功。
