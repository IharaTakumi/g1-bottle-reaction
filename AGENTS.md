# Development guide

- Windows 11 with the mock adapter and Python 3.13 is the current primary environment.
- Keep every Unitree dependency and import isolated to `adapters/g1_robot.py`; never guess undocumented Unitree APIs.
- Real-robot behavior must remain an explicit opt-in guarded by `--enable-real-robot`.
- The state machine and reaction engine must stay independent of Unitree, YOLO, and webcam hardware.
- Run `pytest` after changes and keep `--simulate` working without a model, camera, network, or Unitree SDK.
- Put tunable thresholds, delays, and reaction content in YAML instead of hard-coding them.
- Never run audio inference in the microphone callback or vision loop; use the bounded audio worker queue.
- Keep `AudioSource` swappable and isolate unverified G1 microphone work to its placeholder/docs.
- Automated tests must not require a microphone, TensorFlow model, network, or Unitree SDK.
- Keep the sounddevice-private WASAPI RAW bridge isolated to `audio/windows_raw.py`; never change global Windows audio settings.
- Keep MuJoCo optional and isolated from `G1RobotAdapter`; preview animation is never a physical G1 trajectory.
- Do not block Vision or Audio with Viewer updates; keep MuJoCo qpos animation on its dedicated worker.
- Reaction text is deterministic; do not add an LLM or dialogue generation without an explicit request.
- Keep AivisSpeech HTTP and style logic inside SpeechBackend adapters, and keep synthesis separate from audio output.
- Tests must not require a running AivisSpeech Engine; robot motion must not wait for speech synthesis.
- Raw detector labels must not leak into stealth game rules; `cell phone` currently maps to semantic `PLAYER` and must remain swappable with `person`.
- Reaction Engine handles discrete game events; continuous target tracking stays outside it.
- Stealth simulation must remain testable without a camera or YOLO model, and no unverified real-G1 tracking API may be added.
