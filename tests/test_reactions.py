from __future__ import annotations

from dataclasses import dataclass, field

from g1_bottle_reaction.adapters.mock_robot import MockRobotAdapter
from g1_bottle_reaction.adapters.speech import SpeechBackend
from g1_bottle_reaction.reactions.engine import ReactionEngine
from g1_bottle_reaction.state.events import BottleEvent, ReactionEvent


@dataclass
class RecordingSpeech(SpeechBackend):
    messages: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)

    def speak(self, text: str, *, voice_profile: str = "neutral") -> None:
        self.messages.append(text)
        self.profiles.append(voice_profile)


def test_event_mapping_mock_adapter_and_timing(app_config) -> None:
    robot = MockRobotAdapter()
    speech = RecordingSpeech()
    delays: list[float] = []
    engine = ReactionEngine(
        app_config.reaction,
        robot,
        speech,
        sleep=delays.append,
        start_worker=False,
    )
    result = engine.handle(BottleEvent.FOUND, encounter_count=1, now=0.0)
    assert result.accepted
    assert robot.motions == ["notice"]
    assert speech.messages == ["ん？それ……ペットボトル？"]
    assert speech.profiles == ["curious"]
    assert delays == [0.3]


def test_reaction_cooldown(app_config) -> None:
    robot = MockRobotAdapter()
    speech = RecordingSpeech()
    engine = ReactionEngine(
        app_config.reaction,
        robot,
        speech,
        sleep=lambda _: None,
        start_worker=False,
    )
    assert engine.handle(BottleEvent.FOUND, encounter_count=1, now=1.0).accepted
    assert not engine.handle(BottleEvent.NEAR, encounter_count=1, now=2.0).accepted
    assert engine.handle(BottleEvent.NEAR, encounter_count=1, now=3.0).accepted
    assert robot.motions == ["notice", "reach_forward"]


def test_encounter_variant(app_config) -> None:
    engine = ReactionEngine(
        app_config.reaction,
        MockRobotAdapter(),
        RecordingSpeech(),
        start_worker=False,
    )
    assert engine.resolve(BottleEvent.FOUND_AGAIN, 2).speech == "いた！"
    assert engine.resolve(BottleEvent.FOUND_AGAIN, 3).speech == "またそれかよ"


def test_music_started_mapping(app_config) -> None:
    robot = MockRobotAdapter()
    speech = RecordingSpeech()
    engine = ReactionEngine(
        app_config.reaction,
        robot,
        speech,
        sleep=lambda _: None,
        start_worker=False,
    )
    result = engine.handle(
        ReactionEvent.MUSIC_STARTED, encounter_count=1, now=0.0
    )
    assert result.accepted
    assert robot.motions == ["little_dance"]
    assert speech.messages == ["お、いいね"]
    assert speech.profiles == ["happy"]


def test_stealth_reaction_mapping_and_priority(app_config) -> None:
    robot = MockRobotAdapter()
    speech = RecordingSpeech()
    engine = ReactionEngine(
        app_config.reaction,
        robot,
        speech,
        sleep=lambda _: None,
        start_worker=False,
    )
    suspicion = engine.handle(
        ReactionEvent.SUSPICION_STARTED, encounter_count=1, now=0.0
    )
    found = engine.handle(ReactionEvent.PLAYER_FOUND, encounter_count=1, now=0.1)
    assert suspicion.accepted
    assert found.accepted  # higher-priority game event bypasses normal cooldown
    assert robot.motions == ["notice", "spot_target"]
    assert speech.messages == ["ん？", "そこだ！"]
    assert speech.profiles == ["curious", "shout"]
