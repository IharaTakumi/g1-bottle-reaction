# G1 Bottle Reaction Prototype

Windows 11上でWebカメラのペットボトルを検出し、時間方向の状態変化からMock Robotの動作表示とPC音声を発生させる先行プロトタイプです。現在の主開発環境は **Python 3.13** です。default起動ではUnitree G1へ接続せず、Phase 1実機経路だけを明示的なCLIで有効化します。

対応Pythonは **3.10以上** です。通常のWindows開発環境はPython 3.13を維持し、公式`unitree_sdk2_python`と`cyclonedds==0.10.2`を使うG1用venvはPython 3.10を使用します。Python 3.10/3.11ではlegacy TeleImagerとも共存可能なNumPy 1.26系、Python 3.12以降ではNumPy 2系をdependency markerで選択します。

## Windowsセットアップ

PowerShellで次を実行します。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

`pyproject.toml` の通常依存関係はWindows/Python 3.13でインストールできる範囲を指定しています。Unitree SDKは通常依存にもoptional dependencyにも含めていないため、Windowsにはインストールされません。

音楽リアクションも使う場合はaudio extraを追加します。

```powershell
python -m pip install -e ".[audio,dev]"
```

TensorFlow/YAMNet/sounddevice/SciPyはaudio extraだけに含まれます。Bottle-only環境はTensorFlowなしで従来どおりimport・実行できます。

G1用Python 3.10環境は通常環境と分離します。

```powershell
py -3.10 -m venv .venv-g1
.\.venv-g1\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip install -e C:\dev\unitree_sdk2_python
```

Unitree公式packageが要求する`cyclonedds==0.10.2`は変更しません。Unitree SDKは通常のWindows 3.13環境へは入れず、`.venv-g1`だけへ導入します。TeleImagerは標準G1 cameraには不要です。

WindowsのUnitree DDS初期化では、`--network-interface`をWindowsの`InterfaceAlias`として`Get-NetIPAddress`でIPv4へ解決します。公式SDKを呼ぶ間だけ`ChannelConfigHasInterface`を`NetworkInterface address="..."`形式へ一時差し替え、Linux用`/tmp/cdds.LOG` tracingを除外します。SDK repositoryのファイル自体は変更せず、呼び出し後は元のconfigへ戻します。直接IPv4を指定する場合は`--network-address`を使用できます。

```powershell
python -m g1_bottle_reaction --g1-test connection --network-interface "イーサネット 3"
python -m g1_bottle_reaction --g1-test connection --network-address 192.168.123.222
```

Linuxでは従来どおり`--network-interface eth0`を公式name方式へ渡し、config差し替えを行いません。

## 実行

カメラもモデルも不要なsimulation:

```powershell
python -m g1_bottle_reaction --simulate --robot mock --speech console
```

Webカメラ:

```powershell
python -m g1_bottle_reaction --robot mock --camera 0 --speech auto
```

`q`またはEscで終了します。`--speech auto` は日本語のWindows音声が利用できる場合に読み上げ、利用できなければconsole表示へフォールバックします。`--speech console` と `--speech mute` も選択できます。

初回のカメラ実行時、Ultralyticsが `yolo11n.pt` をダウンロードする場合があります。モデル名またはローカルパスは `config/default.yaml` の `vision.model` で変更できます。自動テストとsimulationはUltralytics、モデル、Webカメラ、インターネットを使用しません。

## Smartphone Stealth Game

スマートフォンを人間の代わりの`PLAYER`として使う、Windows向けステルスゲームです。YOLOは現在COCOの`cell phone`を検出しますが、Target Perceptionがsemanticな`PLAYER`へ変換するため、ゲームルールはraw class名を知りません。将来は`stealth_game.target.detector_label`を`person`へ変更するだけで同じゲーム状態、追跡、Reactionを利用できます。既存Bottle modeはdefaultのまま残り、ゲームは`--game stealth-phone`で明示的に有効化します。

