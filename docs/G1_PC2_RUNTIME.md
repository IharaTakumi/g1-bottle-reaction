# PC2 robot-side runtimeの準備

2026-09-05。G1未接続でローカル実装・テストまで実施。PC2への配備・venv作成・DDS購読は未実施。
1801/1802/1804や1102をこの準備では実行しない。

## 確認状況と不足依存の扱い

| 確認項目 | 分かっていること | 今回の実機確認 |
| --- | --- | --- |
| PC2 SDK/CycloneDDS | 過去の記録でPython 3.8＋既存SDKの直接DDS購読に成功した形跡あり | 接続がないため未検証。doctorで現在のimport成否・version・pathを確認 |
| rt/lowstate | 使用型はunitree_hg/msg/LowState_ | PC2上の現在の受信は未検証 |
| mapping/odom等 | 過去のPC2 recorderはOdometryを記録できていた | 現在の受信は未検証。mapping未開始で0件でも依存欠落とは限らない |
| Python | PC2は過去の調査で標準3.8。Desktopアプリは3.10+を要求 | 軽量runtimeを3.8互換の構文・標準APIで分離。実3.8/aarch64実行は未検証 |
| 最小依存 | PyYAML、CycloneDDS Python/native library、既存Unitree SDKとそのimport依存 | 正確な不足一覧はPC2 doctor実行後に確定 |

Desktopで確認できたのはPython 3.12でPyYAMLとCycloneDDS 0.10.2、SDKのLowState/Odometry/String型のimport成功。
これはPC2の環境や購読成功の代わりにはならない。ROS、Humble、OpenCV、YOLO等のDesktopアプリをruntimeからimportしない。
SDK自体のimportが要求する依存はdoctorが例外を表示する。ネイティブDDSのABI互換性もPC2実行で初めて確定する。

## 配置とネットワーク

推奨配置先はPC2 `/home/unitree/g1-slam-runtime`。新規配備先として人間が選ぶパスであり、現時点で存在確認済みではない。
必要なファイルはscripts/g1-slam-session.py、scripts/g1-pc2-doctor.py、scripts/setup-g1-pc2-env.sh、
robot_side/、config/g1-slam-session.yaml、docs/G1_NEW_MAP_LOCALIZATION.md、docs/G1_PC2_RUNTIME.md。
Desktopの.venv、.runtime、認証情報、アプリ本体は転送不要。SDKはPC2にある `/home/unitree/unitree_sdk2_python` を参照する。
接続回復後の配備はPC2へのWRITEなので、READ ONLY確認と区別して扱う。今回は転送していない。

DesktopはWi-Fi/SSHでPC2を操作するだけ。DDS/RPCはPC2 eth0、domain 0を使う。
readerとRPCは同じXMLを使い、Desktopのenp129s0設定は読み込まない。eth0/unitree1・IP・ROS設定・Unitree serviceを変更しない。
Linux/aarch64とeth0=192.168.123.164/24を読み取り確認し、不一致ならDDS参加前に拒否。
PC2のSDＫサービス名slam_operateへ送る仕組みで、IP .161を直接TCP接続先に指定するものではない。
.161はSLAM提供元としてのアーキテクチャ上の情報。session.jsonにruntime_host=.164、pcd_host=.161を明記する。
PCD addressに対してPC2上でopen/stat/exists確認をしない。

## 先に行うREAD ONLY確認（接続復旧後）

**配備前でも、Desktopで生成した診断コードをSSHの標準入力へ渡せる。PC2にスクリプトやvenvを作らない。**
以下は接続復旧後にDesktopで実行する手順で、今回実行していない。`PROJECT`はローカルcheckout、`PC2_SSH_TARGET`は自分のSSH接続先として端末内で設定する（実値はリポジトリに保存しない）。パスワードは通常のSSH入力画面へ入力し、コマンドやファイルへ記載しない。

```bash
cd "$PROJECT"
python3 scripts/build-pc2-probe.py | ssh -T "$PC2_SSH_TARGET" 'PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -'
```

import結果を確認した後、別コマンドで購読する。

```bash
python3 scripts/build-pc2-probe.py | ssh -T "$PC2_SSH_TARGET" 'PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B - --subscribe --seconds 15'
```

