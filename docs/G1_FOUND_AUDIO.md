# G1 person / banana / plushie → cached audio + one safe reaction

**2026-09-10: 2画面＋YOLOからG1本体の音声再生に成功し、ユーザーが「聞こえます」と確認しました。**
PC側でカメラと音声のDDSを同時使用すると3102/3104が発生しました。接続共有や取得頻度低減でも
改善しなかったため、その試作は撤回。既存のカメラ処理は元のままです。
音声だけ、認証済みSSH経由でG1上の既存SDKを使います。厳密なDDS通信不調の原因は未確定です。
G1の既存Python 3.8、unitree_sdk2py、cycloneddsを使用し、G1へファイル配置・インストールはしていません。
音量はユーザー指定で85。カメラサービス/SDK/system設定には変更を加えていません。

Reactionは明示的に `--found-audio` を指定した場合だけ有効です。G1内蔵カメラのYOLO結果だけを使います。
person、banana、plushieを同時検出した場合はperson → banana → plushieの順に優先し、
音声やmotionを重ねたりqueueへ溜めたりしません。plushieはYOLO COCOの`teddy bear`（class 77）を意味します。
object音声の再生中に高優先度の対象が現れた場合は、再生を途中で切らず、終了後に判定します。
従来のカメラreader・YOLO worker・USB表示と固定WAVを維持し、発火先を初期実装から残る
`ReactionEngine`へ戻しました。TTS生成は追加しません。

`--robot mock`（既定）はmotion名を表示するだけです。実機はDesktop側DDSを使用せず、
`--robot g1-ssh --enable-real-robot --g1-motion safe-actions --execute-real-action` と
`G1_ALLOW_REAL_ACTION=1` の全gateが必要です。選択したmotionは既存の `notice` だけです。
認証済みSSH経由で起動したG1-local helperが公式 `G1ArmActionClient.GetActionList()` を確認し、
ID 23とrelease ID 99が存在する場合のみ `ExecuteAction(23)`、2秒後に
`ExecuteAction(99)` を1回ずつ送ります。歩行、locomotion、新規joint trajectoryはありません。

## 初期設定

`config/person_found_audio.yaml`: found_duration=.3秒、dropout_grace=.15秒、audio_cooldown=2秒、rearm_absence=1秒。
confidenceは既存の `--yolo-confidence`（既定.25）と共通です。
`--found-duration` / `--detection-grace` / `--audio-cooldown` / `--rearm-absence` で変更できます。

最新指示「見つけている間ずっと喋る」を受け、映り続ける間は1回だけに変更しました。
SEARCHING → DETECTING → Reaction（motion＋音声）1回 → COOLDOWN → FOUND - WAITING FOR CLEAR。
クールダウンは発火時点から2秒。その後、人がいない新しい推論結果が1秒以上継続した場合だけSEARCHINGへ戻ります。
再登場時は改めて0.3秒のperson検出が必要。短い検出抜けでは再発火しません。
YOLO停止・カメラ切断・古い結果の再利用を「人が去った」とは扱いません。
個人識別はしないため、複数人の場合も全員が画角からいなくなるまで再発火しません。
単発frameや、同じ推論結果をGUIが繰り返し読むだけでは発火しません。
短い検出抜けは.15秒まで許容。カメラ・推論の停止、YOLO OFF、長い検出抜けで検出継続時間を破棄します。
Reaction全体を抑制し、COOLDOWN中もYOLO、映像、枠は継続。手動リセット `r` は追加していません。

## 音声

2026-09-12に旧hash名WAV 31個を整理しました。使用中のperson音声とユーザー提供のbanana音声だけを残し、
不要な旧WAV 30個を削除しました。今後は `assets/audio/reactions/<対象>/<イベント>.wav` で追加します。
2026-09-13にユーザー提供のplushie音声を追加しました。この3ファイルはGit管理対象で、
cloneした環境でもそのまま利用できます。

- `assets/audio/reactions/person/detected.wav`: PCM WAV、44,100 Hz、16bit、mono（旧f0e5...音声）
- `assets/audio/reactions/banana/detected.wav`: PCM WAV、44,100 Hz、16bit、mono（banana_surprised.wav）
- `assets/audio/reactions/plushie/plushie_affectionate.wav`: PCM WAV、44,100 Hz、16bit、mono

