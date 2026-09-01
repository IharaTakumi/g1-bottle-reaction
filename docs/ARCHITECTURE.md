# Architecture

```text
OpenCV camera --\
                  -> CameraSource -> uint8 BGR --+-> Bottle YOLO -> BottleTracker --\
G1 VideoClient --/                              `-> Target YOLO -> StealthGame -----+-> events
                                                                                   |
Mic -> AudioSource -> bounded queue -> YAMNet -> MusicStateTracker ----------------/
                                                                                   |
                                                                                   v
                                                                        ReactionEngine queue
                                                                           |-> RobotAdapter
                                                                           |     |-> Mock
                                                                           |     |-> MuJoCo worker
                                                                           |     `-> G1 safe actions
                                                                           `-> SpeechBackend
                                                                                 |-> Console/Windows TTS
                                                                                 `-> Aivis -> WAV cache
                                                                                              `-> AudioOutput
                                                                                                  |-> Windows
                                                                                                  `-> G1 speaker
```

Stealth modeでは、YOLOのraw `cell phone` detectionを`TargetPerception`が`TargetObservation(role=PLAYER)`へ変換します。`StealthGameEngine`はraw class名、YOLO、カメラをimportせず、semantic role、visibility、confidence、正規化位置、bbox面積、monotonic timestampだけから警戒値とstate transitionを計算します。`detector_label`を`person`へ変えてもGame Engineは変更しません。

離散的な`SUSPICION_STARTED`、`ALERT_STARTED`、`PLAYER_FOUND`等だけが共有Reaction Engineへ入ります。毎フレームのPLAYER方向は別の`TargetTrackingController`がFPS非依存の一次応答で`TrackingCommand`へ変換し、`RobotAdapter.apply_tracking()`へ送ります。Commandはdesired/actual yaw、strength、game state、ACTIVE/HOLD/RECENTER/LOCKED status、last-seen時間を持ちます。Mockは記録のみ、MuJoCoは実在する`waist_yaw_joint`のpreview offset、G1 adapterは未確認APIを使わないsafe no-opです。

`CameraSource.read()` は入力元に関係なくOpenCV互換のHxWx3 `uint8` BGR frameだけを返します。`OpenCVCameraSource`は従来の`VideoCapture`を、`G1CameraSource`は既存Windows DDS compatibility layer、公式`VideoClient.GetImageSample()`、JPEG decodeを隔離します。標準`videohub_pc4`を利用し、serviceを停止・killしません。`TeleImagerCameraSource`は明示的なlegacy optionだけです。YOLO、Target Perception、Bottle Trackerは入力元を知りません。

`vision` は1フレームから最大のbottle検出と `proximity_ratio` を返します。`state` は時刻付きの検出情報だけを受け取るためYOLO非依存です。`reactions` はイベントをYAML定義のMotion/Speechへ変換し、worker threadで順次実行します。

`audio` は `AudioSource` callbackからbounded queueへPCM chunkをコピーし、別workerでmono化、16 kHz変換、約1秒window、YAMNet推論、`MusicStateTracker`を処理します。VisionとAudioは互いをimportせず、イベントだけがAppで合流します。

`--record-audio-debug` はworkerが正規化した連続PCMをwindowing直前で分岐し、16 kHz mono WAVへ保存します。`--audio-file` はそのWAVを同じnormalizer、YAMNet、MusicStateTrackerへoffline windowとして再入力するため、入力品質と分類結果を分けて調査できます。

Windows inputは同じ `AudioSource` contractの `WindowsMicSource`（normal）と `WindowsRawMicSource`（WASAPI RAW）を持ちます。RAW sourceだけがPortAudioの `PaWasapiStreamInfo.streamOption=eStreamOptionRaw` を要求し、callback以降のqueue、normalizer、resampler、recorder、YAMNet、state trackerは共通です。`raw` / `auto` でRAW endpointを開けない場合は、同じprocess内でnormal sourceへfallbackします。Windows全体の音声設定は変更しません。

sounddeviceの公開 `WasapiSettings` にRAW optionがないため、private CFFI ABIへの依存は `audio/windows_raw.py` のbridge一か所だけです。PortAudio/sounddevice更新でABIが使えなくなった場合も `RawCaptureUnavailable` として扱い、normal captureを継続します。

