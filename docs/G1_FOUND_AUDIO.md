# G1 person → cached audio (debug cooldown)

**2026-09-10: 2画面＋YOLOからG1本体の音声再生に成功し、ユーザーが「聞こえます」と確認しました。**
PC側でカメラと音声のDDSを同時使用すると3102/3104が発生しました。接続共有や取得頻度低減でも
改善しなかったため、その試作は撤回。既存のカメラ処理は元のままです。
音声だけ、認証済みSSH経由でG1上の既存SDKを使います。厳密なDDS通信不調の原因は未確定です。
G1の既存Python 3.8、unitree_sdk2py、cycloneddsを使用し、G1へファイル配置・インストールはしていません。
音量はユーザー指定で85。カメラサービス/SDK/system設定には変更を加えていません。

音声は明示的に `--found-audio` を指定した場合だけ有効です。G1内蔵カメラのYOLO結果だけを使います。
従来のカメラreader・YOLO worker・USB表示は変更せず、動作命令・TTS生成は追加しません。

## 初期設定

`config/person_found_audio.yaml`: found_duration=.3秒、dropout_grace=.15秒、audio_cooldown=2秒、rearm_absence=1秒。
confidenceは既存の `--yolo-confidence`（既定.25）と共通です。
`--found-duration` / `--detection-grace` / `--audio-cooldown` / `--rearm-absence` で変更できます。

最新指示「見つけている間ずっと喋る」を受け、映り続ける間は1回だけに変更しました。
SEARCHING → DETECTING → 音声1回 → COOLDOWN → FOUND - WAITING FOR CLEAR。
クールダウンは発火時点から2秒。その後、人がいない新しい推論結果が1秒以上継続した場合だけSEARCHINGへ戻ります。
再登場時は改めて0.3秒のperson検出が必要。短い検出抜けでは再発火しません。
YOLO停止・カメラ切断・古い結果の再利用を「人が去った」とは扱いません。
個人識別はしないため、複数人の場合も全員が画角からいなくなるまで再発火しません。
単発frameや、同じ推論結果をGUIが繰り返し読むだけでは発火しません。
短い検出抜けは.15秒まで許容。カメラ・推論の停止、YOLO OFF、長い検出抜けで検出継続時間を破棄します。
音声だけを抑制し、COOLDOWN中もYOLO、映像、枠は継続。手動リセット `r` は追加していません。

## 音声

既存の `/home/ubuntu/dev/g1-bottle-reaction/.cache/tts/` 内に31 WAVを確認しました。
すべてMicrosoft PCM WAV、44,100 Hz、16bit、mono、長さ0.52〜3.01秒。
キャッシュ名はSpeechCacheのSHA256（text/speaker UUID/style/profile等）ですが、
話者UUID/styleを含む対応表がなく、ファイル名からセリフを特定できませんでした。
最新のユーザー指定により、既定音声は次の1ファイルに固定しました。

`/home/ubuntu/dev/g1-bottle-reaction/.cache/tts/f0e5a8e8bb3d40781278d8f577044bea1b63ba198655936ef908a15f3e9ccb9b.wav`

PCM WAV、44,100 Hz、16bit、mono、1.378秒です。
実際に使ったフルパスは `FOUND AUDIO: /home/.../xxxx.wav` とconsoleに出力します。
明示的に `--found-sound random` を指定した場合のみ、以前のランダム選択も使用できます。

固定音声なら `--found-sound /absolute/path.wav`。存在・非空PCM WAVを起動前に検証します。
キャッシュを追加生成・上書きしたり、31件をまとめて試聴したりしません。