```text
Webcam -> YOLO cell phone -> Target Perception -> PLAYER observation
  -> StealthGameEngine -> discrete Game Events -> shared Reaction Engine
  -> TargetTrackingController -> preview-only MuJoCo waist yaw
```

警戒値は0–100です。PLAYERが確認されると、confidence、bbox面積、画面中央への近さを重み付き平均した`visibility_score`に応じて、`gain_per_second * visibility_score * dt`で上昇します。見失うと`decay_per_second * dt`で低下します。フレーム数ではなく`time.monotonic()`由来の経過時間を使います。既定stateは`UNAWARE -> SUSPICIOUS -> ALERT -> FOUND -> GAME_OVER`です。初期`SUSPICIOUS`閾値は、0.2秒の検出確認後でも約0.4–0.5秒のチラ見せをゲーム上の「気づき」にできるよう10に設定し、ALERT / FOUNDは65 / 100を維持しています。敏感すぎる環境では25程度へ上げてください。

`detection_confirm_seconds`と`lost_grace_seconds`がYOLOの短い揺れを吸収します。離散イベントは既存Reaction queueへ入り、`SUSPICION_STARTED=notice +「……ん？」`、`ALERT_STARTED=guard +「誰かいるな」`、`RETURNED_TO_UNAWARE=stand +「気のせいか……」`、`PLAYER_FOUND=spot_target +「そこだ！」`として順次実行されます。Game eventには通常Reactionより高いpriorityと明示的なcooldown bypassがあり、`PLAYER_FOUND`を落としません。ゲームがUNAWARE以外の間はMusic reactionを抑制します。

PLAYER追跡は高頻度制御なのでReaction Engineを使いません。`TargetTrackingController`はraw observationとgame stateから`TrackingCommand`（visible、desired/actual yaw、strength、state、status、last-seen時間）を生成します。center Xはdeadzoneを除いた範囲を最大±25度へ線形変換し、`invert_x`で左右反転できます。actual yawは`alpha = 1 - exp(-response * dt)`の一次応答で補間するためFPS非依存です。初期responseはSUSPICIOUS=2、ALERT=4、FOUND=6、recenter=1.5です。

MuJoCoでは公式29-DoF modelから実在を確認した`waist_yaw_joint`へ、`base motion yaw + tracking offset`として加算し、モデルrange内へclampします。実モデルrangeは±2.618 radですが、controller側を±25度に制限します。検出消失後0.5秒は最後の方向を保持し、その後recenterします。FOUNDで最後の方向をlockし、GAME_OVER中の新しい位置入力は無視します。R resetはdesired yawとlast-seen/lockを消去し、滑らかに正面へ戻します。実G1 adapterのtrackingは安全なno-opで、未確認Unitree APIは呼びません。

カメラやYOLOなしでゲーム全体を確認します。

```powershell
python -m g1_bottle_reaction --robot mujoco --preview-tracking
python -m g1_bottle_reaction --simulate-stealth --robot mock --speech console
python -m g1_bottle_reaction --simulate-stealth --robot mujoco --speech mute
```

WebcamでスマホをPLAYERとして使います。OpenCV画面にはraw detection、PLAYER、confidence、位置、面積、visibility、警戒ゲージ、state、last eventを表示します。GAME OVER後は`R`で警戒値、target、tracking、overlayをresetし、`Q`またはEscで終了します。

```powershell
python -m g1_bottle_reaction --game stealth-phone --robot mock --camera 0 --speech console
python -m g1_bottle_reaction --game stealth-phone --robot mujoco --camera 0 --speech mute
python -m g1_bottle_reaction --game stealth-phone --robot mujoco --camera 0 --speech aivis --tracking-debug
```

`--tracking-debug`では中央縦線、PLAYER bbox中心、desired/actual/error yaw、tracking status、last-seen経過時間を表示します。左右が逆に見える場合は`stealth_game.tracking.invert_x: true`へ変更してください。

