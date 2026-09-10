# G1 Game Vision

実装状態: ハードウェア非依存コードとmock transportは完成しています。D435iをG1 PC2から同時利用できるか、PC2→UbuntuのWi-Fi到達性、実効FPS・遅延は **NOT VERIFIED ON REAL G1** です。

## 1. 目的と安全境界

`g1_bottle_reaction.game_vision` は、G1視点で「近距離だけが見える」かくれんぼのゲーム性を検証する独立viewerです。既存Reaction Engine、YOLO、ロボット制御、R3コントローラーには接続しません。G1は従来どおりR3で手動操作します。

```text
FrameSource
  -> aligned BGR + depth[m]
  -> DepthFilter
  -> FovFilter
  -> future detector hook
  -> HUD / transport
  -> game / safety / both display
```

距離制限映像は衝突回避用ではありません。実機試験では次を必須運用とします。

- ゲーム映像を見ない独立した安全監視者を置く
- G1の歩行安全機構を変更・無効化しない
- R3の停止操作と試験範囲を事前確認する
- 通信断、映像停止、操作者の違和感があれば直ちに歩行を止める

YOLO、人物判定、自律歩行、SLAM、Navigation、音声、独自Web UIは今回の範囲外です。

## 2. Phase 0調査結果

2026-09-08に読み取り調査できた開発hostはUbuntuではなくWindows 11 Home build 26200です。WSLはありません。

| 項目 | 確認結果 |
|---|---|
| Python | system/default 3.13.15。3.10.11、3.11も存在 |
| project venv | `.venv`、Python 3.13.15、editable install、systemと分離 |
| OpenCV / NumPy / PyYAML / pytest | 4.14.0.94 / 2.5.2 / 6.0.3 / 9.1.1 |
| GPU | Intel UHD 730。PyTorch 2.14 CPU build、CUDA false。`nvidia-smi`利用不可 |
| `pyrealsense2` | 未導入 |
| `unitree_sdk2py` | 未導入 |
| `teleimager` / `xr_teleoperate` | 未導入 |
| RealSense | 接続なし |
| G1 network | Ethernet `192.168.150.106/24`。Wi-Fi切断、`192.168.123.0/24` routeなし |

既存記録 `docs/G1_LOCAL_ENV.md` には、過去のUbuntu 24.04.4、Python 3.12、Unitree SDK 1.0.1、CycloneDDS 0.10.2、`enp129s0=192.168.123.99/24` が記録されています。これは現在の対象Ubuntuを再確認した値ではありません。

既存 `vision.CameraSource` はBGR-only契約で、標準G1経路は `videohub_pc4 -> VideoClient.GetImageSample() -> JPEG/BGR` です。既存契約とconfig loaderを変更せず、今回のRGBD機能を専用packageと専用YAMLへ分離しました。system Python、既存venv、CUDA、RealSense、Unitree SDKへのinstall/updateは実行していません。

repositoryは `src/g1_bottle_reaction` のsrc layoutで、既存の `vision/`、`adapters/`、`audio/`、Reaction/game modulesと、root `config/`、`tests/` に分かれています。今回のruntime codeは `src/g1_bottle_reaction/game_vision/` 内だけに置き、既存applicationのentry point、camera interface、YAML schema、依存宣言を変更していません。

### 対象Ubuntu / G1 PC2で最初に行う読み取り監査

以下は設定を変更しません。結果を保存してから、使用する既存Python環境とcamera取得経路を決めます。

```bash
uname -a
cat /etc/os-release
python3 --version
command -v python3
env | grep -E '^(VIRTUAL_ENV|CONDA_PREFIX)=' || true

lspci | grep -Ei 'vga|3d|nvidia' || true
nvidia-smi || true

command -v rs-enumerate-devices realsense-viewer || true
rs-enumerate-devices -s 2>/dev/null || true
dpkg-query -W 'librealsense*' 2>/dev/null || true
lsusb
ls -l /dev/video* 2>/dev/null || true

pgrep -a videohub_pc4 || true
pgrep -af 'teleimager|image_server|xr_teleoperate' || true
fuser -v /dev/video* 2>/dev/null || true

ip -br address
ip route
ss -ltnp | grep -E ':(55558|55559)\b' || true

for repo in "$HOME/xr_teleoperate" "$HOME/teleimager"; do
  if [ -d "$repo/.git" ]; then
    printf '%s: ' "$repo"
    git -C "$repo" rev-parse HEAD
  fi
done
```

