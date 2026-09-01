from __future__ import annotations

import builtins
import importlib

import numpy as np

from g1_bottle_reaction.adapters.mock_robot import MockRobotAdapter
from g1_bottle_reaction.adapters.speech import ConsoleSpeechBackend
from g1_bottle_reaction.app import BottleReactionApp, run_audio_simulation
from g1_bottle_reaction.audio.classifiers import FakeAudioClassifier
from g1_bottle_reaction.audio.models import AudioChunk
from g1_bottle_reaction.audio.pipeline import AudioProcessor
from g1_bottle_reaction.state.events import ReactionEvent


def test_fake_classifier_and_processor_require_no_tensorflow(app_config, monkeypatch) -> None:
    original_import = builtins.__import__

    def reject_tensorflow(name, *args, **kwargs):
        if name.startswith("tensorflow"):
            raise AssertionError("TensorFlow must not be imported by the fake classifier")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_tensorflow)
    processor = AudioProcessor(app_config.audio, FakeAudioClassifier([0.7]))
    update = processor.process_chunk(
        AudioChunk(np.zeros(16000, dtype=np.float32), 16000), now=0.0
    )
    assert update is not None
    assert update.music_score == 0.7


def test_audio_simulation_reaches_shared_reaction_engine(app_config) -> None:
    robot = MockRobotAdapter()
    app = BottleReactionApp(app_config, robot, ConsoleSpeechBackend())
    updates = run_audio_simulation(app)
    events = [update.event for update in updates if update.event is not None]
    assert events == [
        ReactionEvent.MUSIC_STARTED,
        ReactionEvent.MUSIC_STOPPED,
        ReactionEvent.MUSIC_STARTED,
    ]
    assert robot.motions == ["little_dance", "little_dance"]


def test_g1_mic_placeholder_imports_without_unitree_sdk() -> None:
    module = importlib.import_module("g1_bottle_reaction.audio.sources")
    source = module.G1MicSource()
    try:
        source.start(lambda chunk: None)
    except RuntimeError as exc:
        assert "not implemented" in str(exc)
    else:
        raise AssertionError("placeholder must not pretend to capture audio")
