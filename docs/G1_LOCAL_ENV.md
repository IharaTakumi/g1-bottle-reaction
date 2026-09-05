# 共有Ubuntu PC: G1ローカル環境とREAD ONLY診断

> 過去のDesktop Ethernet/DDS調査記録です。現在の構成・再開手順は[G1_PC2_RUNTIME.md](G1_PC2_RUNTIME.md)を参照してください。以下の未確認事項や結果は調査時点のものです。

## 再開

```bash
cd ~/dev/g1-bottle-reaction
source scripts/activate-g1-env.sh
scripts/check-g1-env.sh
python scripts/read-g1-state.py --mode discovery --seconds 10
python scripts/read-g1-state.py --mode lowstate --seconds 10
python scripts/read-g1-state.py --mode slam --seconds 10
```

全診断は読み取り専用。DDS discovery/ACKなどのプロトコル通信は発生しますが、
アプリケーションのPublisher、DataWriter、RPC clientを生成しません。
`read-g1-state.py` は enp129s0 / domain 0 固定、既定10秒、最大60秒で終了します。
データなしは終了コード2。SLAMモードの成功は少なくとも1トピックの受信を示すだけで、
pose取得成功を意味しません。各トピックの件数を確認してください。

activationはvenv、pip/XDG/uvキャッシュ、TMPDIR、Pythonユーザーsite無効化、
CycloneDDSライブラリ・設定の場所を現在のシェルに設定します。
`.bashrc`、`.profile`、global Git config、OS Pythonは変更しません。
終了時はシェルを閉じてください（`deactivate`だけでは独自環境変数は戻りません）。
この環境自体はモーション禁止を強制するサンドボックスではありません。
今回の操作は上記診断だけに限定してください。

IPは一時設定のため再起動後に消える可能性があります。
activation/診断はIP設定を変更しません。enp129s0に192.168.123.99/24がなくなった場合は、
共有PCの利用者と調整して一時IPを復元してから再診断します。
NetworkManagerへの恒久設定は作成していません。

## 配置と導入判断

| 場所 | 内容 |
| --- | --- |
| `.venv/` | 既存Python 3.12.3通常開発環境。パッケージ変更なし |
| `.venv-g1/` | Python 3.12.14、プロジェクト＋dev、Unitree SDK2 Python |
| `.runtime/python/` | uvが取得したヘッダー付きCPython 3.12.14 |
| `.runtime/uv-tool/` | プロジェクトローカルuv 0.12.10 |
| `.vendor/unitree_sdk2_python/` | 公式SDK checkout、editable install |
| `.vendor/cyclonedds/` | 公式CycloneDDS C 0.10.2ソース |
| `.runtime/cyclonedds/` | Cライブラリとヘッダーのローカルinstall prefix |
| `.runtime/build/` | CMake生成物・ビルドログ |
| `.runtime/` | 診断結果、依存lock、環境manifest、キャッシュ、一時ファイル |

公式SDKのsetup.pyはPython >=3.8、CycloneDDS Python ==0.10.2、numpy、opencv-pythonを要求。
プロジェクトはPython >=3.10、Python 3.12ではnumpy >=2.1.3,<3、opencv-python <5を要求します。
SDKのpinは変更していません。Python 3.12用0.10.2 wheelが取得できず、ソースビルドしました。
OS Pythonの開発ヘッダーがないため、apt追加ではなくローカルCPythonを使いました。
CMakeは既存gccを使用。SSL/security、examples、tests、ddsperfはビルド無効です。
OSパッケージ追加、/usr/localへのinstall、ldconfig実行はありません。

SDK commit: `65691c8a8bc53b98d3976dba4dbf9d5d20b2e7f5`

CycloneDDS C commit (tag 0.10.2): `9995905bce6c4cf9f740d6438bbf7fcfd1c83dfd`

詳細は `.runtime/g1-environment.json` と `.runtime/g1-requirements.lock.txt`。
通常依存は `.runtime/dev-baseline.txt` のバージョンに合わせて別venvへ導入しました。
通常環境のパッケージ一覧は作業前後で一致しました。
`.git/info/exclude` に `.venv/`、`.venv-g1/`、`.runtime/`、`.vendor/` を登録しています。

## 再構築の主要コマンド

既存環境を上書きする自動セットアップは作成していません。下記はソース取得済みの環境での
ビルド手順の記録です。再起動後は再インストール不要です。