使用予定のPython executableごとにimport元とversionを確認します。

```bash
python3 - <<'PY'
import importlib.metadata
import importlib.util
import sys

print('python:', sys.version)
print('executable:', sys.executable)
for name in ('cv2', 'numpy', 'yaml', 'pyrealsense2', 'unitree_sdk2py', 'teleimager'):
    spec = importlib.util.find_spec(name)
    print(f'{name}:', None if spec is None else spec.origin)
for dist in ('opencv-python', 'numpy', 'PyYAML', 'pyrealsense2', 'unitree-sdk2py', 'teleimager'):
    try:
        print(f'{dist} version:', importlib.metadata.version(dist))
    except importlib.metadata.PackageNotFoundError:
        pass
PY
```

PC2 senderとUbuntu receiverの両方で、既存OpenCVがWebP alphaを保持できるか読み取りprobeを行います。packageは変更しません。

```bash
/exact/approved/python - <<'PY'
import cv2
import numpy as np

sample = np.zeros((2, 2, 4), dtype=np.uint8)
sample[:, :, :3] = 120
sample[:, :, 3] = 255
sample[0, 0] = 0
ok, encoded = cv2.imencode('.webp', sample, [cv2.IMWRITE_WEBP_QUALITY, 80])
decoded = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED) if ok else None
assert decoded is not None and decoded.shape == sample.shape
assert decoded[0, 0, 3] == 0 and decoded[1, 1, 3] == 255
print('WebP alpha: OK')
PY
```

この監査前に `apt install`、system-wide pip、CUDA/driver更新、RealSense firmware/SDK更新、TeleImagerやxr_teleoperate環境の上書きは行いません。特に `videohub_pc4` は停止・killしません。

## 3. 採用アーキテクチャ

ローカル入力と無線入力で同じ表示pipelineを使います。

```text
直接/記録入力
  RealSense -> rs.align(color) -> Depth fog -> FOV -> [future detector]
                                        |-> local game/safety display
                                        `-> sender HUD -> TeleImager ZMQ

無線B構成
  G1 PC2: D435iを一度だけcapture/align/filter
           -> game: WebP RGB + binary alpha mask, TCP 55558
           -> safety: optional JPEG, TCP 55559
  Ubuntu: TeleImager latest-frame subscriber
           -> new payloadだけ同期decode
           -> alphaでhidden pixelを再度black
           -> fullscreen display
```

無線は構成B「PC2でRGB+Depthを処理し、表示用画像を送る」を採用しました。新しいsocket protocolを作らず、Unitree TeleImagerの公開 `ZMQ_PublisherManager` / `ZMQ_SubscriberManager` とlatest-frame設定を利用します。current TeleImagerの `teleimager.client` とxr_teleoperate pin版の `teleimager.image_client` はlazy importで両方に対応します。既存環境へどちらかをinstallし直す処理はありません。

game RGBはWebPで非可逆圧縮しますが、binary alpha maskは保持されます。受信後、alphaが255でないpixelを必ず0へ戻すため、RGB codecのringingで遠方の元画像が再出現しません。送信前に既に捨てた遠方RGBはtransportへ入りません。画面下のRange/FOV HUDは距離filter後に送信側で付けるため、hidden領域上でも意図的に表示されます。

TeleImagerから新しいencoded payload objectが届かない限りframe番号を進めません。初回接続はdefault 3秒、接続後のstale timeoutは500msです。通信断時に最後のBGRをlive映像として再利用せず、errorでwindowを閉じます。

現在のread/display loopは同期式のため、通信断を判定するまで最後の画面が最大およそ500ms残る可能性があります。これは安全映像ではなく、違和感があればtimeoutを待たずR3で歩行を止めます。非同期blank表示は今回のMVP外です。

## 4. 導入方針

現在のWindows `.venv` では追加installなしでsynthetic、recording、unit testを実行できます。対象UbuntuではPhase 0監査のあと、まず承認済み既存Pythonでzero-install起動を試します。

```bash
cd /path/to/g1-bottle-reaction
PYTHONPATH=src /exact/path/to/python -m g1_bottle_reaction.game_vision \
  --source synthetic --windowed
