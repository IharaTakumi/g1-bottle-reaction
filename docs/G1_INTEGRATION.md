# Ubuntu / Unitree G1 integration

この文書の項目は未実機検証です。SDKやファームウェアのバージョンを固定し、Unitree公式資料と実機の安全手順でAPIを再確認してから進めてください。未確認APIを推測して追加しないでください。

## 差し替えポイント

| Windows prototype | Ubuntu / G1 target | 残作業 |
|---|---|---|
| USB Webcam | G1 D435i | 公式映像取得経路とRealSenseのフレーム形式を確認し、detector入力へ変換する |
| `MockRobotAdapter` | `G1RobotAdapter` | SDK client初期化、timeout/domain、安全停止、各Motionを実機検証する |
| Windows/console Speech | G1 `AudioClient` | `TtsMaker`, `PlayStream`, `PlayStop` の正確な引数と音声形式を対象SDKで確認する |
| bounding box `proximity_ratio` | RealSense Depth | 深度の欠損処理とボトル領域内の堅牢な代表距離を設計する |

## SDKの扱い

`unitree_sdk2_python` はWindows用 `pyproject.toml` 依存関係に含めません。Ubuntuの専用環境へ、対象ロボットで検証した公式SDK revisionを別途導入します。コード内のSDK importは `src/g1_bottle_reaction/adapters/g1_robot.py` の `initialize()` に隔離されています。

公式SDKで存在を再確認する対象は `ChannelFactoryInitialize`, `LocoClient`, `LocoClient.WaveHand()`, `AudioClient`, `AudioClient.TtsMaker()`, `AudioClient.PlayStream()`, `AudioClient.PlayStop()` です。これらの存在だけでは、`notice`, `reach_forward`, `guard`, `look_around`, `surprise` との安全かつ正確な対応を意味しません。現在、それらのG1 Motionは意図的にno-opです。

## 安全な導入順

1. G1に接続しないUbuntu PCでコアテストとsimulationを実行する。
2. SDK revisionとG1 firmwareの対応を公式資料で固定する。
3. Channelとclient初期化を読み取り中心で検証する。
4. 非歩行・低リスクの操作から、周囲を確保して個別に検証する。
5. 緊急停止、timeout、切断、アプリ終了時の安全動作を実装する。
6. 検証済みのMotionだけをReaction名へ明示的に対応付ける。
7. 最後にAudioとD435i/Depthを統合する。

実機起動は常に `--robot g1 --network-interface <name> --enable-real-robot` の三つを要求します。Windowsではフラグの有無にかかわらずG1 adapterが拒否します。

