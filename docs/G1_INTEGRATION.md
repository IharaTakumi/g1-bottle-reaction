# Windows/Ubuntu / Unitree G1 Phase 1 integration

Phase 1は、公式に公開されている経路だけでhead camera、speaker、`G1ArmActionClient`の検証済みActionを段階的に確認します。G1 microphone、歩行、navigation、low-level joint control、MuJoCo trajectoryの実機転送、実機closed-loop trackingは実装していません。実機確認前に周囲を空け、G1の公式安全手順と緊急停止方法を確認してください。

## 1. CheckoutとPython環境

Windowsで公式`unitree_sdk2_python`と`cyclonedds==0.10.2`を使う場合はPython 3.10の`.venv-g1`を使います。通常開発用Python 3.13環境とは分離し、Unitree公式のCycloneDDS pinは変更しません。標準G1 cameraはSDKに含まれる`VideoClient`を使うため、TeleImagerは不要です。

```powershell
py -3.10 -m venv .venv-g1
.\.venv-g1\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip install -e C:\dev\unitree_sdk2_python
python -m pytest
```

WindowsではNIC aliasをIPv4へ解決し、SDK呼び出し中だけaddress方式のCycloneDDS XMLを使用します。`unitree_sdk2_python` checkoutは編集しません。

```powershell
python -m g1_bottle_reaction --g1-test connection --network-interface "イーサネット 3"
```

alias解決を避ける場合はIPv4を直接指定します。

```powershell
python -m g1_bottle_reaction --g1-test connection --network-address 192.168.123.222
```

成功時には次のように実際の選択内容を表示します。

```text
[G1 DDS] platform=Windows
[G1 DDS] interface=イーサネット 3
[G1 DDS] address=192.168.123.222
[G1 DDS] mode=address
```

`--network-address`使用時のinterface表示は`-`です。LinuxではこのWindows configを使用せず、公式`ChannelFactoryInitialize(0, "eth0")`を従来どおり呼びます。

Ubuntu hostで次を実行します。Python versionは導入するUnitree SDK releaseの要件にも合わせてください。

```bash
git clone <this-repository-url> g1-bottle-reaction
cd g1-bottle-reaction
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[audio,dev]'
python -m pytest
python -m g1_bottle_reaction --simulate --robot mock --speech console
```

`pyproject.toml`は通常環境へUnitree依存を入れないため、Unitree SDKはG1専用環境へ公式repositoryから別途導入します。

## 2. 公式Unitree SDK2 Python