閾値、増減速度、visibility weight、確認時間、GAME OVER delay、tracking角度・応答速度、manual/auto resetは`config/default.yaml`の`stealth_game`で調整できます。defaultは`auto_reset: false`です。詳細は[docs/STEALTH_GAME.md](docs/STEALTH_GAME.md)を参照してください。

Windows Webcamは固定で、MuJoCo G1の向きが変わってもカメラ画像は変化しません。**Webcam view does not change when Virtual G1 rotates.** 現在のtrackingは演技確認用previewであり、closed-loop camera simulationではありません。実機移行時はTarget Perceptionとゲーム層を保ち、安全確認済みの`RobotAdapter.apply_tracking()`実装だけを交換します。

## Music reaction

明示的にAudioを有効にした場合だけ、次の経路を追加します。デフォルトの `--audio-source none` はマイクへアクセスしません。

```text
Windows microphone -> 16 kHz mono PCM -> official YAMNet
  -> MusicStateTracker -> MUSIC_STARTED
  -> shared Reaction Engine -> little_dance + 「お、いいね」
```

### Audio deviceの確認

```powershell
python -m g1_bottle_reaction --list-audio-devices
```

先頭が `*` のdeviceがOS default inputです。特定deviceは一覧のindexまたは名前で `--audio-device` に指定します。省略時はdefault inputを使用します。

一覧には `host_api` も表示します。RAW captureで明示的にdeviceを指定する場合は、`host_api=Windows WASAPI` の入力deviceを選んでください。指定なしの場合はWASAPIのdefault input endpointを選びます。

### Windows audio mode

`--audio-mode` はアプリの入力streamだけに作用し、Windowsのグローバル設定、レジストリ、ドライバ設定を変更しません。

- `normal`: 従来どおりの通常capture
- `raw`: WASAPI shared-mode streamへPortAudioの `eStreamOptionRaw` を要求。利用できなければwarningを出してnormalへfallback
- `auto`: RAWを試し、利用できなければnormalへfallback。既定値

RAW実装は `sounddevice.InputStream(extra_settings=...)` を使用します。sounddevice 0.5.6の公開 `WasapiSettings` がRAW optionを公開していないため、PortAudio公式の `PaWasapiStreamInfo.streamOption` を設定する小さなprivate CFFI bridgeだけを `audio/windows_raw.py` に隔離しています。

RAWはWASAPI Audio Engineのsignal processing bypassを要求しますが、Intel SST、Realtek、PCメーカーのOEM processing、microphone array firmware、USB microphone内部DSPなど、OS streamより前の処理まで解除できる保証はありません。RAWでも音が強く加工される場合は、外付けUSB microphoneとの比較が有効です。

### Audio-only test

```powershell
python -m g1_bottle_reaction --no-camera --robot mock --speech auto --audio-source windows --audio-mode auto --audio-classifier yamnet --audio-debug
```

音楽をPCの近くで1秒以上再生すると、判定確定後に次を出します。停止はCtrl+Cです。

```text
[MOTION] little_dance
[SPEECH] お、いいね
```

### 音声入力とYAMNetの切り分け

YAMNetへ渡す直前の正規化済み音声を約10秒録音します。スマホから同じ音楽を同じ位置・音量で流し、normalとrawを別々に実行します。`Saved YAMNet input debug WAV` が表示されるまで待ってからCtrl+Cで終了します。

```powershell
python -m g1_bottle_reaction --no-camera --robot mock --speech mute --audio-source windows --audio-mode normal --audio-debug --record-audio-debug
python -m g1_bottle_reaction --no-camera --robot mock --speech mute --audio-source windows --audio-mode raw --audio-debug --record-audio-debug
```

既定の保存先は順に `debug/audio_normal.wav` と `debug/audio_raw.wav` です（autoは `debug/audio_auto.wav`）。これらは16 kHz、mono、PCM16であり、streaming時に正規化・リサンプリングされた、実際のYAMNet入力と同じ連続PCMです。保存先を指定する場合:

