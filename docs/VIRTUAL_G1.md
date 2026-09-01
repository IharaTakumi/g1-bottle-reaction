# Virtual G1 reaction viewer

## Purpose

This viewer previews reaction timing and character expression on a G1-shaped model. It does not simulate a controller or validate motion safety. `custom_notice` is also available as a preview, but its real-G1 implementation uses separate current-pose-relative data. The path is:

```text
Bottle / Audio event
  -> shared Reaction Engine
  -> abstract motion name
  -> MujocoRobotAdapter
  -> keyframe interpolation worker
  -> qpos + mj_forward + standard MuJoCo Viewer
```

Speech continues through the existing Windows `SpeechBackend`. MuJoCo contains no speech, Unitree SDK, DDS, LowCmd, LowState, RL policy, gait, or balance code.

## Official model

The default model is Unitree's official 29-DoF G1 MJCF:

```text
models/unitree_mujoco/unitree_robots/g1/scene_29dof.xml
```

It comes from <https://github.com/unitreerobotics/unitree_mujoco>, whose root license is BSD-3-Clause. The project does not vendor the model or meshes. Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_g1_model.ps1
```

The script makes a shallow sparse checkout of only `unitree_robots/g1` plus the repository root files, prints the exact revision, and leaves Unitree's `LICENSE` beside the checkout. To use another verified official checkout:

```powershell
python -m g1_bottle_reaction --robot mujoco --mujoco-model C:\path\to\unitree_mujoco\unitree_robots\g1\scene_29dof.xml --preview-motion stand
```

## Install and smoke test

```powershell
python -m pip install -e ".[sim]"
python -c "import mujoco; import mujoco.viewer; print(mujoco.__version__)"
python -m g1_bottle_reaction.simulation.smoke_test
```

The smoke test does not open a GUI. It loads the official model and runs `stand -> little_dance -> stand` kinematically.

## Preview commands

```powershell
python -m g1_bottle_reaction --list-motions
python -m g1_bottle_reaction --robot mujoco --preview-motion little_dance
python -m g1_bottle_reaction --robot mujoco --preview-motion notice
python -m g1_bottle_reaction --robot mujoco --preview-motion guard
```

The standard MuJoCo Viewer supports mouse orbit, pan, and zoom. A preview returns to `stand`, holds briefly, and exits. The console prints `[MOTION START]` and `[MOTION END]`.

## Motion editing

Edit `config/mujoco_motions.yaml`. Angles are radians and each motion contains time-ordered keyframes:

```yaml
little_dance:
  keyframes:
    - time: 0.0
      pose: {}
    - time: 0.5
      pose:
        right_shoulder_pitch_joint: -1.25
```

The names come from Unitree's official `g1_29dof.xml`; no joints are invented. Current preview motions are `stand`, `notice`, `custom_notice`, `wave_hand`, `reach_forward`, `guard`, `look_around`, `surprise`, `little_dance`, and `spot_target`.

Each omitted joint eases toward its `stand` value. Keyframes use smoothstep ease-in/out at the configured 60 fps. At model load, joint names are resolved and official `jnt_range` values are read. Unknown joints produce a warning and are ignored. Out-of-range angles produce a warning and are clamped.

## Concurrency

`play_motion()` only validates and queues a name. The MuJoCo worker owns animation time, `qpos`, `mj_forward()`, and Viewer synchronization. This preserves the existing reaction timing: motion starts immediately, while speech is dispatched after the reaction's configured delay. Vision capture, YOLO, microphone callback, audio worker, and YAMNet do not wait for animation completion.

## Limitations

- The base remains at the official MJCF default pose and no physics steps are taken.
- Motions emphasize the upper body and are designed only for visual reaction review.
- Viewer animation does not prove dynamic balance, torque limits, collision safety, stability, or safe physical trajectories.
- Do not send these joint sequences to a real G1. Future `G1RobotAdapter` mappings must use verified Unitree actions or hardware-tested safe sequences.
- If the upstream official model changes joint names, the loader warns and skips unmatched config entries instead of crashing the application.
