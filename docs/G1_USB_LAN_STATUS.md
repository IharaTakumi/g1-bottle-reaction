# G1に接続した追加USBカメラ: アクセス調査（2026-09-10）

追記: 以下はSSH認証前の調査記録です。その後、一時SSH接続で実機を確認し、
USB転送と内蔵カメラの2画面表示、USBの180度回転、連続5分試験まで成功しました。
現在の実行手順・結果は [G1_USB_DUAL.md](G1_USB_DUAL.md) を参照してください。

USBカメラはG1本体に接続したまま扱います。Ubuntuに/dev/videoがないのは正常です。
今回の停止点はG1内部へのSSH認証です。USB capture/転送/dual displayは未実装・未試験。
ユーザー指定の「shellアクセス不可なら推測で進めない」に従い、既存ツールの有無や
USBポートの接続先を想定したsenderは作成していません。

## 現在の到達性

Ubuntuの有線NICはenp129s0、IPv4は192.168.123.200/24。
ip routeで192.168.123.0/24が同NICへのdirect routeと確認しました。

| 宛先 | ICMP（有線NIC指定） | TCP到達可能 | 今回接続拒否だったTCPポート |
| --- | --- | --- | --- |
| 192.168.123.161 | 2/2応答、平均0.192ms | 調査範囲ではなし | 22, 80, 443, 554, 8554, 55555, 55558, 55559 |
| 192.168.123.164 | 2/2応答、平均0.172ms | 22, 80 | 443, 554, 8554, 55555, 55558, 55559 |

これは上記8ポートだけのTCP connect確認です。全ポートやUDPを調査したものではなく、
DDS映像サービスの不在も意味しません。ICMP往復時間は映像遅延ではありません。
近隣cacheには.201/.202もありますが、機器の役割・G1帰属は未同定であり調査対象にしませんでした。

## 既存の構成記録とアクセス

`/home/ubuntu/.ssh/config`には`g1`/`g1-jetson`/`192.168.123.164`を
`unitree@192.168.123.164`へ向ける既存設定があります。
docs/G1_NEW_MAP_LOCALIZATION.mdとG1_PC2_RUNTIME.mdでは、.164をJetson PC2、
.161をPC1/SLAM側と記録しています。**これは既存記録であり、今回のshellによる再確認ではありません。**
今回取り付けたUSBカメラがどの内部コンピュータにつながったかは未確認です。

.164の22番から`SSH-2.0-OpenSSH_8.2p1 Ubuntu-4ubuntu0.11`を受信しました。
既存host keyの検証を保ち、既存aliasにBatchModeで接続すると
`Permission denied (publickey,password)`です。ssh-agentにはidentityがありません。
これはネットワーク障害やSSHサービス不在ではなく、現在の非対話認証でログインできない状態です。
パスワードの推測、ユーザー名の総当たり、鍵の追加、SSH設定変更は行っていません。

## SSH以外の既存経路

- .164:80のGET /は200 OK、TornadoServer/6.3.3、HTML titleは`unitree-upgrade`。
  アップグレード画面の入口と判断できますが、追加USBカメラの取得・選択APIは確認できません。
  設定変更・アップグレード・管理API呼び出しは行っていません。
- 動作済み内蔵カメラは公式VideoClient、service `videohub`、API 1001 `GetImageSample()`。
  既存SDKのこのAPIにはUSB device pathやカメラindexを選ぶ引数がありません。
  内蔵カメラを読めることは、追加USBカメラも同じAPIで読める根拠にはなりません。
- リポジトリにはTeleImager経路がありますが、実機上の導入/稼働状態は未確認。
  今回確認したstream候補TCPポートへの接続は拒否されました。別ポートの存在は否定しません。

## 次の最小手順

Ubuntuの通常端末で、既存aliasと正規の資格情報でログインしてください。
パスワードはSSHの入力欄に入力し、チャット・コマンド引数・ファイルへ書かないでください。

```bash
ssh g1
```

入れない場合は、機体管理者に.164の正しいSSHユーザー/鍵/パスワードを確認してください。
別のユーザー名で入る構成なら、その正式な接続先を使用します。セキュリティ設定は変更しません。

入れたらG1側でまず次を実行すると、ホストとUSB/映像nodeを確認できます。

```bash
hostname; uname -m; ip -br addr; lsusb; ls -l /dev/video* /dev/v4l/by-id/ /dev/v4l/by-path/ 2>/dev/null; command -v python3 ffmpeg gst-launch-1.0 v4l2-ctl
```

さらにUbuntu側から下の一コマンドを実行すると、標準入力経由の診断をG1で行えます。
G1へのスクリプト配置やvenv作成は不要です。通常のSSHパスワード認証も利用できます。

```bash
ssh g1 'sh -s' < /home/ubuntu/dev/g1-bottle-reaction/tools/g1_usb_inventory.sh
```

診断はUSB ID/model、stable path、sysfsの接続経路、既存カメラprocess名、device使用状況、
既存v4l2-ctlで取得できるcapabilities/対応format・resolution・FPS、ffmpeg/GStreamer/Pythonの
存在を表示します。V4L2はqueryだけでstreamを開きません。ツールがない場合はinstallしません。
fuserで他ユーザーのprocessが見えない場合もあるため、表示なしを未占有の証明にはしません。

認識されたUSB cameraを既存カメラ/metadata nodeから区別した後、利用可能な既存ツールで
1フレーム取得→USBだけのLAN転送→dual表示→5分試験の順に進めます。
ffmpeg/GStreamerを使うかPython/OpenCVを使うか、port/codec/解像度は実機の結果で決めます。
UDPの場合も受信側が対応するcodec・packetizationを確認してから設定します。

## 現時点の結果

| 項目 | 状態 |
| --- | --- |
| USB camera detected | 未確認（shell未取得。NO＝未認識と断定しない） |
| G1 computer / IP | 既存SSH接続先.164。USB接続先hostは未確認 |
| USB device / model / resolution / FPS | 未確認 |
| capture method / protocol / codec / G1 port / Ubuntu port | 未選定 |
| USB latency / dropped frames | 未測定 |
| G1内蔵カメラ | 前回5分試験: 1920x1080、表示平均32.5 FPS、ユーザー目視で数秒遅延なし |
| 今回の内蔵カメラ再確認 | GUIなし5.0秒、195フレーム、取得平均38.7 FPS、エラー0、最大間隔0.046秒 |
| dual display / 2系統5分試験 | 未実施 |

今回G1側へのinstall・ファイル配備・system/firmware/サービス/制御設定変更はありません。
Ubuntuのvenv・system・condaも変更していません。追加dependencyなし。
追加したのはこの文書と`tools/g1_usb_inventory.sh`だけです。
既存G1映像取得コードは変更していません。
診断shellの`sh -n`と`git diff --check`を確認済みです。G1上での診断実行は認証待ちです。