```powershell
python -m g1_bottle_reaction --no-camera --robot mock --speech mute --audio-source windows --audio-mode normal --audio-debug --record-audio-debug logs\phone_music.wav
```

保存WAVをマイクなしで同じYAMNetへ再入力します。

```powershell
python -m g1_bottle_reaction --audio-file debug\audio_normal.wav --robot mock --speech mute
python -m g1_bottle_reaction --audio-file debug\audio_raw.wav --robot mock --speech mute
```

各windowについてRMS、Peak amplitude、sample rate、buffer duration、`BEST MUSIC LABEL`、Top 10 YAMNet predictionsを表示します。
live debugには実際に選ばれた `AUDIO MODE` と `DEVICE` も表示します。RAWが使えずfallbackした場合は `NORMAL (RAW unavailable)` と表示されます。

切り分けの目安:

- 保存WAVを実際に聞いて音が小さい、歪む、スマホ音楽が入っていない場合は、device選択、マイクgain、Windowsのノイズ抑制など入力側を確認します。
- WAVが明瞭なのに再入力でもSpeech/Silence中心なら、YAMNet class score、環境音、音源の音量、thresholdを確認します。
- liveとWAV再入力で結果が大きく異なる場合は、streaming timing、chunk欠落、CPU負荷を確認します。

### Bottle + Audio

PowerShell 1行版:

```powershell
python -m g1_bottle_reaction --robot mock --camera 0 --speech auto --audio-source windows --audio-classifier yamnet
```

Audio workerはマイクcallbackおよびVision loopとは別threadです。Vision/Audioイベントは同じReaction queueへ入り、順番にMotionとSpeechを実行します。既存reaction cooldown中に到着したイベントはskipされ、同時実行しません。

### Modelとscore tuning

公式TensorFlow Hub YAMNet `https://tfhub.dev/google/yamnet/1` を使用します。初回だけmodel downloadが必要で、以後は `audio.yamnet.cache_dir`（既定 `.cache/tfhub`）を再利用します。

YAMNetが約1秒windowに返すframe-levelの521 class scoreを、公式例に合わせてclassごとに **mean集約** します。`music_labels` に一致するclassのうち最大値を `music_score` とし、そのclassを `BEST MUSIC LABEL` として表示します。既定候補は `Music`, `Musical instrument`, `Singing`, `Pop music`, `Rock music`, `Hip hop music`, `Electronic music`, `Dance music`, `Background music`, `Classical music` です。このscoreは校正済み確率として扱いません。

環境に応じて `config/default.yaml` の次を調整します。

- `music_start_threshold` / `music_stop_threshold`
- `music_confirm_seconds` / `music_lost_seconds`
- `music_cooldown_seconds`
- `window_seconds` / `inference_interval_seconds`
- `music_labels`（class mapの完全一致名）
- `debug_record_seconds`（診断WAVの長さ。既定10秒）

fake scoreだけで経路を確認する場合は、TensorFlow、model、マイク、network不要です。

```powershell
python -m g1_bottle_reaction --simulate-audio --robot mock --speech console
```

実YAMNetのsilence smoke test:

```powershell
python -m g1_bottle_reaction.audio.smoke_test
```

これはmodelをロードし、16 kHz silenceを推論して521 class outputを確認します。

## Virtual G1

MuJoCo Viewerは、実機なしでreactionのtiming、pose、キャラクター表現、全体の見え方を確認するための軽量な演技ビューアです。既存Reaction Engineの抽象motion名を `MujocoRobotAdapter` がkeyframe animationへ変換します。物理歩行、DDS、Unitree SDK、RL policyは使用しません。

MuJoCoと公式G1モデルをセットアップします。モデルはUnitree公式 `unitreerobotics/unitree_mujoco` のBSD-3-Clause版を外部checkoutするため、このrepositoryには複製しません。

