# Smartphone Stealth Game

## Boundary

The phone is a temporary proxy for the human PLAYER. `YoloTargetDetector` emits raw detections for the configured `detector_label`; `TargetPerception` selects one candidate and maps it to `TargetObservation(semantic_role=PLAYER)`. Raw `cell phone` checks do not exist in `StealthGameEngine`. A future switch to `person` changes configuration and perception input only.

When several candidates exist, selection favors apparent bbox area, confidence, and horizontal centrality. The selected observation contains visibility, confidence, pixel bbox, normalized center coordinates (`-1` left, `0` center, `+1` right), bbox area ratio, timestamp, raw label, and semantic role.

## Suspicion and states

The game confirms a detection for `detection_confirm_seconds` and holds it through short misses for `lost_grace_seconds`. Confirmed visible exposure uses:

```text
visibility = weighted_mean(confidence, normalized bbox area, horizontal centrality)
suspicion += gain_per_second * visibility * dt
suspicion -= decay_per_second * dt     # after confirmed loss
```

Values clamp to 0–100. Thresholds create `UNAWARE`, `SUSPICIOUS`, `ALERT`, and `FOUND`; FOUND emits `PLAYER_FOUND` once. After `game_over_delay_seconds`, the state becomes `GAME_OVER` and emits once. Falling below the suspicious threshold emits `RETURNED_TO_UNAWARE`. All durations and weights live under `stealth_game` in `config/default.yaml`.

The initial suspicious threshold is 10 rather than the illustrative 25 so a roughly 0.4–0.5 second edge exposure can produce the intended notice after the 0.2 second confirmation debounce. Alert and found remain 65 and 100. Raise the first threshold toward 25 if a camera setup is too sensitive.

## Reactions and tracking

Game rules never call speech or motion. State-change events enter the existing Reaction Engine queue. The fixed lines never mention a phone: `……ん？`, `誰かいるな`, `気のせいか……`, and `そこだ！`. Game transition reactions bypass the normal cooldown deliberately; `PLAYER_FOUND` has the highest priority. Music reactions are accepted only while the game is UNAWARE.

Continuous tracking is separate. `TargetTrackingController` maps horizontal position through a configurable deadzone and optional X inversion to a bounded yaw, then emits a lightweight `TrackingCommand`. It uses `alpha = 1 - exp(-response * dt)`, so the response does not depend on camera FPS. SUSPICIOUS tracks slowly, ALERT faster, FOUND locks the last target, and GAME_OVER ignores subsequent target movement. Missing input holds the last direction for `lost_hold_seconds`, then recenters at its own response rate.

MuJoCo composes `base motion pose + tracking offset` on the official model's existing `waist_yaw_joint` and clamps the result to its loaded range. The tested 29-DoF model exposes `(-2.618, +2.618)` radians; normal tracking is additionally bounded to ±25 degrees. A missing tracking joint is a safe no-op. `R` clears desired yaw, last-seen state, and FOUND lock, then recenters smoothly.

The Windows webcam does not rotate with the virtual robot. This is a tracking-behaviour preview, not a closed-loop camera simulation. No walking, IK, navigation, or unverified Unitree command is used. A real-G1 implementation must replace only the attention output after the appropriate official API and safety envelope are confirmed.

Use `--tracking-debug` to show the image center line, PLAYER bbox center, desired/actual/error yaw, tracking status, and last-seen age. If the visual left/right convention is reversed on a specific setup, set `invert_x: true` rather than changing controller code.

## Commands

```powershell
python -m g1_bottle_reaction --robot mujoco --preview-tracking
python -m g1_bottle_reaction --simulate-stealth --robot mujoco --speech mute
python -m g1_bottle_reaction --game stealth-phone --robot mock --camera 0 --speech console
python -m g1_bottle_reaction --game stealth-phone --robot mujoco --camera 0 --speech aivis --tracking-debug
```
