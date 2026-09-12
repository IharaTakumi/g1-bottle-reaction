# G1 saved PCD inspection — 2026-09-12

## Path interpretation

Returned path:
`/home/unitree/g1_20260912T120249Z_5ace6ff3c9844d63b66c9805e76ebc08.pcd`

Likely filesystem host: **192.168.123.161のSLAM service namespace**。
serviceがcontainerまたは別root/mount内にある場合、`.161` hostから見たpathとの対応は未確認。

Evidence:

- 保存済み`save.attempt.json`、`save.result.json`、`results.json`でpathが一致する。
- Unitree公式exampleの`endMappingPlFun`は1802へ
  `{"data":{"address":"/home/unitree/test.pcd"}}`を渡し、`address`をPCD save addressと説明する。
- 実行構成はPC2 `.164`上のRPC clientからDDS経由で`.161`の`slam_operate` serviceを呼ぶ。
  既存のDDS/packet観測でrequest subscriberとresponse publisherは`.161`に対応している。
- session実装は`runtime_host=.164`と`pcd_host=.161`を別々に保存する。
- DesktopとPC2 `.164`の同一pathはどちらも存在しない。PC2の`/home/unitree`はローカルext4で、
  `.161` filesystemのmountではない。

以上から、1802の絶対pathはclient/Desktop pathではなく、SLAM service側で解釈されるpathと判断する。

## Desktop check

- exists: **false**
- size: unavailable
- result: `/home/unitree`自体がDesktopに存在しない。server側pathという構成と整合する。

## Server-side check

Method:

- 既存SSH経路でPC2 `.164`上の対象pathを`lstat`し、`findmnt`でmountを確認。
- `.161`へのpingとTCP/22到達性、既存SSH host設定を読み取り確認。
- 新規SSH設定、host key登録、迂回接続、service/process操作は行っていない。

Result:

- PC2 `.164`: 対象pathなし。`.161` filesystem mountなし。
- `.161`: ping応答あり、TCP/22は到達不可。既存のread-only shell/consoleなし。
- **server-side PCD existence not yet verifiable**。

## PCD metadata

- exists: **UNKNOWN**
- size: **UNKNOWN**
- mtime: **UNKNOWN**
- owner: **UNKNOWN**
- permissions: **UNKNOWN**
- symlink: **UNKNOWN**

## PCD header

- VERSION: **UNKNOWN**
- FIELDS: **UNKNOWN**
- SIZE: **UNKNOWN**
- TYPE: **UNKNOWN**
- COUNT: **UNKNOWN**
- WIDTH: **UNKNOWN**
- HEIGHT: **UNKNOWN**
- VIEWPOINT: **UNKNOWN**
- POINTS: **UNKNOWN**
- DATA: **UNKNOWN**

## Classification

**PCD-C**

DesktopとRPC実行host `.164`には存在しないが、server側pathであることは仕様・実装・DDS提供hostの
観測と整合する。`.161` filesystemを読む既存経路がないため、1802 successだけからpointを含む
map生成成功とは判定しない。

## Interpretation

- Does this suggest the internal LiDAR/map pipeline is working? **UNKNOWN**
- Does `mapping/points=0` currently block the next manual Mapping test? **UNKNOWN**

1802 successはserverが要求を受理した証拠だが、PCDのsize、POINTS、DATAを確認できていない。
PCD-A/PCD-Bのどちらかが決まるまで、silentな外向けDDS publicationと内部map生成を切り分けられない。

## Recommended next action

Unitreeが認める既存のread-only管理consoleまたは`.161`管理者経路で、この正確なpathだけを
`lstat/stat`し、PCD headerを`DATA`行まで読む。

## Modified files

- `docs/G1_SAVED_PCD_INSPECTION_2026-09-12.md`
- `.runtime/static-slam-mapping-20260912/desktop-pcd-check.txt`
- `.runtime/static-slam-mapping-20260912/pc2-pcd-check.txt`

## Commands sent to G1

- RPC: **NONE**
- Navigation: **NONE**
- Motion: **NONE**
- Joint: **NONE**
- LiDAR switch: **NONE**