bananaも0.3秒継続検出後に1回だけ再生します。実機で短い検出抜けが見られたためbananaだけ0.30秒まで
検出抜けを許容し、画面から1秒以上消えた後に再発火可能です。
plushieは0.3秒継続、1.0秒dropout grace、2秒cooldown、3秒の明示的不在で、
画面に映り続ける間はユーザー提供音声を1回だけ再生します。
実際に使ったフルパスは `FOUND AUDIO: /home/.../xxxx.wav` とconsoleに出力します。

## Plushie AFFECTION / JOY minimal integration

`--robot motiondecode` を選んだ場合だけ、confirmed plushie eventを既存の
`ReactionEvent.YOLO_PLUSHIE_FOUND`として共有Reaction Engineへ渡し、実機validated済みの
`motiondecode:surprise`（上半身 arms 50% / waist 25%、脚trajectoryなし）と
`plushie_affectionate.wav`を同じjobから並列開始します。`motiondecode:joy`は所有runtimeで
`real_g1_validated=false`のため実機経路ではfail-closedです。
この新規modeではperson/bananaは表示・ログだけに残し、Reaction選択には入れません。
既存robot modeのperson → banana → plushie優先規則はそのままです。

wireless G1 JPEG経路で実測した低い有効frame rateに対し、confirmation thresholdは
0.3秒のまま、plushieだけdropout graceを1.0秒、rearm連続陰性を3.0秒にします。同一resultの再利用は拒否され、
camera/YOLO停止・stale・新規観測なしでは不在時間を進めません。また、最後のplushie領域の10%以上を覆うperson誤分類も不在として数えません。
異なる2つ以上のfresh positive frameがない限り発火しません。

`--quiet-mode`は再生用の一時PCMだけを`config/yolo_objects.yaml`の
`quiet_mode_gain_db`（会場調整値 -24 dB）で減衰します。元WAVとOS/G1のvolume設定は変更しません。
実機MotionDecodeでは`--enable-real-robot --confirm-site-ready --quiet-mode`をすべて必須とします。
固定音声なら `--found-sound /absolute/path.wav`。存在・非空PCM WAVを起動前に検証します。

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
音声経路は移動・姿勢・関節・腕・LED・TTS生成のAPIを使用しません。実機motionをopt-inした場合だけ、
上記の検証済みArm Action 23/99を`SshG1SafeActionAdapter`から使用します。
system設定・依存packageの変更なし。

明示的に `--found-output pc` を指定した場合のみ、追加したLinuxAplayOutputで
PCの既存 `/usr/bin/aplay` → ALSA default（このPCではPipeWire）へ出力できます。

Reaction Engineはmotion用worker 1つとaudio用の固定1-worker executorを使用し、1イベントにつき
audioとmotionを最大1回ずつほぼ同時に開始します。実行中の発火を受け付けず、長い音声やmotionを重ねません。
motion失敗はaudioを止めず、audio失敗もG1-local helperのAction 99 cleanupを妨げません。
再生エラー時は音声だけ無効化し、画面にAUDIO ERRORを表示。カメラ・YOLOは継続します。
motion例外時はその例外をcamera loopへ返さず、以後のmotionを無効化して自動retryしません。
終了時は専用SSHのstdinを閉じ、G1のhelperはEOFで自分のstreamを停止して終了します。
既存USB送信やG1サービスを音声処理から停止しません。

## 起動

追加USBを外してG1起動 → 内蔵映像確認 → USB追加、認証済み一時SSH接続が有効な状態で実行します。
既存viewerと二重起動せず、先にqで終了してください。

ハードウェアへ音声・motionを送らないMock確認:

```bash
/home/ubuntu/.venvs/g1-game-vision/bin/python -B /home/ubuntu/dev/g1-bottle-reaction/tools/g1_dual_camera.py --usb-bind 10.42.0.1 --usb-host 10.42.0.76 --network-interface wlp128s20f3 --no-usb-camera --windowed --yolo --yolo-confidence 0.25 --banana-confidence 0.25 --plushie-confidence 0.25 --found-audio --found-output mock --robot mock
```