最終構成ではSSHの到達先を実際に確認したG1-AP側IPへ置き換えてよい（内部.164へのWi-Fi経路を仮定しない）。
SSH経路がどちらでも、PC2内のDDS/RPCはeth0/.164に固定。DesktopからDDS通信は行わない。

PC2にdoctorとrobot_sideが配備済みなら以下。まだ未配備なら、ファイル転送は別のWRITE準備として扱う。
既存Pythonの確認だけならvenv作成前に実行できる。-BとPYTHONDONTWRITEBYTECODEでpycacheを書かない。
doctorは標準ライブラリから起動し、yamlがない場合も不足依存を報告する。import確認はDDS participantを作らない。

```bash
cd /home/unitree/g1-slam-runtime
export G1_SDK_PATH=/home/unitree/unitree_sdk2_python
export PYTHONDONTWRITEBYTECODE=1
/usr/bin/python3 -B scripts/g1-pc2-doctor.py
```

必要なimportが成功した後の明示的な購読診断。アプリケーションwriter/RPC clientは作らず、端末に要約だけを出す。
通常のDDS discovery/ack通信は発生する。journal・設定やROS起動処理は使わない。

```bash
/usr/bin/python3 -B scripts/g1-pc2-doctor.py --subscribe --seconds 15
```

topicごとのsamplesとmatched、LowStateのtick、odomのframeとsource stampを確認する。
matchedだけでは購読成功としない。0件なら未確認として記録し、確認のために1801を送らない。
schemaが見つからない場合も別型に勝手に切り替えない。exit 2は依存/実行条件不成立、exit 3はLowState無受信。
exit 0でもmapping/relocationが0件なら、そのtopicの購読は未検証のまま。

## 専用環境の準備（後でPC2にWRITEする段階）

以下は手順を用意しただけで、今回実行していない。
既存Pythonでdoctorを実行してから、新しいプロジェクトローカルvenvを作る。

```bash
bash scripts/setup-g1-pc2-env.sh
```

標準Pythonの既存site-packagesを読み取り継承するvenv方式。パッケージを完全隔離する方式ではなく、
既に動くnative CycloneDDSを置き換えずに、追加インストール先をプロジェクト内に限定する方式。
既存venvは上書きせず、SDKもsystem Pythonも変更しない。スクリプトにpip/apt/sudoやネットワーク設定変更はない。
python3-venv/ensurepipがない場合は失敗して止まる。部分生成されたvenvも自動削除・上書きしない。
その場合、doctorの結果からユーザー領域Python等の別手段を選ぶ。/usrへのプロジェクト固有installは行わない。

不足がPyYAMLだけと分かった場合の追加先は `.venv-robot` に限定する。例（まだ実行しない）：

```bash
.venv-robot/bin/python -m pip install --no-cache-dir 'PyYAML==6.0.2'
```

CycloneDDS/native libraryやSDK import依存が不足していた場合は、現在のversion/pathとエラーを基に個別に準備する。
いきなりpip install -e .を行うとDesktop向け重い依存・Python>=3.10要件を持ち込むため、ここでは使わない。
sudo pip、pip --user、system/ROS環境へのインストール、既存SDK checkoutの編集はしない。

準備後に同じdoctorをvenvで再実行する。

```bash
.venv-robot/bin/python -B scripts/g1-pc2-doctor.py
.venv-robot/bin/python -B scripts/g1-pc2-doctor.py --subscribe --seconds 15
.venv-robot/bin/python -B scripts/g1-slam-session.py --help
```

**今回の停止点はここ。** init以降はG1_NEW_MAP_LOCALIZATION.mdのPC2用手順へ進むが、
1801/1802/1804はまだ送信しない。initはPC2にsessionファイルを作るローカルWRITE。
recordはPC2に受信ログを書く。start/save/relocateはそれぞれ独立した明示的な実機WRITE。

## ソース境界と検証

robot_side/adapters/g1_robot.pyがSDK importを集約する専用の軽量境界。
src/g1_bottle_reaction/adapters/g1_robot.pyを含むDesktopアプリの3.10+依存を読み込まない。
RPCのSDKチャネル設定は初期化時にプロセス内だけ差し替え、トレースを無効化する。
SDKファイルや既存DDS設定は書き換えない。READ ONLY doctor/recordはこのRPC初期化経路を使わない。
ネットワークなしのテストではNIC/architectureガード、旧Desktop session拒否、Python 3.8構文、単発送信契約を確認する。