```powershell
python -m pip install -e ".[sim]"
powershell -ExecutionPolicy Bypass -File scripts\setup_g1_model.ps1
```

対応motion一覧:

```powershell
python -m g1_bottle_reaction --list-motions
```

単独preview:

```powershell
python -m g1_bottle_reaction --robot mujoco --preview-motion little_dance
python -m g1_bottle_reaction --robot mujoco --preview-motion notice
python -m g1_bottle_reaction --robot mujoco --preview-motion guard
```

Viewerだけを開く場合:

```powershell
python -m g1_bottle_reaction --robot mujoco --no-camera --audio-source none
```

Bottle / Music reaction:

```powershell
python -m g1_bottle_reaction --robot mujoco --camera 0 --speech auto
python -m g1_bottle_reaction --robot mujoco --no-camera --speech auto --audio-source windows --audio-mode raw --audio-classifier yamnet
python -m g1_bottle_reaction --robot mujoco --camera 0 --speech auto --audio-source windows --audio-mode raw --audio-classifier yamnet
```

animationは60 fpsのsmoothstep補間で `qpos` を設定し、`mj_forward()` とViewer `sync()`だけを実行します。motion workerはVision/Audio loopと分離されています。joint角度とdurationは `config/mujoco_motions.yaml` で変更できます。存在しないjointはwarning付きで無視し、公式MJCFのrangeを超える値はclampします。

このanimationは実機trajectoryではありません。dynamic balance、torque、collision、stability、実機で安全なjoint trajectoryを検証・保証しません。詳しい設計、motion調整、model path変更は [docs/VIRTUAL_G1.md](docs/VIRTUAL_G1.md) を参照してください。

## Expressive Speech with AivisSpeech

AivisSpeechは、既存の固定台詞を変更せず、styleと控えめなvoice profileで自然な日本語音声へ合成します。LLM、会話生成、音声認識は含みません。AivisSpeechまたはAivisSpeech Engineは別アプリとしてインストール・起動し、このプロジェクトは `http://127.0.0.1:10101` のHTTP APIだけを利用します。AivisSpeech側のPython環境はこのPython 3.13環境へ組み込みません。

Engineを起動した後、接続とインストール済みSpeaker / Styleを確認します。

```powershell
python -m g1_bottle_reaction --check-aivis
python -m g1_bottle_reaction --list-aivis-speakers
```

既定speakerは落ち着いた成人男性声の `阿井田 茂` です。style IDは固定せず、起動時に完全一致speaker名とstyle名から解決します。既定styleは `Calm`、存在しない場合は `Mid`、同speakerの最初のstyleの順にfallbackします。

音声だけを比較できます。

```powershell
python -m g1_bottle_reaction --preview-speech "お、いいね" --voice-profile happy --speech-debug
python -m g1_bottle_reaction --preview-speech "お、いいね" --voice-profile serious --speech-debug
python -m g1_bottle_reaction --preview-speech "ん？それ……ペットボトル？" --voice-profile curious --speech-debug
python -m g1_bottle_reaction --preview-voice-profiles "お、いいね"
```

デモ前に固定Reaction台詞をすべて合成して `.cache/tts` へ保存します。同じtext、speaker名/UUID、style ID、voice profile parameterなら2回目以降はHTTP合成せずcache WAVを再生します。

```powershell
python -m g1_bottle_reaction --precache-speech --speech-debug
```

Music / Bottle reaction:

```powershell
python -m g1_bottle_reaction --robot mujoco --no-camera --speech aivis --audio-source windows --audio-mode raw --audio-classifier yamnet
python -m g1_bottle_reaction --robot mujoco --camera 0 --speech aivis --audio-source windows --audio-mode raw --audio-classifier yamnet
```

`config/default.yaml` の `speech.aivis` でEngine URL、speaker名、default style名またはstyle ID、cache、voice profileを調整します。style IDが指定されればそれを優先します。profile用styleが同じspeakerにない場合は、configured default style、ノーマルstyle、最初のstyleの順で安全にfallbackします。

