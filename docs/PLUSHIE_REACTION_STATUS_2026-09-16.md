# Plushie reaction status — 2026-09-16

## Camera

- ssh-jpeg PASS
- camera checkpoint `383866d` pushed
- approximately 5.2 FPS

## Plushie

- YOLO detection PASS
- reaction-target plushie filtering PASS
- person visual detection does not trigger reaction
- mock A/B/C PASS
- fresh-observation-based rearm implemented

## Reaction

- detection → reaction previously measured 372 ms
- motion/audio parallel start difference 0.23 ms
- quiet audio PASS at -24 dB
- validated motion: `motiondecode:surprise`

## Safety

- independent LowState PASS
- ~978–999 Hz
- latest age ~100 ms
- eth0 / DDS domain 0
- ownership safe
- external writers 0
- weight 0

## Current blocker

- resident MotionDecode worker restart fails before READY
- `ModuleNotFoundError: mujoco`
- resident worker currently STOPPED
- no additional real motion tests performed after failure
- do NOT bypass LowState or safety gates

## Tests

- 488 passed, 1 skipped
- simulation PASS
- git diff --check PASS

## Next

1. fix resident worker Python/runtime dependency so mujoco is available in the correct existing environment
2. restore stable READY for >=10 sec without motion
3. rerun Real Plushie Test A only
4. if PASS, run B/C
5. then consider separate PERSON reaction