```bash
source scripts/activate-g1-env.sh
cmake -S .vendor/cyclonedds -B .runtime/build/cyclonedds \
  -DCMAKE_INSTALL_PREFIX="$PWD/.runtime/cyclonedds" \
  -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF -DBUILD_DDSPERF=OFF \
  -DENABLE_SSL=NO -DENABLE_SECURITY=NO
cmake --build .runtime/build/cyclonedds --parallel 4
cmake --install .runtime/build/cyclonedds
python -m pip install -c .runtime/dev-baseline.txt -e '.[dev]' -e .vendor/unitree_sdk2_python
python -m pip check
python -m pytest -o cache_dir=.runtime/cache/pytest-g1
```

uvはbootstrap用venvのpipから `--target .runtime/uv-tool` へ導入し、
`UV_PYTHON_INSTALL_DIR=.runtime/python`、`UV_CACHE_DIR=.runtime/cache/uv`、
`UV_PYTHON_BIN_DIR=.runtime/bin` を絶対パスで指定して
`uv python install 3.12 --no-bin` を実行しました。
`.venv-g1` は取得したCPythonを明示指定して作成しています。

## 実測結果（2026-09-05、このPC・接続機）

- Ubuntu 24.04.4、enp129s0 UP、192.168.123.99/24。Wi-Fiがdefault route。
- 192.168.123.161 / .164: interface指定ping、各2/2受信、損失0%。
- 通常venvとG1 venv: `pip check`正常、`pytest`各174 passed, 1 skipped。
- Unitree 1.0.1 / CycloneDDS 0.10.2 / G1 LowState schema import成功。
- Mock Navigation: IDLE、connected=True。Mock simulationも確認。
- DDS discovery: 10秒で288外部publication/subscription endpointを観測。
  publication行は**他participantの公開情報**であり、この診断からのpublishではありません。
- `rt/lowstate` (`unitree_hg::msg::dds_::LowState_`): 10秒で954サンプル、954異なるtick。
  IMU quaternion/rpy/gyro/acceleration、joint q/dq、mode_machineを受信。
  motor_state配列は35スロットですが、物理的な35関節という意味ではありません。
- `rt/slam_info`: 10秒で55サンプル。観測したctrl_infoは
  `info="not init"`、`stateMachine.state="ready"`、`errorCode=0`、currentPoseは0。
  **有効なSLAM poseやnavigation準備完了とは判定しません。**
- `rt/slam_key_info`、`rt/unitree/slam_mapping/odom`、
  `rt/unitree/slam_relocation/odom`: この10秒の購読では受信0。
  公開endpointは存在しますが、非稼働・QoS・型互換性等の原因は未確定です。
- `rt/api/slam_operate/request` / `response`、SLAM points・map等のendpointは観測。
  RPC呼出し、SLAM初期化、地図変更、pause/stop、姿勢・関節・歩行変更は未実施。

観測ログ: `.runtime/check-g1-env.log`、`g1-discovery.jsonl`、
`g1-lowstate.jsonl`、`g1-slam.jsonl`、各`.err`、`pytest-dev.log`、`pytest-g1.log`。
stateは約1秒に1回だけJSON出力し、終了時に受信総数を出力します。
生のSLAM文字列は最大16384文字に制限します。

## 次の段階への引き継ぎ

RemoteNavigationAdapterには本番transportがまだありません。
今回の受信結果をNavigationStatusへ推測でマッピングしません。
次回は搭載SLAM serviceの正確なAPI/schema、firmware対応、初期化条件、
frame/timestamp、pause/stopとlease watchdogを確認します。
現在poseを得るためにSLAMを起動・初期化する操作は行っていません。
実機のモード変更、pause/stop試験、Mock Robot + Real Navigation、waypointは別途明示許可が必要です。

公式exampleのG1 low-level/arm SDKコードにはcommand Publisherがあります。
名前にstate/readが含まれていても未監査exampleを実行しないでください。
本プロジェクトの既存 `--g1-test connection` はAudioClientも初期化するため今回未使用。
公式channel設定には `/tmp/cdds.LOG` があるため、この診断はSDK channel factoryを使わず、
`config/g1-readonly-dds.xml` とCycloneDDS DataReaderを使用します。
Unitreeの直接importはAGENTS.mdに従い `UnitreeSdkRuntime` のlazy loaderに隔離しました。

参考: [Unitree SDK2 Python公式](https://github.com/unitreerobotics/unitree_sdk2_python)、
[CycloneDDS C 0.10.2](https://github.com/eclipse-cyclonedds/cyclonedds/tree/0.10.2)。

最新の実機READ ONLY調査: [G1_NAVIGATION_READONLY.md](G1_NAVIGATION_READONLY.md)。
