# MotionDecode Reaction Engine integration

## Resident IPC path

`MotionDecodeReactionAdapter` は既定でresident workerを使います。PC2 transportでは
adapter生成時に1本のSSH stdio bridgeを起動し、そのbridgeをUnix socketへ接続したまま
再利用します。reaction trigger時に新しいPython/DDS processは起動しません。

workerがREADYでない場合は明示的に失敗し、ゲーム中に低速CLIへ自動fallbackしません。
旧CLIは診断用に `resident=False` を指定した場合だけ利用できます。Reaction Engineの既存
audio executorはmotion requestと並列に開始されます。

Validated on G1 through the explicit test-event path:

```text
ReactionEngine
  -> MotionDecodeReactionAdapter
  -> motiondecode-test named reaction CLI
  -> validated MotionDecode runtime on PC2
  -> G1
```

## Validated reaction

- name: `frustration`
- source: `EESB_Frustration_00001 [5552,5613)`
- arms: `50%`
- waist: `25%`
- legs: MotionDecode trajectory excluded
- requires stop: `true`

## Integrated real-G1 result

- Reaction Engine job: **PASS**
- named reaction execution: **PASS**
- commands sent: `744`
- maximum weight: `1.0`
- maximum tracking error: `0.072214 rad`
- maximum leg deviation: `0.017377 rad` (threshold `0.03 rad`)
- minimum collision clearance: `0.040592 m`
- fast-loop maximum interval: `0.022291 s`
- deadline miss: `0`
- joint violation: `0`
- collision violation: `0`
- ownership conflict: `false`
- duplicate execution: `none`
- q0 return: **PASS**
- release: **PASS**

The adapter accepts only allowlisted named profiles. It captures timeout and exit
status, rejects concurrent execution, and leaves trajectory generation, DDS,
prevalidation, acquire, HOLD, blending, watchdogs, and release inside the
validated `motiondecode-test` runtime.
