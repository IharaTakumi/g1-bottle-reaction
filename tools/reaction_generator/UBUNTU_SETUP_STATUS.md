# Ubuntu構築・検証結果（2026-09-05）

パス変数は各自のcheckoutを指します。個別マシンの絶対パスは記載しません。

対象：`$PROJECT`。今回の依頼はまだ完了していません。SMPL/SMPL-Xモデル未配置のため、`hmr4d_results.pt` と `motions/surprised.npz` は未生成です。動画からのGMR retargetと最後の1コマンド化の実証は、GVHMR単体成功後に続けます。

## 完了

- Ubuntu 24.04、RTX 5060 Ti 16GB、NVIDIA Driver 595.84を確認。
- project-local micromamba 2.8.1、独立したPython 3.10.21環境2つを構築。
- GVHMR: PyTorch 2.7.1/cu128、torchvision 0.22.1、PyTorch3D 0.7.9をCUDA 12.8でソースビルド。実GPU行列演算とPyTorch3D KNN演算に成功。
- GMR: CPU版PyTorch 2.3.0、MuJoCo 3.12.0、Mink 1.3.0。G1のMJCF読込、Minkタスク初期化、DAQP利用可否、EGL描画を確認。これは動画retargetの成功を意味しません。
- 公開checkpoint 4点を取得（約5.2GB）。GVHMR checkpointはZIP整合性も確認。
- setup再実行で既存環境を再インストールせず検査に進むことを確認。終了コード2は不足するライセンスモデル3配置先をdoctorが報告するため。
- pip constraintsとconda explicit lockを `ubuntu/locks/` に保存。conda再作成計画とGVHMR依存解決のdry-runも成功。
- 既存G1環境check成功。依存一覧は `~/g1-env-before-reaction-setup.txt` と作業後で差分なし。`.bashrc`、`.profile`、`/usr/bin/python3`のSHA256一致。
- 既存ゲームを含むpytest：184 passed, 1 skipped。`--simulate`も正常終了。
- システムパッケージ、Driver、既存G1環境へのinstallやshell設定の変更は実施していません。

## 動画の実行結果

入力：`input/surprised_01.mp4`、406×720、30fps、127フレーム、約4.23秒。画面内の人物が横向きのため、元ファイルを残して左90度回転した `.reaction-cache/normalized/surprised_01.mp4` を作成しました。

固定カメラ指定 `-s` でGVHMRを実行し、次の前処理に成功しました。

```text
.reaction-cache/manual/gvhmr/surprised_01/preprocess/bbx.pt
.reaction-cache/manual/gvhmr/surprised_01/preprocess/vitpose.pt
.reaction-cache/manual/gvhmr/surprised_01/preprocess/vit_features.pt
```

3D推論のモデル初期化が `hmr4d/utils/body_model/smplx_lite.py` の `assert smplx_path.exists()` で停止しました。単なる「CUDA不可」ではなく、必要なライセンスモデルの不在です。

## 再開に必要な配置

本人がライセンスに同意して取得したファイルを置いてください。取得済みの保存先があれば、そのパスを指定すれば配置作業を続けられます。

```text
$GVHMR_ROOT/inputs/checkpoints/body_models/smplx/SMPLX_NEUTRAL.npz
$GVHMR_ROOT/inputs/checkpoints/body_models/smpl/SMPL_NEUTRAL.pkl
$GMR_ROOT/assets/body_models/smplx/SMPLX_NEUTRAL.npz
```

取得先：[SMPL-X](https://smpl-x.is.tue.mpg.de/)、[SMPL](https://smpl.is.tue.mpg.de/)。同じSMPL-XファイルをGMRにも使用できます。

配置後、[Ubuntu手順](README_UBUNTU.md) のGVHMR単体コマンドから再開します。保存済み前処理はGVHMRが再利用します。GMR実動画確認→cleaner→腕14関節50Hzのnpz→元動画からの1コマンド実証の順に続けてください。

## ログ・使用領域

- `.reaction-setup.log` / `.reaction-setup-rerun.log`
- `.reaction-gvhmr-manual.log`
- `.reaction-gmr-smoke.log`
- `.reaction-tests.log` / `.reaction-simulate.log` / `.reaction-g1-check.log`
- `.reaction-tools/locks/g1-before.txt` / `g1-after.txt`

`.reaction-tools` はビルド・ダウンロードcache込みで約20GB、公開checkpointは外部GVHMR repo下に約5.2GBです。teardownは環境・cacheのみを削除し、外部repo内のcheckpointは残します。
