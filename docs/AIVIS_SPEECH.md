# AivisSpeech integration

## Boundary

AivisSpeech runs as an independent local service. This project is only an HTTP client:

```text
Reaction(text, voice_profile)
  -> AivisSpeechBackend
  -> GET /speakers
  -> POST /audio_query?text=...&speaker=<style_id>
  -> profile-adjusted AudioQuery JSON
  -> POST /synthesis?speaker=<style_id>
  -> cached WAV file
  -> AudioOutput
  -> Windows PC speaker
```

No Aivis runtime, model, or Python dependency is installed into this project. The HTTP client uses Python's standard library.

## Setup

Install and start the official AivisSpeech application separately. The default Engine URL is:

```text
http://127.0.0.1:10101
```

The running Engine exposes Swagger at <http://127.0.0.1:10101/docs>. Check the actual installed models before selecting a style:

```powershell
python -m g1_bottle_reaction --check-aivis
python -m g1_bottle_reaction --list-aivis-speakers
```

## Speaker and style resolution

`GET /speakers` supplies speaker name, UUID, style name, and global style ID. Nothing is hard-coded. Resolution is:

1. `speech.aivis.style_id`, when configured
2. configured `speaker_name` plus `style_name`
3. configured `style_fallback_names`
4. the selected speaker's first style

A profile's optional `style_name` is looked up within that same speaker. If unavailable, it falls back to the resolved default style and logs a warning. Invalid explicitly configured speaker/style values are errors and print the available list.

The current character default is `speaker_name: "阿井田 茂"`, with `Calm -> Mid -> first available` as the default-style order. IDs remain runtime values obtained from `/speakers`; none are written into config.

## Voice profiles

Reactions keep their original fixed text and add one lightweight profile name. Initial mappings are:

- `FOUND`, `NEAR`, `LOST`: `curious`
- `TOO_CLOSE`: `surprised`
- `FOUND_AGAIN`, `MUSIC_STARTED`: `happy`
- omitted profile: `neutral`

Profiles set `style_name`, `intonationScale`, `tempoDynamicsScale`, `speedScale`, `volumeScale`, and `pitchScale`. AivisSpeech defines `intonationScale` as selected-style emotion strength and `tempoDynamicsScale` as tempo variation. Defaults deliberately stay near 1.0–1.3. `pitchScale` remains 0.0 because changing it may degrade quality.

The `curious` profile additionally sets `prePhonemeLength=0.08` for the cached short utterance `ん？`. The generated cache WAV began audible content at about 98 ms using a -40 dB threshold, so destructive waveform trimming is not applied.

For 阿井田 茂, `neutral` and `curious` select `Calm`, `happy` selects `Mid`, `surprised` selects `Surprise`, `serious` selects `Heavy`, and the future `shout` profile selects `Shout`. These profile styles also fall back to the resolved default when an installed model does not provide the requested name.

Edit `config/default.yaml`, then compare quickly:

```powershell
python -m g1_bottle_reaction --preview-voice-profiles "お、いいね" --speech-debug
```

## Cache

The cache key is a stable SHA-256 over schema version, text, speaker name/UUID, resolved style ID, and all profile parameters. WAV files are stored atomically under `.cache/tts`. Cache lookup happens before `/audio_query`, so a hit requires no synthesis request. Schema version 2 prevents WAV files generated for the previous speaker from being reused after switching to 阿井田 茂.

```powershell
python -m g1_bottle_reaction --precache-speech --speech-debug
```

This includes base Reaction text and encounter-variant text. Cache files are local generated artifacts and ignored by Git.

## Concurrency and fallback

The existing Reaction worker starts motion first, waits only the configured speech delay, then performs synthesis/playback. Camera, Vision, microphone callback, YAMNet worker, and MuJoCo animation do not run on this worker and continue independently.

Explicit `--speech aivis` fails clearly when the Engine is unavailable. `--speech auto` tries AivisSpeech, then Windows Japanese TTS, then console. Runtime Aivis failure in auto mode also delegates the same text/profile to the selected fallback.

`AudioOutput` is separate from synthesis and caching. `WindowsWaveOutput` uses the standard-library Windows WAV player. `G1AudioOutput` consumes the same cached WAV, converts it to 16 kHz mono signed PCM16 little-endian, and sends it through the official Unitree `AudioClient`; Aivis HTTP code does not move into `G1RobotAdapter`.