阿井田 茂向けの初期profileは `neutral=Calm`、`curious=Calm`、`happy=Mid`、`surprised=Surprise`、`serious=Heavy`、`shout=Shout` です。感情値は成人男性らしさを保つため概ね0.9–1.15に抑えています。`intonation_scale`と`tempo_dynamics_scale`は0–2、`speed_scale`は0.5–2、`volume_scale`は0–2でvalidationします。音質劣化を避けるため`pitch_scale`は全profileで0.0です。

`--speech aivis`はEngineへ接続できなければ明確なエラーで終了します。`--speech auto`はAivisSpeech、Windows日本語TTS、consoleの順にfallbackします。MotionはReaction workerから直ちに開始され、設定delay後のHTTP合成・再生も同じ既存worker上で行うため、Camera、YOLO、microphone、YAMNet、MuJoCo Viewerをブロックしません。

API・cache・G1 speakerへ出力先を交換する境界は [docs/AIVIS_SPEECH.md](docs/AIVIS_SPEECH.md) を参照してください。

## G1 Hardware Integration Phase 1

G1では入力元だけを公式SDK2 `VideoClient`へ交換し、既存YOLO以降をそのまま使います。

```text
G1 D435i -> videohub_pc4 -> VideoClient JPEG -> G1CameraSource -> BGR -> existing YOLO/game
AivisSpeech -> existing WAV cache -> G1AudioOutput -> G1 speaker
Reaction -> G1RobotAdapter -> verified G1ArmActionClient actions only
```

Windowsの既定は従来どおり`--camera-source opencv --audio-output windows --g1-motion disabled`です。Unitree SDKはlazy importされ、通常のWindows依存関係には入りません。

G1 camera単独診断（Robot、YOLO、Speechは起動しません）:

```bash
python -m g1_bottle_reaction --g1-test camera --network-interface "イーサネット 3"
```

G1 speaker単独診断（motionは送りません）:

```bash
python -m g1_bottle_reaction --g1-test speaker --network-interface eth0 --wav test.wav
```

G1 camera + game logicのみ:

```bash
python -m g1_bottle_reaction --game stealth-phone --robot mock --camera-source g1 --network-interface "イーサネット 3" --speech console
```

G1 camera + AivisSpeechのG1 speaker出力:

```bash
python -m g1_bottle_reaction --game stealth-phone --robot mock --camera-source g1 --speech aivis --audio-output g1 --network-interface "イーサネット 3"
```

実機motionは`--robot g1 --enable-real-robot --g1-motion safe-actions`の3指定を必須とし、起動時に`GetActionList()`で実機のAction IDを確認してから`REAL G1 MOTION ENABLED`を表示します。`notice`は`G1ArmActionClient.ExecuteAction(23)`（right hand up）と2秒後のrelease `ExecuteAction(99)`、`spot_target`は`ExecuteAction(26)`（high wave）へ写像します。timeout既定値は公式exampleと同じ10秒です。それ以外の抽象motionとreal trackingはsafe no-opです。

`ExecuteAction()`のreturn code `3104`はRPC timeoutとしてwarningに記録します。Actionが実際には開始している可能性があるため、失敗とは断定せず自動retryもしません。Actionの開始・終了確認は将来`rt/arm/action/state`購読を追加するTODOです。

SDK setup、connection/camera/speaker/waveの順序、full commandは [docs/G1_INTEGRATION.md](docs/G1_INTEGRATION.md) を参照してください。標準camera pathはG1の`videohub_pc4`を利用し、本projectはこのserviceを停止・killしません。TeleImagerは`--camera-source g1-teleimager`のlegacy optionとしてだけ残していますが、camera device競合の可能性があるためprimary pathには使用しません。

## G1 custom_notice

