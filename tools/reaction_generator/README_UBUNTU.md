# 共有Ubuntu PC：動画からG1腕リアクション

現在の到達点と未完了項目は [構築・検証結果](UBUNTU_SETUP_STATUS.md) を参照してください。

この手順は `~/dev/g1-bottle-reaction` で実行します。OS Python、既存 `.venv-g1`、Driver、システムCUDA、shell設定に依存を追加しません。G1実機への送信は行いません。

## 初回構築

```bash
cd ~/dev/g1-bottle-reaction
bash tools/reaction_generator/setup_ubuntu.sh
python3 tools/reaction_generator/doctor.py
```

micromamba 2.8.1をSHA256照合して `.reaction-tools/bin` に配置し、Python 3.10環境を `.reaction-tools/envs/gvhmr` と `gmr` に作成します。`conda init`、activate、sudoは不要です。環境を有効化済みのシェルから実行してもインストール先は変わりません。

RTX 5060 Ti用の構成はGVHMR公式PyTorch 2.3/cu121から、PyTorch 2.7.1/cu128、torchvision 0.22.1、PyTorch3D v0.7.9のソースビルドに変更しています。CUDA Toolkit 12.8とコンパイラもGVHMR環境内のみです。ビルドは2並列、対象GPU architectureは12.0です。他世代GPU向けの汎用セットアップではありません。GMRはCPU版PyTorch 2.3、NumPy 1.26.4の別環境です。

GVHMR runner内のみ、PyTorch 2.6以降のcheckpoint読込既定値の変更を `TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1` で互換化します。このrunnerには公式checkpointと自分で生成した結果だけを渡してください。

上流checkoutはrevisionを固定し、既存checkoutが異なるrevision／変更済みの場合は上書きせず停止します。検証したpipの全バージョンとcondaのpackage URLを `ubuntu/locks/` に同梱し、新規構築時に使用します。実際に構築した依存の一覧は `.reaction-tools/locks/` に保存します。GVHMR公式requirementsに不足する `yacs==0.1.8` も補っています。完了済み環境は再インストールせず `pip check`、未完了環境は再開します。同時setupはロックで拒否します。

上流資料：