```bash
/home/ubuntu/.venvs/g1-game-vision/bin/python -B /home/ubuntu/dev/g1-bottle-reaction/tools/g1_dual_camera.py --usb-bind 192.168.123.200 --usb-host 192.168.123.164 --start-usb-sender --ssh-control /home/ubuntu/dev/g1-bottle-reaction/.runtime/usb-camera-ssh/control --usb-rotate 180 --windowed --yolo --yolo-confidence 0.25 --banana-confidence 0.25 --plushie-confidence 0.25 --found-audio --found-output g1 --found-duration 0.3 --audio-cooldown 2.0
```

Ubuntu=`10.42.0.1`、G1 Wi-Fi=`10.42.0.76`の完全無線構成では、同じplushie対応版を次のように起動します。
指定するControlMaster socketは`unitree@10.42.0.76`へ接続した無線用socketである必要があります。
公開鍵認証だけで接続できない場合は、先に次の認証用接続を作成します（remote commandは実行しません）。

```bash
ssh -M -S /home/ubuntu/dev/g1-bottle-reaction/.runtime/usb-camera-ssh/wifi-control -o ControlPersist=600 -fN unitree@10.42.0.76
```

```bash
G1_ALLOW_REAL_ACTION=1 /home/ubuntu/.venvs/g1-game-vision/bin/python -B /home/ubuntu/dev/g1-bottle-reaction/tools/g1_dual_camera.py --usb-bind 10.42.0.1 --usb-host 10.42.0.76 --network-interface wlp128s20f3 --ssh-control /home/ubuntu/dev/g1-bottle-reaction/.runtime/usb-camera-ssh/wifi-control --g1-camera-transport ssh-rtp --g1-camera-port 56001 --g1-camera-fps 30 --no-usb-camera --windowed --yolo --yolo-confidence 0.25 --banana-confidence 0.25 --plushie-confidence 0.25 --found-audio --found-output g1 --found-duration 0.3 --audio-cooldown 2.0 --robot g1-ssh --enable-real-robot --g1-motion safe-actions --execute-real-action
```

`--ssh-target`省略時は`unitree@10.42.0.76`が自動選択され、USB senderとG1 speakerが同じSSH先を使います。
G1上の音声helperにある`eth0`はG1内部のUnitree DDS用であり、UbuntuからG1へのSSH宛先ではありません。

キー: y=YOLO切替、b=枠切替、1/2/3=表示切替、f=fullscreen、q/Esc=終了。r操作は不要です。

変更: config/person_found_audio.yaml、config/yolo_objects.yaml、game_vision/person_yolo.py、
game_vision/found_audio.py、adapters/cached_audio.py、game_vision/app.py、game_vision/dual.py、
tests/test_person_yolo.py、tests/test_found_audio.py、reaction WAV、この文書。
G1出力対応: adapters/g1_robot.py（音声限定SSH helper）、adapters/g1_audio.py、tests/test_g1_audio.py、tests/test_g1_cached_ssh.py。
tools/g1_cached_sound.pyは単独切り分け用で、通常の2画面起動では使いません。

自動テスト: 453 passed / 1 skipped。新規依存の追加なし。
再発火抑制の修正後、ユーザーが「うまくいきました」と実機動作を確認しました。
G1-local SDK経由の統合試験中もcamera約41 FPS / USB29.5 FPS / YOLO14.6 FPSを維持。
音声APIの成功と、実際の聞こえ方は別に確認します。

## 表示と配布

カメラ画像は上部のカメラ情報欄・下部のYOLO/音声状態欄と分離しています。
アスペクト比を保って画像全体を表示し、黒い情報帯で画角を覆いません。検出枠は画像領域に合わせて描画します。
Gitには上記3つのreaction WAVを含めます。AivisSpeechの生成キャッシュ、モデル、venv、SDK checkout、
一時SSH接続や認証情報は含めません。別PCでもclone後は既定音声をそのまま利用できます。
G1再起動後は一時SSH接続とUSB送信が終了するため、SSH再認証後にviewerを起動し直す必要があります。
