# G1内蔵カメラ完全無線化

2026-09-13にUbuntu `10.42.0.1`、G1 PC2 Wi-Fi `10.42.0.76`で確認。

## 原因

旧構成はUbuntu上で`ChannelFactoryInitialize(0, "wlp128s20f3")`を実行し、
Wi-Fi越しにUnitree `VideoClient.GetImageSample()`を直接呼んでいた。初期化は完了するが
RPC送信が3102となり、フレームは0だった。

同じAPIをG1 PC2上の`eth0 / domain 0`でreceive-only実行すると、1920x1080 JPEGを
10.001秒で499枚（49.90 FPS、エラー0）取得できた。このためカメラserviceではなく、
Ubuntu Wi-Fi上のdirect Unitree DDS経路が原因と判断した。

## 採用構成

`--g1-camera-transport ssh-rtp`では、Ubuntuから認証済みSSHでG1上に小さいcamera senderを起動する。
senderはG1ローカル`eth0 / domain 0`のVideoClientからJPEGを受け、decode/re-encodeせず、
既存GStreamerの`jpegparse -> rtpjpegpay -> udpsink`でWi-Fiへ送る。

2026-09-16の会場Wi-FiではRTP packet自体はUbuntuへ到達したが、JPEG断片が
大量に欠落した。そのため`--g1-camera-transport ssh-jpeg`を追加した。
この経路は同じreceive-only VideoClientから取得した元JPEGを、lengthとsequence付きの
binary stdoutでSSH転送する。logはstderrに分離し、Ubuntu側は復元後に既存LatestReaderへ渡す。

- G1内蔵カメラ: RTP/JPEG UDP 56001
- 頭部USBカメラ: RTP/JPEG UDP 56000（従来どおり）
- G1/Ubuntu IPはCLIから渡し、コードに固定しない
- SSH process終了時はsender自身が起動したGStreamerだけを停止
- Motion、Navigation、LowCmd、arm_sdk、SLAM RPC、service restartは行わない
- `--no-usb-camera`ではUSB sender/receiverを作らず、G1だけでGUIとYOLOを継続
- `--g1-camera-transport direct-dds`は既存有線利用のため残す
- `--g1-camera-transport ssh-rtp`も従来環境向けに残す

G1音声も同じSSH targetを使うが、AudioClientはG1上の`eth0 / domain 0`で動作する。

## 確認結果

- G1-only headless: 219 frames、23.3 FPS、RTP lost 0、exit 0
- G1-only + YOLO: 441 frames、24.8 FPS、YOLO 13.9 FPS、RTP lost 0
- windowed + YOLO: X11 window 1280x540を確認し、実映像とperson boxを画面キャプチャで確認
- person / banana / plushie (`teddy bear`, COCO class 77) の推論対象を維持
- G1-local plushie WAV再生: volume 85、playback完了、exit 0
- USB未接続でもG1-onlyで正常終了

## 起動

断片化UDPが不安定な会場Wi-Fiでのcamera-only起動：

```bash
/home/ubuntu/.venvs/g1-game-vision/bin/python -B /home/ubuntu/dev/g1-bottle-reaction/tools/g1_dual_camera.py --usb-bind 10.42.0.1 --usb-host 10.42.0.76 --network-interface wlp128s20f3 --ssh-target unitree@10.42.0.76 --ssh-control /home/ubuntu/dev/g1-bottle-reaction/.runtime/usb-camera-ssh/wifi-control-upper --g1-camera-transport ssh-jpeg --g1-camera-fps 10 --no-usb-camera --headless --duration 30
```

この命令はYOLO、Reaction、motion、audioを起動しない。実測は1920x1080、98 frames、
平均3.7 FPS、display drop 0、連続LIVE 21.6秒だった。VideoClient取得が律速であり、
CLI指定の10 FPSは上限であってフレームの複製はしない。

従来のRTP経路：

```bash
/home/ubuntu/.venvs/g1-game-vision/bin/python -B /home/ubuntu/dev/g1-bottle-reaction/tools/g1_dual_camera.py --usb-bind 10.42.0.1 --usb-host 10.42.0.76 --ssh-control /home/ubuntu/dev/g1-bottle-reaction/.runtime/usb-camera-ssh/wifi-control --g1-camera-transport ssh-rtp --g1-camera-port 56001 --g1-camera-fps 30 --no-usb-camera --windowed --yolo --yolo-confidence 0.25 --banana-confidence 0.25 --plushie-confidence 0.25 --found-audio --found-output g1 --found-duration 0.3 --audio-cooldown 2.0
```

USBカメラを再接続する場合は`--no-usb-camera`を外し、必要なら`--start-usb-sender`を追加する。
