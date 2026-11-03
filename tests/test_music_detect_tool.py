from __future__ import annotations

import io
import importlib.util
from pathlib import Path

import numpy as np

from g1_bottle_reaction.audio.classifiers import FakeAudioClassifier
from g1_bottle_reaction.audio.pipeline import AudioProcessor
from g1_bottle_reaction.config.loader import load_config

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("g1_music_detect", ROOT / "tools/g1_music_detect.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class SteppingClock:
    def __init__(self, step):
        self.now, self.step = 0., step

    def __call__(self):
        self.now += self.step
        return self.now


def run(scores, packets=200):
    audio = load_config(ROOT / "config/default.yaml").audio
    pcm = np.zeros(tool.PACKET_BYTES // 2, dtype="<i2").tobytes() * packets
    processor = AudioProcessor(audio, FakeAudioClassifier(scores))
    return tool.detect(io.BytesIO(pcm), processor, duration=1e9, clock=SteppingClock(.08))


def test_sustained_music_reports_a_started_event():
    summary = run([.9] * 100)
    assert [e["event"] for e in summary["events"]][:1] == ["MUSIC_STARTED"]
    assert summary["music_max"] == .9 and summary["over_start_threshold"] == summary["inferences"]


def test_noise_never_reports_music():
    summary = run([.1] * 100)
    assert summary["events"] == [] and summary["over_start_threshold"] == 0


def test_short_music_blip_does_not_confirm():
    summary = run([.9] + [.1] * 99)
    assert summary["events"] == []


def test_relay_is_read_only():
    command = " ".join(tool.mic_relay_command("unitree@g1", None))
    for forbidden in ("rt/", "arm_sdk", "LocoClient", "sendto"):
        assert forbidden not in command


def test_recording_keeps_what_the_microphone_heard(tmp_path):
    import wave
    audio = load_config(ROOT / "config/default.yaml").audio
    pcm = (np.arange(tool.PACKET_BYTES // 2) % 100).astype("<i2").tobytes() * 3
    path = tmp_path / "heard.wav"
    with wave.open(str(path), "wb") as recording:
        recording.setnchannels(1); recording.setsampwidth(2); recording.setframerate(tool.SAMPLE_RATE)
        tool.detect(io.BytesIO(pcm), AudioProcessor(audio, FakeAudioClassifier([.1] * 9)),
                    duration=1e9, recording=recording, clock=SteppingClock(.08))
    with wave.open(str(path), "rb") as heard:
        assert heard.readframes(heard.getnframes()) == pcm
