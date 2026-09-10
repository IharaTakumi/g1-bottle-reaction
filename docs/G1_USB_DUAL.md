# G1内蔵 + G1頭部USBカメラ: 有線2画面表示

起動順序の注意（2026-09-10実機確認）: **追加USBを外してG1を起動し、内蔵映像が復帰してから追加USBを挿してください。**
追加USBを挿したまま再起動するとvideo番号が変わり、標準サービスの固定device指定とずれて
内蔵配信が起動失敗しました。公式サービス設定は変更せず、上記起動順序で復帰を確認しています。
G1だけに人物検出を追加する実験は [G1_PERSON_YOLO.md](G1_PERSON_YOLO.md) を参照してください。

追加USBカメラはG1に挿したまま使います。UbuntuにUSBカメラを挿す必要はありません。
既存のG1最小ビューアとカメラ取得コードは変更していません。
YOLO、PyTorch、Depth、ロボット制御は追加していません。USBだけ180度回転できます。

## 実機で確認した構成

G1: hostname ubuntu、aarch64、Ubuntu 20.04.6、5.10.104-tegra、Python 3.8.10。
eth0=192.168.123.164/24。ユーザーunitreeはvideoグループに所属。
Ubuntu PC: enp129s0=192.168.123.200/24。

| 項目 | 結果 |
| --- | --- |
| 追加USBカメラ | Innomaker-U20CAM-1080p-S1 / USB ID 0c45:6366 |
| USB接続先 | G1の上記host、sysfs usb1/1-3 |
| stable device | /dev/v4l/by-id/usb-Innomaker_Innomaker-U20CAM-1080p-S1_SN0001-video-index0 |
| 当日のvideo node | /dev/video6。video7は同カメラの別nodeで、captureには使用しない |
| 取得方法 | 既存GStreamer 1.16.3のv4l2src |
| 検証済み形式 | image/jpeg (MJPEG)、1280x720@30指定 / 640x480@30指定、各60フレーム取得成功 |
| 採用 | 1280x720。USB単独受信301フレーム、平均29.6 FPS、RTP loss=0 |
| 内蔵カメラ | RealSense D435i + 既存videohub_pc4（起動・停止・設定変更なし） |
| 内蔵取得 | 同じ公式VideoClient / G1CameraSource / trace-free CameraRuntime |

G1側のffmpeg/v4l2-ctlは見つかりませんでした。
既存GStreamerのv4l2src/jpegparse/rtpjpegpay/udpsinkを実機確認し、そのまま使用しています。

## LAN転送と独立性

G1の追加カメラが出すJPEGを再エンコードせず、RTP/JPEG（payload 26、90kHz clock、MTU1200）で送ります。
送信先はUbuntuの指定IPv4:UDP56000。G1の送信socketは指定したG1 LAN IPv4にbindします。
送信元UDPポートはOS割当です。G1側に新しい待受映像ポートは開きません。制御用SSHは既存TCP22です。
Ubuntu受信は指定IPv4だけにbindします。全interface公開やfirewall/route変更はありません。
受信socket自体の送信元認証・暗号化はありません。信頼した直結LAN内で使用します。

G1側queueは1フレーム、leaky=downstream。Ubuntuのrtpjitterbufferは30ms/drop-on-latency、
appsinkはmax-buffers=1/drop=trueです。欠損packetの再送待ちはしません。
GStreamerのRTP depayloaderがJPEGフレームを組み立てます。壊れてdecode不能なJPEGは表示しません。

Ubuntuの既存venvのOpenCVはGStreamer非対応です。そのため既存の
`/usr/bin/python3` + GI/GStreamer 1.24.2の別プロセスでUDP受信し、JPEGをローカルpipeへ渡します。
表示側は専用venvのOpenCVでdecodeします。system packageの追加はありません。
G1のSDK取得も別プロセスです。各pipeを専用reader threadで常時読み、各1枚のlatest frameへ上書きします。
pipeはローカルの小さなOS bufferで、画像を大量保存するqueueはありません。

G1映像は1920x1080で取得後、ローカルpipe転送用だけ960x540へ縮小しています。
これはLAN上のG1映像の解像度設定を変えません。最終画面はアスペクト比を維持してletterbox表示します。

一方の取得停止・native abort・USBのUDP停止があっても、もう一方とGUIは継続します。
500ms以上新しいframeがなければそのpanelを黒いLOST表示にします。
G1側readerは連続5秒画像取得失敗で終了します。終了したreaderは自動再起動しません。
USB受信はUDP途絶中も待機し、sender再開時は新しいpacketを受信できます。
状態とFPSだけで、カメラ自体が同じ画像を繰り返す故障を検出できるわけではありません。

## 起動

Ubuntu PCのデスクトップ端末で一コマンド:

```bash
/home/ubuntu/.venvs/g1-game-vision/bin/python -B /home/ubuntu/dev/g1-bottle-reaction/tools/g1_dual_camera.py --usb-bind 192.168.123.200 --usb-host 192.168.123.164 --start-usb-sender --usb-rotate 180 --windowed
```

既存SSH alias `g1`を使い、必要ならSSHパスワードを端末で入力します。
senderのPythonコードをSSH経由の`python3 -B -c`で実行するため、G1側にファイルを配置しません。
G1側で別コマンドを手入力する必要はありません。既存systemd/service設定にも追加しません。
GUI終了時にはSSH stdinを閉じ、G1側supervisorが**自分で作ったGStreamer processだけ**を終了します。
切断時も同じcleanupが働きます。senderは最大1時間で終了します（viewerはLOST表示）。