VisionとAudioが同時に反応を生成しても、既存の単一Reaction queueがMotionとSpeechを順番に実行します。通常eventはglobal cooldownを維持し、ゲームのstate transitionは設定されたpriorityと明示的cooldown bypassで重要な`PLAYER_FOUND`を落としません。stealth stateがUNAWARE以外の間はMusic reactionを抑制します。

`MujocoRobotAdapter` はReaction Engineから `notice` や `little_dance` という抽象motion名だけを受け取ります。専用workerがYAML keyframeをsmoothstep補間し、公式G1 MJCFのscalar joint `qpos`へclamp済み角度を設定して `mj_forward()` / Viewer `sync()`を行います。tracking対象jointはロード済みmodelの`waist_yaw_joint`で、最終値を`motion pose yaw + tracking actual yaw`として合成後、実model rangeへ再clampします。jointが存在しないmodelではwarning付きsafe no-opです。`mj_step()`、torque、balance controllerは使用しません。Viewer/animation threadはVision、Audio、Reaction workerをブロックしません。

MuJoCoと公式modelはoptionalです。adapter moduleはMuJoCoをtop-level importせず、`--robot mujoco`の初期化時だけロードします。Unitree SDKおよび `G1RobotAdapter`とは依存関係がありません。

Reactionは固定textにoptionalな `voice_profile` 名だけを持ちます。Reaction Engineはmotionを即時queueした後、既存のspeech delayを待って `SpeechBackend.speak(text, voice_profile=...)` を呼びます。Aivis固有のHTTP、style ID、AudioQuery、cache、WAV再生はadapter内部にあり、Reaction Engine、Vision、Audio perception、MuJoCoからは見えません。

`AivisSpeechBackend` は `/speakers` で実modelにあるstyleを解決し、`/audio_query`へprofile parameterを適用して `/synthesis`からWAVを得ます。`SpeechCache`は合成条件のSHA-256でWAVを保存します。`WindowsWaveOutput`はPC speakerへ、`G1AudioOutput`はWAVを16 kHz mono signed PCM16 little-endianへ変換して公式`AudioClient.PlayStream()`へ送ります。合成と出力先は分離したままです。

Windowsでは `MockRobotAdapter`、optionalな `MujocoRobotAdapter`、Windows/console speechを使用します。直接のUnitree importは`adapters/g1_robot.py`のlazy `UnitreeSdkRuntime`だけにあり、G1 cameraはsourceを開く時に同runtimeからVideoClientを生成します。legacy TeleImager importも明示的にsourceを開く時まで遅延されます。コア、simulation、テストのimport graphにはどちらも入りません。

実機motionは、三重の安全gate通過後にだけlazy-loaded `G1ArmActionClient`を初期化し、`GetActionList()`で実機のAction IDを確認します。`notice`はright hand up ID 23（設定時間後にrelease ID 99）、`spot_target`はhigh wave ID 26へ写像します。`3104`はRPC timeout warningとして扱い、自動retryしません。`guard`、`look_around`、`little_dance`、`reach_forward`、`surprise`、`stand`と連続trackingはwarning付きsafe no-opです。MuJoCo qpos、joint trajectory、tracking yawを実機へ送る経路はありません。Action開始・終了の確定は将来`rt/arm/action/state`購読で行うTODOです。

追加opt-inされた`custom_notice`だけは、独立した`CustomArmMotionController`が公式arm7 DDS flowを使います。pure-Python trajectory coreはLowState base pose、YAML relative offsets、smoothstep、range margin、per-step limitを処理し、Unitree importを持ちません。DDS transportは`g1_robot.py`内に隔離されます。preset Actionとcustom controllerはownership lockで排他されます。Reaction Engineはcustom motionだけ共通monotonic timelineでnon-blocking dispatchし、cache済みspeechを設定時刻へscheduleします。

イベントのみをJSON Linesで記録します。`source` は `vision`、`audio`、`stealth_game` です。カメラ画像、PCM、毎フレームの状態は保存しません。