```

分離環境が必要と合意できた場合だけ、repository外に専用venvを作ります。次はsystem Pythonや既存venvを変更しませんが、実行前にpackage取得可否を確認してください。

```bash
python3 -m venv "$HOME/.venvs/g1-game-vision"
GV_PY="$HOME/.venvs/g1-game-vision/bin/python"
"$GV_PY" -m pip install \
  'numpy>=1.26.4,<2' \
  'opencv-python>=4.10.0.84,<5' \
  'PyYAML>=6.0.2,<7' \
  'pytest>=8.3.3,<10'
PYTHONPATH=src "$GV_PY" -m g1_bottle_reaction.game_vision \
  --source synthetic --windowed
```

NumPy `<2` はTeleImagerとの共存を想定した値です。xr_teleoperateがpinする旧TeleImagerはPython `<3.12`、current TeleImagerはPython `<3.13` のため、実機では既存xr_teleoperate環境のPython/TeleImager組合せを優先します。`pyrealsense2` とTeleImagerはhost固有なので上の一括installへ含めません。必要になった時点で既存version・Python ABI・native libraryを確認し、導入理由と対象venvを説明してから行います。apt、sudo、SDK更新はこの実装作業では行っていません。

## 5. G1なしで試す

### 疑似RGBD

```bash
python -m g1_bottle_reaction.game_vision \
  --source synthetic \
  --vision-preset normal \
  --windowed
```

疑似Depthは左0.5mから右4.0mへ変化し、中央にinvalid stripeがあります。GUIなしのsmoke test:

```bash
python -m g1_bottle_reaction.game_vision \
  --source synthetic --headless --max-frames 30
```

### Webcam / RGB動画

```bash
python -m g1_bottle_reaction.game_vision --source webcam --camera 0 --windowed

python -m g1_bottle_reaction.game_vision \
  --source video --video test.mp4 --loop --windowed
```

RGB-only入力では距離fogを再現せず、`NO DEPTH - DISTANCE FOG OFF` と表示します。UI、FPS、FOV、fullscreen、キーを確認する用途です。

### 記録済みRGBD

```bash
python -m g1_bottle_reaction.game_vision \
  --source recording --recording test_rgbd.npz --loop
```

`.npz` は `bgr`（`N,H,W,3`、uint8）、任意の `depth_m`（`N,H,W`、metres）、任意のscalar `fps` を持ちます。`depth_m` は記録作成側で既にColorへalign済みであることが契約です。shape一致だけでは光学的alignmentを証明できません。pickleは無効です。

RealSense `.bag` も再生できます。

```bash
python -m g1_bottle_reaction.game_vision \
  --source realsense --realsense-file recording.bag --loop
```

## 6. RealSense RGB + Depth

`--source realsense` を選んだ時だけ `pyrealsense2` をimportします。live D435iではColor BGR8とDepth Z16をdefault 640x480、30 FPSで要求します。

```bash
python -m g1_bottle_reaction.game_vision \
  --source realsense \
  --realsense-serial YOUR_D435I_SERIAL \
  --vision-preset normal \
  --fullscreen
```

処理順は次のとおりです。

```text
pipeline.wait_for_frames()
  -> rs.align(rs.stream.color).process(frameset)
  -> aligned color/depth取得
  -> Z16 * depth units -> float32 metres
  -> color/depth shape一致を検証
```

同じresolutionだから対応すると仮定しません。align後にColor/Depthの片方がない、またはshapeが違う場合はresizeやRGB fallbackをせず停止します。`.bag` のRGB8はBGRへ変換し、未知のColor formatは拒否します。

このcommandはD435iがUSB接続され、そのdeviceを正当に所有できるhostでだけ実行します。G1 PC2での直接取得は `videohub_pc4` と競合する可能性があるため、Phase 0監査とUnitree承認なしには実行しません。

## 7. 距離fogとFOV

default `fade` のvisibilityです。

```text
d <= clear_m                 : 1.0
clear_m < d < max_m          : (max_m - d) / (max_m - clear_m)
d >= max_m                   : 0.0
d <= 0 / NaN / infinity      : 0.0
```

normalでは1.0m→100%、1.8m→70%、3.0m→0%、invalid→0%です。`hard` はvalidかつ `d < max_m` だけ表示します。

```bash
python -m g1_bottle_reaction.game_vision --source synthetic --fog-mode fade
python -m g1_bottle_reaction.game_vision --source synthetic --fog-mode hard
```

default FOVは `scale=0.55`、`feather=0.25` の楕円vignetteです。中心を通る横・縦方向の中央55%が完全表示、その外側が滑らかに黒へ変わります。物理D435iのFOVやintrinsicsは変更しません。

```bash
python -m g1_bottle_reaction.game_vision \
  --source synthetic --fov-scale 0.55 --fov-feather 0.25