- [GVHMR公式インストール](https://github.com/zju3dv/GVHMR/blob/main/docs/INSTALL.md)
- [GMR公式](https://github.com/YanjieZe/GMR)
- [PyTorch Blackwell対応](https://pytorch.org/blog/pytorch-2-7/)
- [PyTorch3Dビルド](https://github.com/facebookresearch/pytorch3d/blob/main/INSTALL.md)

## モデルと動画の配置

checkpointは公式Colab掲載のURLからダウンロードします。中断したファイルは `.partial` に残し、正常終了後のみ本来の名前にします。`--skip-downloads` で省略できます。

SMPL/SMPL-Xは自動取得しません。それぞれの公式サイトでライセンスに同意し、自分で取得したファイルを配置してください。

```text
~/dev/GVHMR/inputs/checkpoints/body_models/smpl/SMPL_NEUTRAL.pkl
~/dev/GVHMR/inputs/checkpoints/body_models/smplx/SMPLX_NEUTRAL.npz
~/dev/GMR/assets/body_models/smplx/SMPLX_NEUTRAL.npz
```

- [SMPL](https://smpl.is.tue.mpg.de/)
- [SMPL-X](https://smpl-x.is.tue.mpg.de/)

動画は `input/surprised_01.mp4` へ配置します。doctorは不足ファイルの具体的なパスを表示し、環境／モデル不足なら終了コード2を返します。動画がない場合は警告です。環境import成功は、動画からの推論成功とは別です。

## 単体確認（この順番で実施）

固定カメラ動画には `-s` を使います。今回の入力は人物が横向きに記録されているため、元動画を残して左90度回転した `.reaction-cache/normalized/surprised_01.mp4` を作成しました。まずこのコピーでGVHMR単体を実行します。

コピーを再作成する場合：

```bash
mkdir -p .reaction-cache/normalized
.reaction-tools/envs/gvhmr/bin/ffmpeg -i input/surprised_01.mp4 \
  -vf transpose=2 -c:v libx264 -crf 18 -an .reaction-cache/normalized/surprised_01.mp4
```

```bash
PROJECT="$HOME/dev/g1-bottle-reaction"
cd "$HOME/dev/GVHMR"
"$PROJECT/tools/reaction_generator/ubuntu/python-gvhmr" tools/demo/demo.py \
  --video "$PROJECT/.reaction-cache/normalized/surprised_01.mp4" \
  --output_root "$PROJECT/.reaction-cache/manual/gvhmr" -s
```

`$PROJECT/.reaction-cache/manual/gvhmr/surprised_01/hmr4d_results.pt` とGVHMRのプレビューを確認した後にGMRを実行します。

```bash
cd "$PROJECT"
python3 tools/reaction_generator/retarget_g1.py \
  --gvhmr-result "$PROJECT/.reaction-cache/manual/gvhmr/surprised_01/hmr4d_results.pt" \
  --gmr-root "$HOME/dev/GMR" \
  --python "$PROJECT/tools/reaction_generator/ubuntu/python-gmr" \
  --output "$PROJECT/.reaction-cache/manual/gmr_unitree_g1.npz"
```

GMRの元の可視化ツールは同じ結果を `scripts/gvhmr_to_robot.py --gvhmr_pred_file ... --robot unitree_g1` に渡して使用できます。デスクトップ表示を使う場合は `REACTION_MUJOCO_GL=glfw` をrunnerに指定してください。プレビューは実機動作の承認を意味しません。

G1環境の既存NumPy/YAML/cleanerで変換します（新しい依存の追加不要）。

```bash
cd "$PROJECT"
source scripts/activate-g1-env.sh
python tools/reaction_generator/clean_motion.py \
  .reaction-cache/manual/gmr_unitree_g1.npz \
  --name surprised --output motions/surprised.npz
```

出力は50Hz、腕14関節、`name/fps/joint_names/positions/duration/metadata`入りのnpzです。既存cleanerはsmoothing、resampling、振幅縮小、関節・速度制限を適用します。脚と腰は最終出力から除外します。

positionsは **開始姿勢からの相対角度** (`position_mode=relative_to_runtime_start`) です。絶対関節角度として実機に送ってはいけません。開始・終了blendは `config/reaction_motion.yaml` で調整できます。実機再生側が現在姿勢からの補間、絶対関節角度の制限、所有権を扱う必要があります。今回、実機再生コードは変更しません。

## 単体確認後の既存generateコマンド

保存済み `environment.local.json` から両repoと専用runnerを自動選択します。まず単体確認を完了してから使用してください。

```bash
python tools/reaction_generator/generate.py .reaction-cache/normalized/surprised_01.mp4 \
  --name surprised --static-camera --work-dir .reaction-cache/reaction_generator
```

`--static-camera` は固定カメラの場合のみ指定します。出力済みassetを上書きする場合だけ `--overwrite` を指定してください。

## 削除

```bash
bash tools/reaction_generator/teardown_ubuntu.sh
```

`DELETE` 入力後、`.reaction-tools` と `.reaction-cache` のみ削除します。生成中の処理を終了してから実行してください。GVHMR/GMRのrepo、配置モデル、入力動画、生成motion、保存設定は残ります。repo／モデルも不要になった場合はそれぞれのディレクトリを別途選んで削除してください。環境削除に巻き込みません。

## トラブルシューティング

| 症状 | 確認と対処 |
|---|---|
| CUDA unavailable | `nvidia-smi` とdoctorの実CUDA演算結果を確認。Driverを自動更新しない。専用runnerを使っているか確認。 |
| PyTorch3D import error | `setup_ubuntu.sh` のビルドログを確認。古いcu121 wheelはRTX 5060 Ti構成に使用しない。既存G1 venvへのpip installは禁止。 |
| checkpoint missing | setupを `--skip-downloads` なしで再実行。失敗時は `.partial` のみ残り、不完全ファイルを有効と扱わない。 |
| SMPL-X missing | 上記の公式サイトからライセンスに従って取得し、doctorが示すパスへ配置。 |
| MuJoCo display error | headlessは既定EGL。デスクトップviewerは `REACTION_MUJOCO_GL=glfw` で起動。Driverの再インストールは行わない。 |
| ffmpeg missing | PATHを手動変更せず `ubuntu/python-gvhmr` / `python-gmr` 経由で実行。ffmpegは専用環境に導入される。 |
| G1 environment changed | `.reaction-tools/locks/g1-before.txt` と現在のfreezeを比較。自動修復／再インストールしない。 |

作業前後のチェック：

```bash
source scripts/activate-g1-env.sh
bash scripts/check-g1-env.sh
python -m pip freeze > /tmp/g1-env-after-reaction-setup.txt
diff -u ~/g1-env-before-reaction-setup.txt /tmp/g1-env-after-reaction-setup.txt
python -m pytest
```
