# G1 custom_notice safety and test guide

`custom_notice`は、スマホを一瞬見つけたG1が右腕を小さく反射させる約1.15秒の上半身motionです。挙手、wave、guardには見えない振幅から始めます。脚、waist、歩行、tracking、IK、全身low-level control、hand graspは使用しません。

## Verified Unitree control path

実装の基準は、インストール済み公式SDKの`example/g1/high_level/g1_arm7_sdk_dds_example.py`です。そこから次を確認しています。

- LowState: `rt/lowstate` / `LowState_`
- arm command: `rt/arm_sdk` / `LowCmd_`
- SDK control weight: `motor_cmd[29].q`
- update period: 0.02秒
- command fields: `q`, `dq=0`, `kp`, `kd`, `tau=0`、CRC
- release: weightを1から0へ漸減

使用jointは右肩pitch（SDK index 22）、右肩roll（23）、右肘（25）だけです。rangeは公式`unitree_mujoco` 29-DoF modelの順に`[-3.0892, 2.6704]`、`[-2.2515, 1.5882]`、`[-1.0472, 2.0944]`です。コードは名前、index、rangeが検証済み値と一致しなければ設定を拒否します。

## Relative motion and safety

実機ではmotion開始直前のLowStateを`base_pose`として読み、常に`base_pose + scaled relative offset`を生成します。絶対姿勢やMuJoCo qposは実機へ送りません。設定は`config/custom_g1_motions.yaml`です。

- smoothstep interpolation
- 20ms update
- joint rangeから0.10 rad内側へclamp
- 1 update最大0.02 rad
- t=0.20で小さいnotice pose、t=0.55からreturn、t=1.15でbase pose
- return後、0.10秒かけてarm SDK weightを0へrelease
- DDS例外時はtrajectory送信を止め、可能な範囲でrelease

振幅profileは`small=0.25`、`medium=0.50`、`demo=1.00`です。実機defaultは必ず`small`です。smallの最大relative offsetは肩pitch -0.04 rad、肩roll -0.03 rad、肘 +0.05 radです。

LowState、joint、range、control publisherのいずれかが利用できない場合、またはcontrollerがbusyの場合は開始しません。preset `G1ArmActionClient`とcustom controllerは同じprocess内のownership lockを共有します。preset Actionの終了を`rt/arm/action/state`で確認する機能は未実装なので、終了が確認できないpresetを実行した後はcustom motionを保守的に拒否します。強制takeoverはしません。

## Motion and speech timeline

custom motionと「ん？」はReaction Engineの同じmonotonic start timeを使います。motionをnon-blocking dispatchし、既定0.25秒後にcache済みWAVを再生します。`--reaction-timing-debug`ではreaction開始、first command、notice pose、speech request、実playback開始、return、motion endを表示します。

AivisSpeechの`curious` profileは`prePhonemeLength=0.08`を使用し、過剰な先頭無音を避けます。既存`--precache-speech`はSUSPICION_STARTEDの実text「ん？」をcacheします。G1 speaker出力時は既存`G1AudioOutput`が16 kHz mono PCM16へ変換します。

## Staged manual test

LEVEL 0はsynthetic zero baseでtrajectory計算だけを行い、DDSやG1 commandを使用しません。LEVEL 1のMuJoCo motionは演技確認専用です。

LEVEL 2以降は、公式安全手順、十分な周囲空間、緊急停止手段を確認してください。実機custom motionには既存三重gateに加えて`--g1-custom-motion`が必要です。banner、motion名、amplitudeを確認してから進めます。最初は必ずsmall・motionのみです。

`SUSPICION_STARTED`のdefault mappingは従来の安全な`notice`のままです。単独試験後だけ`--enable-custom-notice-reaction`で`custom_notice + 「ん？」`へ切り替えます。ALERT、FOUNDとその他のReactionは変更しません。

### LEVEL 0: dry-run

```powershell
python -m g1_bottle_reaction `
  --g1-test custom-notice `
  --robot mock `
  --custom-motion-amplitude small `
  --custom-motion-dry-run
```

### LEVEL 1: MuJoCo preview

```powershell
python -m g1_bottle_reaction `
  --robot mujoco `
  --preview-motion custom_notice
```

### LEVEL 2: real G1 small、motionのみ

```powershell
python -m g1_bottle_reaction `
  --g1-test custom-notice `
  --robot g1 `
  --enable-real-robot `
  --g1-motion safe-actions `
  --g1-custom-motion `
  --custom-motion-amplitude small `
  --network-interface "イーサネット 3"
```

### LEVEL 3: medium（small承認後、必要な場合だけ）

LEVEL 2の`--custom-motion-amplitude small`を`medium`へ変更します。`demo`は演技preview用であり、初回実機試験には使用しません。

### LEVEL 4: cache済み「ん？」との同期

最初にAivisSpeech cacheを生成します。

```powershell
python -m g1_bottle_reaction --precache-speech
```

次にcustom motion diagnosticへspeechを追加します。

```powershell
python -m g1_bottle_reaction `
  --g1-test custom-notice `
  --robot g1 `
  --enable-real-robot `
  --g1-motion safe-actions `
  --g1-custom-motion `
  --custom-motion-amplitude small `
  --custom-motion-with-speech `
  --speech aivis `
  --audio-output g1 `
  --reaction-timing-debug `
  --network-interface "イーサネット 3"
```

### LEVEL 5: Stealth Game opt-in integration

```powershell
python -m g1_bottle_reaction `
  --game stealth-phone `
  --robot g1 `
  --enable-real-robot `
  --g1-motion safe-actions `
  --g1-custom-motion `
  --enable-custom-notice-reaction `
  --custom-motion-amplitude small `
  --camera-source g1 `
  --speech aivis `
  --audio-output g1 `
  --reaction-timing-debug `
  --network-interface "イーサネット 3"
```

音声時刻の一時調整には`--notice-speech-delay 0.25`を追加できます。確定値は`config/custom_g1_motions.yaml`の`speech_delay_seconds`へ保存します。

## Emergency handling and limitations

異常時は新規trajectory commandを停止し、best-effortでbase poseのままweightを0へ下げます。通信断ではrelease packet到達を保証できないため、必ずG1公式の停止手段を用意してください。`rt/arm/action/state`によるclosed-loop開始・終了確認、別processが持つarm ownership検出、実機でのoffset/range妥当性、音声と機械動作の最終同期は実機確認が必要です。