[Unitree公式 unitree_sdk2_python](https://github.com/unitreerobotics/unitree_sdk2_python)をcloneし、そのREADMEどおり同じvenvへ導入します。使用するrevisionとG1 firmwareの対応を現場で固定してください。

```bash
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git ../unitree_sdk2_python
python -m pip install -e ../unitree_sdk2_python
```

G1へつないだNIC名を推測せず、Ubuntuで確認します。

```bash
ip addr
```

以後の`eth0`は、実際にG1へ接続されているinterface名へ置き換えます。接続診断はDDS channelと公式`AudioClient.Init()`だけを行い、音声やmotionは送信しません。

```bash
python -m g1_bottle_reaction --g1-test connection --network-interface eth0
```

成功時は`G1 DDS and AudioClient initialization: OK`を表示します。

## 3. 標準videohub_pc4 camera

Primary camera pathはG1標準serviceをそのまま使います。

```text
G1 D435i
  -> videohub_pc4
  -> unitree_sdk2py.go2.video.video_client.VideoClient
  -> GetImageSample() JPEG bytes
  -> numpy.frombuffer()
  -> cv2.imdecode(..., IMREAD_COLOR)
  -> BGR CameraSource contract
```

本projectは`videohub_pc4`を停止・kill・再設定しません。TeleImagerは`--camera-source g1-teleimager`のlegacy optionとして残していますが、このG1ではcamera device競合が確認されているためprimary pathには使用しません。`--g1-image-server-ip`もlegacy optionにだけ適用されます。

cameraだけを確認します。YOLO、Robot、Speechは起動しません。

```powershell
python -m g1_bottle_reaction --g1-test camera --network-interface "イーサネット 3"
```

OpenCV windowに映像とFPSが表示されれば成功です。`q`またはEscで終了します。

## 4. G1 speaker

入力WAVは`G1AudioOutput`内部で16 kHz、mono、signed 16-bit little-endian PCMへ変換されます。公式Python SDKの`AudioClient.SetTimeout()`、`Init()`、`SetVolume()`、`PlayStream()`、`PlayStop()`だけを使用します。既定volume、chunk size、送信間隔は`config/default.yaml`の`g1`で変更できます。

```bash
python -m g1_bottle_reaction --g1-test speaker --network-interface eth0 --wav test.wav
```

音声がG1 speakerから最後まで聞こえ、`G1 speaker test: complete`が表示されれば成功です。この診断は`G1ArmActionClient`を生成せず、motionを送りません。

AivisSpeech利用時は既存のHTTP synthesisとcacheを保ち、出力先だけをG1へ交換します。

```bash
python -m g1_bottle_reaction --game stealth-phone --robot mock --camera-source g1 --speech aivis --audio-output g1 --network-interface eth0
```

## 5. G1ArmActionClient safety gate

実機motionは次の3指定が同時に必要です。

- `--robot g1`
- `--enable-real-robot`
- `--g1-motion safe-actions`

一つでも欠ける場合はmotionを送りません。起動時に`REAL G1 MOTION ENABLED` bannerを必ず表示します。周囲を空けてから単独診断します。

```bash
python -m g1_bottle_reaction --g1-test wave --robot g1 --enable-real-robot --g1-motion safe-actions --network-interface eth0
```

起動時に公式`G1ArmActionClient.GetActionList()`を呼び、利用可能なAction IDを表示します。必要IDがlistになければ対象motionはsafe no-opです。現在のReaction mappingは次のとおりです。

| Abstract motion | Real G1 Phase 1 |
|---|---|
| `notice` | right hand up: `ExecuteAction(23)`、2秒後にrelease: `ExecuteAction(99)` |
| `spot_target` | high wave: `ExecuteAction(26)` |
| `guard` | warning + safe no-op |
| `look_around` | warning + safe no-op |
| `little_dance` | warning + safe no-op |
| `reach_forward` | warning + safe no-op |
| `surprise` | warning + safe no-op |
| `stand` | warning + safe no-op |

`apply_tracking()`と`set_attention_yaw()`もsafe no-opです。PLAYER位置をwaist、歩行、旋回へ変換しません。

client timeoutは公式exampleと同じ10秒です。`ExecuteAction()`が`3104`を返した場合は、RPC responseがtimeoutしていても実motionが始まっている可能性があるためwarningだけを記録します。二重動作を避けるため自動retryしません。将来は`rt/arm/action/state`を購読してActionの開始・終了を確認しますが、現時点では未実装です。

## 6. 段階的なゲーム確認

まずG1 cameraとゲームロジックだけをMock/consoleで確認します。

```powershell
python -m g1_bottle_reaction --game stealth-phone --robot mock --camera-source g1 --network-interface "イーサネット 3" --speech console
```

次にmotionをOFFのままG1 speakerを確認します。

```bash
python -m g1_bottle_reaction --game stealth-phone --robot mock --camera-source g1 --speech aivis --audio-output g1 --network-interface eth0
```

最後に、単独arm Action診断を完了した環境でだけPhase 1 full flowを明示的に有効化します。

```bash
python -m g1_bottle_reaction --game stealth-phone --robot g1 --enable-real-robot --g1-motion safe-actions --camera-source g1 --speech aivis --audio-output g1 --network-interface eth0
```

このfull flowでも`notice` / `spot_target`以外はno-opで、real trackingは行いません。

追加opt-inの相対上半身motion `custom_notice`は、preset Actionの単独確認後に別の段階試験として扱います。最初からStealth Gameへ有効化せず、[CUSTOM_G1_MOTION.md](CUSTOM_G1_MOTION.md)のLEVEL 0〜5を順に実施してください。

## 未実装とトラブルシュート

- `G1MicSource`はplaceholderです。Windows/YAMNet audio pathには影響しません。
- RealSense depthは使わず、既存YOLOへBGR frameだけを渡します。
- VideoClientの一時的なreturn code/decode failureは短時間retryし、設定回数を超えた場合だけ明確なerrorで停止します。
- Unitree SDKがないWindowsでも`import g1_bottle_reaction`、simulation、pytestは動きます。
- DDS interfaceを途中で別名へ変更する場合はprocessを再起動してください。
- 未確認のstate API、low-level command、joint trajectoryを診断目的で追加しないでください。