`--ssh-target`で確認済み別aliasを指定可能。
既存の作業用ControlMasterを使う場合は
`--ssh-control /home/ubuntu/dev/g1-bottle-reaction/.runtime/usb-camera-ssh/control`を追加します。
ControlMasterは単なる一時認証済み接続で、パスワードをファイルに保存しません。

現在値を確認してからIPv4を指定してください。自動sender開始前にdirect routeと送信元IPv4を確認し、
既存有線経路と一致しなければ起動を拒否します。ネットワーク設定は変更しません。

キー: `1`=G1のみ、`2`=USBのみ、`3`=2画面、`f`=fullscreen切替、`q`/Esc=終了。
`1`/`2`でも両方のreaderは動き続け、切替時に古い映像を再生しません。
`--usb-rotate 180`はUSBだけに適用し、HUDの文字やG1映像は回転しません。

USBだけを起動する場合は同じコマンドに`--source usb-lan`を追加します。
既存のmodule CLIでも`--source dual` / `--source usb-lan`を使用できます（PYTHONPATH=srcが必要）。
G1だけなら引き続き`tools/g1_camera_minimal.py`を使えます。

## 手動sender（必要な場合だけ）

自動senderと同時に起動しないでください。G1にSSHした端末で:

```bash
GST_REGISTRY=/dev/null GST_REGISTRY_UPDATE=no gst-launch-1.0 -e v4l2src device=/dev/v4l/by-id/usb-Innomaker_Innomaker-U20CAM-1080p-S1_SN0001-video-index0 do-timestamp=true ! image/jpeg,width=1280,height=720,framerate=30/1 ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! jpegparse ! rtpjpegpay pt=26 mtu=1200 ! udpsink host=192.168.123.200 bind-address=192.168.123.164 port=56000 sync=false async=false
```

終了はその端末でCtrl+C。Ubuntu側コマンドは`--start-usb-sender`を省略します。
systemや既存Unitreeサービスをkillする操作は不要です。

## 指標の読み方

- FPS: PCで得られたフレーム頻度。G1 GetImageSampleにはsequence/timestampがないため、ユニークな露光回数とは限りません。
- local age: PC上の取得完了（G1）またはGStreamer appsink受信（USB）から、表示ループまでの経過時間。
  **センサー露光・USB転送・LAN・jitterbufferより前の遅延は含みません。**
- display skip: 表示側が読む前にlatest slotで上書きされたフレーム数。古いframeを捨てた数であり、通信欠損ではありません。
- RTP lost: USB受信GStreamerの欠損packet数。フレーム数ではありません。G1はこの統計を持たず、0欄は欠損0の証明ではありません。

ユーザーによる回転後の確認: 「向きは正しく、両方とも大きな遅延なし」。
撮影から表示まで200ms以下の達成は、同期時計等で実測していません。

## 最終実機試験（2026-09-10）

USBを180度回転した2画面viewerを330秒起動し、両カメラが連続LIVEだった最長区間は327.3秒でした。
開始時の接続待ちを含めず5分以上を達成しています。

| カメラ | 取得解像度 | 受信フレーム数 | 平均FPS | 表示前の上書き破棄 | RTP欠損packet |
| --- | --- | --- | --- | --- | --- |
| G1内蔵 | 1920x1080 | 11793 | 36.0 | 15 | 計測不可 |
| 頭部USB | 1280x720 | 9672 | 29.5 | 112 | 0 |

ログ: `.runtime/usb-dual-rotated-330s.log`。FPSは取得回数で、特にG1のユニーク露光FPSとは限りません。
回転前の短い試験ではG1取得が連続失敗してreaderが終了しましたが、USB側は継続しました。
再起動後の上記330秒試験では同じ停止は再現していません。将来の無停止を保証する結果ではありません。

続けて実機で、自作senderだけを停止・再開し、自作G1 readerだけを終了させて確認しました。

- USB送信停止後もG1取得継続、USBは500ms超でLOST扱い: PASS。
- USB送信再開後、受信readerを再起動せず映像復帰: PASS。
- G1 reader終了後もUSB取得継続: PASS。
- 物理的なUSB抜き差しは未試験。sender自体の自動再起動はありません。

自動テストは311 passed / 1 skipped。回転のUSB限定適用、CLI、letterbox、2画面合成、
停止時の黒表示、latest-slot破棄、IPC異常、reader障害分離を含みます。
動きへの追従はユーザーの「両方とも大きな遅延なし」という目視確認が根拠です。
R3歩行を5分間継続したかは別途未確認であり、プログラムから移動命令は発行していません。

## 変更と復元

追加: `game_vision/camera_ipc.py`、`game_vision/dual.py`、`tools/g1_camera_pipe.py`、
`tools/g1_usb_send.py`、`tools/g1_dual_camera.py`、`tests/test_dual_camera.py`、この文書。
既存コード変更: `game_vision/app.py`に独立したCLI分岐と追加引数のみ。
案内文書: `docs/G1_GAME_VISION.md`、`docs/G1_USB_LAN_STATUS.md`に最新手順へのリンクを追記。
依存追加・venv変更・system/conda/firmware変更なし。G1側へのファイル配備なし。
一時SSH socketと試験ログ/cacheはローカル`.runtime/`以下です。

復元はviewerを終了し、上記追加ファイルとapp.pyの今回の追加だけを戻します。
前段で動作済みの`g1_camera_minimal.py`やSDK camera loaderは削除しないでください。
一時SSH接続を閉じる場合:

```bash
ssh -S /home/ubuntu/dev/g1-bottle-reaction/.runtime/usb-camera-ssh/control -O exit g1
```