`custom_notice`は右肩pitch/rollと右肘だけを、実LowStateの現在姿勢から小さいrelative offsetで動かします。公式arm7 DDS exampleの`rt/lowstate`、`rt/arm_sdk`、control weight/releaseフローを使用し、MuJoCo poseを実機へコピーしません。実機defaultは`small`（肩pitch -0.04、肩roll -0.03、肘 +0.05 rad）です。joint margin、per-step delta、timing、profileは`config/custom_g1_motions.yaml`で管理します。

段階試験は必ずdry-run、MuJoCo、実機small motionのみ、cache済み「ん？」付き、最後にStealth統合の順です。実機には既存三重gateと追加`--g1-custom-motion`が必要です。`SUSPICION_STARTED`はdefaultでは従来`notice`のままで、`--enable-custom-notice-reaction`を指定した場合だけ切り替わります。詳しい安全設計とコマンドは [docs/CUSTOM_G1_MOTION.md](docs/CUSTOM_G1_MOTION.md) を参照してください。

## 設定

`config/default.yaml` には以下があります。

- YOLOモデルとconfidence threshold
- 検出／消失の時間デバウンス
- `proximity_ratio` のnear/too-close enter・exit閾値
- 再登場判定時間とreaction cooldown
- Motion、台詞、MotionからSpeechまでのdelay
- JSON Linesイベントログの保存先
- Audio window、推論間隔、music labels、start/stop threshold、confirm/lost/cooldown
- G1 client timeout、arm release delay、speaker volume、audio chunk、camera reconnect

別設定は `--config path\to\settings.yaml` で指定できます。`proximity_ratio` はbounding box面積を画像全体の面積で割った疑似指標であり、実距離ではありません。

## 現在できること

- COCO `bottle` のうち最大bounding boxを追跡
- `NO_BOTTLE / FAR / NEAR / TOO_CLOSE / LOST` の状態管理
- `FOUND / NEAR / TOO_CLOSE / LOST / FOUND_AGAIN` の一度だけのイベント生成
- 時間デバウンス、近接ヒステリシス、reaction cooldown
- 非同期のMock MotionとWindows/console Speech
- bounding box、confidence、状態、イベント、リアクション、台詞、遭遇回数、FPSの表示
- `logs/events.jsonl` へのイベント記録
- Windows microphoneの非同期YAMNet Music判定と共有Reaction queue
- OpenCV/G1 VideoClientを交換できるBGR `CameraSource`
- AivisSpeech cache WAVを16 kHz mono PCM16へ変換するG1 speaker出力
- 明示的な三重gate付きの`G1ArmActionClient`実機mapping

## 現在できないこと・前提

- Webカメラだけでは実距離を測定しません。
- G1 cameraは実機確認済みの`videohub_pc4` + `VideoClient.GetImageSample()`経路を使用します。
- G1の`guard`、`look_around`、`little_dance`、`reach_forward`、`surprise`、`stand`は安全なno-opです。起動時Action Listに必要IDがない場合も対象motionを送りません。
- G1 D435iからはVideoClient JPEGをdecodeしたBGRだけを使い、depthは未使用です。
- G1 built-in microphone取得は未接続です。将来は `WindowsMicSource` だけを `G1MicSource` へ交換します。
- G1 real closed-loop tracking、歩行、navigation、low-level joint controlは未実装です。
- 複数のボトルを人物ごとに追跡せず、画面上で最大の1本だけを対象にします。

G1実機motionはUbuntu側でも `--robot g1` だけでは開始できず、`--enable-real-robot`、`--g1-motion safe-actions`、network interfaceを明示的に要求します。移植作業は [docs/G1_INTEGRATION.md](docs/G1_INTEGRATION.md)、マイクは [docs/G1_AUDIO_INTEGRATION.md](docs/G1_AUDIO_INTEGRATION.md) を参照してください。

## テスト

```powershell
python -m pytest
```

テストはカメラ、マイク、YOLO/YAMNetモデル、TensorFlow、インターネット、Unitree SDKを必要としません。