ユーザーの指定により、既定出力は**G1本体スピーカー**です。
既存 `AudioOutput.play_wav()` と `G1AudioOutput` の変換・送信間隔・終了処理を再利用します。
PCの専用threadが1つのSSH接続を維持し、PCMだけをG1へ送ります。G1内のAudioClientが再生します。
既存変換器で16kHz/16bit/mono PCMに変換し、公式AudioClientのPlayStream / PlayStopだけを使用します。
0.5秒分ずつ送信・待機して長い音声の途中切断を避けます。
G1AudioOutputに `volume=None` を追加し、通常再生ではSetVolumeを呼ばず現在のG1音量を維持します。
ユーザーの「音量ちょっと大きくして」に従い、一度だけ80→85へ変更しGetVolumeで85を確認しました。
旧呼び出しの既定volume=85は変更しません。
DDSのtrace-free初期化をG1の音声専用process内で行います。音声失敗でもPCのカメラ/GUIは落としません。
送信前にG1内でGetVolumeの応答を確認。最大10秒の準備確認で再試行するのは読み取りだけで、
音声送信自体を自動再試行しません。SSHはBatchModeで実行しGUIでパスワード待ちにはなりません。
一時SSH接続が切れた場合は、端末で再認証してviewerを起動し直してください。
移動・姿勢・関節・腕・LED・TTS生成のAPIは使用しません。
system設定・依存packageの変更なし。

明示的に `--found-output pc` を指定した場合のみ、追加したLinuxAplayOutputで
PCの既存 `/usr/bin/aplay` → ALSA default（このPCではPipeWire）へ出力できます。

専用threadは1つだけ。再生中の発火を受け付けず、再生queueは作らず、長い音声も重ねません。
再生エラー時は音声だけ無効化し、画面にAUDIO ERRORを表示。カメラ・YOLOは継続します。
終了時は専用SSHのstdinを閉じ、G1のhelperはEOFで自分のstreamを停止して終了します。
既存USB送信やG1サービスを音声処理から停止しません。

## 起動

追加USBを外してG1起動 → 内蔵映像確認 → USB追加、認証済み一時SSH接続が有効な状態で実行します。
既存viewerと二重起動せず、先にqで終了してください。

```bash
/home/ubuntu/.venvs/g1-game-vision/bin/python -B /home/ubuntu/dev/g1-bottle-reaction/tools/g1_dual_camera.py --usb-bind 192.168.123.200 --usb-host 192.168.123.164 --start-usb-sender --ssh-control /home/ubuntu/dev/g1-bottle-reaction/.runtime/usb-camera-ssh/control --usb-rotate 180 --windowed --yolo --yolo-confidence 0.25 --found-audio --found-output g1 --found-duration 0.3 --audio-cooldown 2.0
```

キー: y=YOLO切替、b=枠切替、1/2/3=表示切替、f=fullscreen、q/Esc=終了。r操作は不要です。

変更: config/person_found_audio.yaml、game_vision/found_audio.py、adapters/cached_audio.py、
game_vision/app.py、game_vision/dual.py、tests/test_found_audio.py、この文書。
G1出力対応: adapters/g1_robot.py（音声限定SSH helper）、adapters/g1_audio.py、tests/test_g1_audio.py、tests/test_g1_cached_ssh.py。
tools/g1_cached_sound.pyは単独切り分け用で、通常の2画面起動では使いません。

自動テスト: 357 passed / 1 skipped（6.39秒、表示欄分離後）。mockの--simulateも完走。新規依存の追加なし。
再発火抑制の修正後、ユーザーが「うまくいきました」と実機動作を確認しました。
G1-local SDK経由の統合試験中もcamera約41 FPS / USB29.5 FPS / YOLO14.6 FPSを維持。
音声APIの成功と、実際の聞こえ方は別に確認します。

## 表示と配布

カメラ画像は上部のカメラ情報欄・下部のYOLO/音声状態欄と分離しています。
アスペクト比を保って画像全体を表示し、黒い情報帯で画角を覆いません。検出枠は画像領域に合わせて描画します。
Gitには音声キャッシュ、モデル、venv、SDK checkout、一時SSH接続や認証情報を含めません。
別PCでは既存のPCM WAVを用意して `--found-sound /absolute/path.wav` で指定してください。
G1再起動後は一時SSH接続とUSB送信が終了するため、SSH再認証後にviewerを起動し直す必要があります。