```

## 8. Preset、画面、キー、設定

| Preset / key | clear | fade end / max |
|---|---:|---:|
| `close` / `1` | 1.0m | 2.0m |
| `normal` / `2` | 1.5m | 2.5m |
| `long` / `3` | 2.0m | 3.5m |

| Key | 動作 |
|---|---|
| `Q` / `Esc` | 終了 |
| `F` | fullscreen / windowed切替 |
| `1` / `2` / `3` | close / normal / long |
| `[` / `]` | max distanceを0.25m減少 / 増加 |

`[` で下げても `max_m` は `clear_m + 0.1m` 未満になりません。無線B構成ではfilterはPC2で完了済みのため、受信Ubuntuの距離キーは無効です。preset変更はPC2 senderで行うか、senderを指定値で再起動します。受信側で `--vision-preset`、`--fog-mode`、`--fov-*` を指定すると、黙って無視せず起動を拒否します。

表示mode:

```bash
python -m g1_bottle_reaction.game_vision --source realsense --display game
python -m g1_bottle_reaction.game_vision --source realsense --display safety
python -m g1_bottle_reaction.game_vision --source realsense --display both
```

- `game`: fog + FOV。ゲーム操作者用
- `safety`: filter前RGBに赤い警告label
- `both`: game大画面 + raw safety inset。commissioning用

`both` はゲーム操作者へ遠方を見せるため、本番ゲームでは別の安全監視者にだけ見せます。

専用defaultは `src/g1_bottle_reaction/game_vision/default.yaml` です。既存 `config/default.yaml` は変更していません。部分override例:

```yaml
game_vision:
  vision_preset: normal
  fog_mode: fade
  fov:
    enabled: true
    scale: 0.55
    feather: 0.25
  display:
    fullscreen: true
    show_fps: true
    mode: game
  transport:
    g1_host: 192.168.123.164
    game_port: 55558
    safety_port: 55559
    bind_host: 127.0.0.1
    webp_quality: 80
    connect_timeout_ms: 3000
    stale_timeout_ms: 500
```

```bash
python -m g1_bottle_reaction.game_vision \
  --source synthetic --config my_game_vision.yaml
```

未知key、非有限値、`clear_m >= max_m`、範囲外値は起動時に拒否します。

## 9. G1 PC2 → Ubuntu無線手順

この二つは別hostで実行します。どちらも既存の、承認済みTeleImager環境を使用します。

### 9.1 PC2 sender

D435iを競合なく開けることを確認できた場合だけ、D435iがUSB接続されたG1 PC2で実行します。

```bash
PYTHONPATH=src /exact/approved/python -m g1_bottle_reaction.game_vision \
  --source realsense \
  --realsense-serial YOUR_D435I_SERIAL \
  --vision-preset normal \
  --publish-processed \
  --publish-bind PC2_WIFI_BIND_IP \
  --g1-stream-port 55558 \
  --headless
```

optional safety streamも一度のcaptureから送る場合:

```bash
PYTHONPATH=src /exact/approved/python -m g1_bottle_reaction.game_vision \
  --source realsense \
  --realsense-serial YOUR_D435I_SERIAL \
  --vision-preset normal \
  --publish-processed \
  --publish-safety \
  --publish-bind PC2_WIFI_BIND_IP \
  --g1-stream-port 55558 \
  --safety-stream-port 55559 \
  --headless
```

送信前のnetwork prototypeは `--source synthetic` またはaligned `.npz` に置換できます。RGB-only sourceからのpublishは拒否します。

### 9.2 Ubuntu receiver

`PC2_WIFI_REACHABLE_IP` は監査で得た実際に到達可能なaddressに置き換えます。repository defaultの `192.168.123.164` をWi-Fi経路で到達確認せず盲用しません。

```bash
PYTHONPATH=src /exact/approved/python -m g1_bottle_reaction.game_vision \
  --source g1 \
  --g1-stream-host PC2_WIFI_REACHABLE_IP \
  --g1-stream-port 55558 \
  --fullscreen
```

起動後の標準出力は概ね次の形です。正確なRange/FOVはsenderが映像下部へ焼き込み、receiverは上部にlink FPSを表示します。

```text
G1 CAMERA CONNECTED
Resolution: 640x480
FPS target: 30
Fog/FOV: already applied by the PC2 sender
```

safety streamもsenderが有効にしている場合:

```bash
PYTHONPATH=src /exact/approved/python -m g1_bottle_reaction.game_vision \
  --source g1 \
  --g1-stream-host PC2_WIFI_REACHABLE_IP \
  --g1-stream-port 55558 \
  --safety-stream-port 55559 \
  --display both \
  --fullscreen
```

`--display safety` も同じ二portを購読し、raw側だけを大画面表示します。gameとsafetyは同一captureから順にpublishしますが、既存latest-frame managerに共通frame IDはないため厳密なpair同期は保証しません。commissioning用途に限ります。

ユーザー提示の一行commandにある `--vision-preset normal` はB構成ではreceiverではなくsenderへ指定します。受信映像からDepthを復元して再filterすることはしません。

## 10. 公式VideoClientのRGB-only診断

stock `videohub_pc4` を利用する既存の公式RGB経路は `--source g1-rgb` です。これは安全監視・接続確認用で、距離game streamとは別です。

```bash
PYTHONPATH=src /exact/approved/python -m g1_bottle_reaction.game_vision \
  --source g1-rgb \
  --network-interface YOUR_G1_INTERFACE \
  --display safety \
  --windowed
```

Windowsの既存DDS address方式:

```powershell
& C:\exact\approved-g1-venv\Scripts\python.exe -m g1_bottle_reaction.game_vision `
  --source g1-rgb `
  --network-address 192.168.123.222 `
  --display safety
```

公式 `VideoClient.GetImageSample()` はJPEG/BGRだけで、Depth、depth scale、RGBとの同期IDを返しません。`g1-rgb` をgame表示するとdefaultで全面黒の `DEPTH UNAVAILABLE - GAME VIEW CLOSED` になります。RGB/FOVだけの診断は明示的に `--allow-rgb-only-g1` を付けられますが、距離ゲームには使用しません。

## 11. 無線構成A/B比較

| 軸 | A: aligned RGB + DepthをUbuntuへ転送 | B: PC2で処理し表示画像を転送（採用） |
|---|---|---|
| 実装量 | lossless Depth、scale、shape、frame ID、timestamp、同期が必要 | single capture/filter後に既存画像transportへ載せる |
| latency | 二stream転送と同期待ちが増える | 一つのgame payloadをlatest-frame転送 |
| bandwidth | raw Z16 Depthだけで640x480x30は約147Mbps | sceneとcodec依存。gameはWebP、safetyはoptional JPEG |
| PC2負荷 | align + Depth圧縮/転送 | align + fog/FOV + WebP encode |
| 安定性 | 欠落時の誤pair対策が必要 | 古いpayloadをdropし、500ms staleで停止 |
| 将来YOLO | Ubuntuでmetric Depthを利用しやすい | PC2のFOV後・HUD前へ2D detectorを追加できる |
| safety | Ubuntuでrawを作りやすい | optional JPEGを別portへfan-out |

ゲーム性MVPではBが小さく、遠方RGBをPC2で捨ててから送れるため採用しました。Aは実装していません。

公式調査で確認した点:

- [Unitree SDK2 Python VideoClient](https://github.com/unitreerobotics/unitree_sdk2_python/blob/65691c8a8bc53b98d3976dba4dbf9d5d20b2e7f5/unitree_sdk2py/go2/video/video_client.py) の公開APIはcolor JPEG/BGRのみ
- [公式camera example](https://github.com/unitreerobotics/unitree_sdk2_python/blob/65691c8a8bc53b98d3976dba4dbf9d5d20b2e7f5/example/go2/front_camera/camera_opencv.py) も `GetImageSample()` をcolor画像としてdecode
- [TeleImagerのRealSense実装](https://github.com/unitreerobotics/teleimager/blob/f883d6a3c3a6f804306ca43257c52b9189312f2f/src/teleimager/server.py#L837-L901) は内部でDepth-to-Color alignするが、[publish loop](https://github.com/unitreerobotics/teleimager/blob/f883d6a3c3a6f804306ca43257c52b9189312f2f/src/teleimager/server.py#L1934-L1965) の公開payloadはcolor画像
- [xr_teleoperateの実機構成](https://github.com/unitreerobotics/xr_teleoperate/blob/817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b/README.md#L323-L364) はPC2 `192.168.123.164` のimage serviceを使用
- [RealSense公式align example](https://github.com/realsenseai/librealsense/blob/master/wrappers/python/examples/align-depth2color.py) と同様、Colorへalignしてからpixelを対応

調査対象の整理:

- stock Unitree Image Server / `videohub_pc4` / SDK2 `VideoClient` はcolor-onlyで、aligned metric Depthのclient APIは確認できません。
- TeleImager RealSense serverは内部でDepthを取得・alignしますが、公開ZMQ/WebRTC client outputはcolor-onlyです。そのserverをそのまま使ってUbuntuで距離filterすることはできません。
- `xr_teleoperate` はTeleImager image clientを利用しますが、pin版にもRGB+metric Depth pairの公開interfaceはありません。
- WebRTCはcolor配信候補ですが、本MVPでは既存managerのlatest-payload/drop動作を直接使え、追加signalingを要しないZMQを選びました。WebRTC経路は実装していません。

公式TeleImager serverにはUVC driverをsudoでreloadする経路があります。本ツールはserverを起動せず低level managerだけをlazy importします。TeleImager import時にnative JPEG runtimeが不足してprocess exitを試みる版もあるため、本ツールはそれをclean errorへ変換します。既存xr_teleoperate環境を上書きして解決しません。

`videohub_pc4` がRealSense deviceを占有し、停止してもwatchdogで再起動したという利用者報告があります。本repositoryでは停止・kill、watchdog変更、driver reloadを行いません。[Unitree xr_teleoperate issue #299](https://github.com/unitreerobotics/xr_teleoperate/issues/299)

## 12. Wi-Fiと遅延確認

PC2の既知addressと、Ubuntuから実際に到達できるWi-Fi addressは同じとは限りません。両hostのSSID、IPv4/subnet、routeを確認し、sender起動後にportを検証します。

```bash
# PC2
ip -br address
ip route
ss -ltnp | grep -E ':(55558|55559)\b'

# Ubuntu receiver
ip -br address
ip route get PC2_WIFI_REACHABLE_IP
ping -c 3 PC2_WIFI_REACHABLE_IP
```

firewall変更が必要と分かった場合は、対象interface/source subnet/portを限定した変更理由を説明してから行います。sudoを前提にしません。

TeleImagerのPUB/SUB stream自体には本ツールによる認証・暗号化がありません。senderはdefaultでloopback `127.0.0.1` にしかbindしません。無線試験では `PC2_WIFI_BIND_IP` を専用・信頼済みSSID上のPC2 addressへ明示し、`0.0.0.0` による全interface公開を避けます。ネットワーク外へのrouting/port forwardingは行いません。

低遅延方針:

- capture 640x480、30 FPSから開始
- webcam backendへbuffer size 1を要求
- application内にframe queueを作らない
- TeleImagerのCONFLATE/HWM=1 latest-frame transportを使用
- FOV maskをcacheし、NumPyでvector処理
- 初回3秒、以後500msにnew payloadがなければstale frameを再表示せず終了
- gameだけを基本とし、raw safety二本目は必要時だけ有効化

このWindows CPUでのtexture-like 640x480参考値では、game WebP+alpha encodeが約28–31ms、decodeが約6msでした。gradientはencode約16msでした。実画像、G1 PC2、Wi-Fi、optional safetyを含む15/25–30 FPS達成は未検証です。WebPが支配的なら、まずquality・scene別profilingを行い、要件を緩めずに別codec/containerまたはhardware accelerationを検討します。解像度増加やframe queue追加はしません。

`captured_at` は各processのlocal monotonic時刻で、sender timestampをtransportしていません。end-to-end latency値には使えません。LEDやmillisecond時計をG1 cameraと外部cameraで同時撮影し、motion-to-photonとfreeze-to-stopを測定します。

## 13. トラブルシューティング

### `pyrealsense2 is required`

指定Pythonからbindingが見えません。system-wide installやSDK upgradeをせず、Phase 0の結果から承認済みvenv、Python ABI、librealsense versionを特定します。

### `Device or resource busy`

別processがcameraを所有しています。`pgrep` と `fuser -v /dev/video*` で確認します。`videohub_pc4`をkillせず、Unitreeへ共存可能なDepth取得方法を確認します。

### `approved Unitree TeleImager environment is required`

current/pinned TeleImager moduleまたはnative JPEG runtimeを、そのPythonから利用できません。既存xr_teleoperate環境のexact Pythonで再確認します。このerrorを理由に最新版を上書きinstallしません。

### `no fresh game/safety G1 frame`

初回ならhost/route/port/sender、接続後ならsender停止・packet loss・PC2負荷を確認します。stale BGRを表示し続けないためのfail-closed停止です。歩行を止めてから調査します。

### `game payload lacks ... alpha mask`

receiverがraw safety portまたは互換性のないsenderへ接続しています。game port 55558と、このrepositoryのsender commandを確認します。rawをgame映像としてfallbackしません。

### `OpenCV build does not preserve ... WebP alpha mask`

既存OpenCVのWebP encoder/decoderが必要な4-channel alphaを保持しません。Phase 0 probeを両hostのexact Pythonで再実行します。自動upgradeやsystem-wide installは行わず、別codec採用を含めて事前に合意します。

### 公式G1 RGBは出るがgameが黒い

`--source g1-rgb` はDepthなしなので正しいfail-closed動作です。接続診断は `--display safety`、距離gameはPC2 sender + `--source g1` を使います。

### Webcam / MP4で距離が消えない

RGB-only inputでは仕様どおりです。synthetic、aligned `.npz`、RealSense `.bag`、またはdirect RealSenseを使います。

### fullscreenから戻れない

`F` でwindowed、`Q` またはEscで終了します。GUI sessionがなければ `--headless --max-frames N` を使います。

## 14. テスト

追加testはcamera、RealSense、Unitree SDK、TeleImager、network、GPU、GUIを必要としません。

```bash
python -m pytest \
  tests/test_game_vision_filters.py \
  tests/test_game_vision_config.py \
  tests/test_game_vision_sources.py \
  tests/test_game_vision_transport.py \
  tests/test_game_vision_viewer.py \
  tests/test_game_vision_cli.py
python -m pytest
```

対象:

- 1.0m visible、1.8m partial、3.0m black、invalid black
- fade/hard境界、FOV中心・端・対称性、mask cache
- RGB-only previewとofficial G1 RGB fail-closed
- YAML default/override/unknown/invalid/non-finite値
- mock RealSense alignment順序、depth units、RGB8変換、bag EOF/cleanup
- aligned `.npz`再生とunsafe pickle禁止
- WebP alphaによるhidden pixelのexact black、optional safety JPEG fan-out
- new payloadだけをframe化、duplicate/stale/disconnect timeout、cleanup
- processed `g1` とofficial `g1-rgb` のCLI分岐、remote再filter防止
- hardware-free CLI smoke、viewer composition/HUD

## 15. 実機確認チェックリスト

次はすべて **NOT VERIFIED ON REAL G1** です。

- [ ] 対象UbuntuとPC2のOS/Python/GPU/venvを再監査
- [ ] D435i serial、librealsense、pyrealsense2、firmwareを記録
- [ ] `videohub_pc4`を止めずにaligned RGB+Depthを取得できるか確認
- [ ] 既存xr_teleoperate/TeleImager exact commitとnative dependencyを確認
- [ ] PC2/Ubuntu双方の既存OpenCVでWebP alpha probeが成功
- [ ] PC2 Wi-Fi address、SSID/subnet、Ubuntuへのroute、TCP 55558/55559を確認
- [ ] 640x480 RGB+Depth 30 FPSとdepth/color alignmentを実景で確認
- [ ] 1.0/1.8/3.0m、invalid、黒/反射物でmaskを実測
- [ ] R3手動歩行とcapture/filter/encodeの同時安定性を確認
- [ ] gameのみで15 FPS最低、25–30 FPS目標、motion-to-photon遅延を測定
- [ ] optional safety併用時のFPS、帯域、PC2 CPU/温度を測定
- [ ] packet loss、sender停止時に500ms以内でviewerが終了することを確認
- [ ] wrong port/raw payloadをgameとして表示しないことを確認
- [ ] game/safety映像の相対遅延を確認
- [ ] 独立安全監視者、R3停止、試験範囲、緊急停止手順を確認

実機の最初のcamera確認は、D435iを直接開かないofficial RGB safety診断から始めます。

```bash
PYTHONPATH=src /exact/approved/python -m g1_bottle_reaction.game_vision \
  --source g1-rgb \
  --network-interface YOUR_G1_INTERFACE \
  --display safety \
  --windowed
```

Phase 0でcamera共存とnetworkを確認した後にだけ、§9のPC2 senderとUbuntu receiverを実行します。
