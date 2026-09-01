from __future__ import annotations

from dataclasses import replace

import pytest

from g1_bottle_reaction.stealth.engine import (
    StealthGameEngine,
    calculate_visibility_score,
)
from g1_bottle_reaction.stealth.models import (
    GameEvent,
    GameState,
    TargetObservation,
    TargetRole,
)
from g1_bottle_reaction.stealth.target_perception import RawDetection, TargetPerception


class FakeDetector:
    def detect_all(self, frame):
        return ()


def observation(
    now: float,
    *,
    visible: bool = True,
    confidence: float = 1.0,
    center_x: float = 0.0,
    area: float = 0.1,
    label: str = "cell phone",
) -> TargetObservation:
    if not visible:
        return TargetObservation.missing(
            timestamp=now,
            raw_detector_label=label,
        )
    return TargetObservation(
        visible=True,
        confidence=confidence,
        bbox=(0, 0, 10, 10),
        center_x_normalized=center_x,
        center_y_normalized=0.0,
        bbox_area_ratio=area,
        timestamp=now,
        raw_detector_label=label,
        semantic_role=TargetRole.PLAYER,
    )


def immediate_config(app_config):
    target = replace(
        app_config.stealth_game.target,
        detection_confirm_seconds=0.0,
        lost_grace_seconds=0.0,
    )
    suspicion = replace(
        app_config.stealth_game.suspicion,
        suspicious_threshold=25.0,
        alert_threshold=65.0,
        found_threshold=100.0,
        gain_per_second=50.0,
        decay_per_second=20.0,
    )
    return replace(app_config.stealth_game, target=target, suspicion=suspicion)


def test_target_mapping_cell_phone_to_player_and_selects_best_candidate() -> None:
    perception = TargetPerception(
        FakeDetector(), detector_label="cell phone", semantic_role="PLAYER"
    )
    mapped = perception.map_detections(
        (
            RawDetection("cell phone", 0.60, (0, 0, 10, 10)),
            RawDetection("cell phone", 0.95, (35, 20, 65, 80)),
        ),
        frame_width=100,
        frame_height=100,
        timestamp=1.0,
    )
    assert mapped.semantic_role is TargetRole.PLAYER
    assert mapped.raw_detector_label == "cell phone"
    assert mapped.confidence == pytest.approx(0.95)
    assert mapped.center_x_normalized == pytest.approx(0.0)


def test_future_person_label_swap_does_not_change_game_contract() -> None:
    perception = TargetPerception(
        FakeDetector(), detector_label="person", semantic_role="PLAYER"
    )
    mapped = perception.map_detections(
        (RawDetection("person", 0.9, (10, 10, 60, 90)),),
        frame_width=100,
        frame_height=100,
        timestamp=1.0,
    )
    assert mapped.raw_detector_label == "person"
    assert mapped.semantic_role is TargetRole.PLAYER


def test_game_engine_behavior_is_unchanged_when_detector_label_becomes_person(
    app_config,
) -> None:
    phone_config = immediate_config(app_config)
    person_config = replace(
        phone_config,
        target=replace(phone_config.target, detector_label="person"),
    )
    phone = StealthGameEngine(phone_config)
    person = StealthGameEngine(person_config)
    phone.update(observation(0.0, label="cell phone"), now=0.0)
    person.update(observation(0.0, label="person"), now=0.0)
    phone_update = phone.update(observation(0.5, label="cell phone"), now=0.5)
    person_update = person.update(observation(0.5, label="person"), now=0.5)
    assert phone_update.state is person_update.state
    assert phone_update.suspicion == person_update.suspicion
    assert phone_update.events == person_update.events


def test_visibility_score_rewards_large_central_target(app_config) -> None:
    config = app_config.stealth_game.visibility_score
    hard = calculate_visibility_score(
        observation(0.0, confidence=0.95, center_x=0.0, area=0.1), config
    )
    easy_to_miss = calculate_visibility_score(
        observation(0.0, confidence=0.5, center_x=0.9, area=0.005), config
    )
    assert 0 <= easy_to_miss < hard <= 1


def test_time_based_state_transitions_game_over_once_and_clamp(app_config) -> None:
    engine = StealthGameEngine(immediate_config(app_config))
    engine.update(observation(0.0), now=0.0)
    suspicious = engine.update(observation(0.5), now=0.5)
    alert = engine.update(observation(1.3), now=1.3)
    found = engine.update(observation(2.0), now=2.0)
    game_over = engine.update(observation(2.8), now=2.8)
    repeated = engine.update(observation(3.0), now=3.0)

    assert suspicious.state is GameState.SUSPICIOUS
    assert suspicious.events == (GameEvent.SUSPICION_STARTED,)
    assert alert.state is GameState.ALERT
    assert GameEvent.ALERT_STARTED in alert.events
    assert found.state is GameState.FOUND
    assert found.suspicion == 100.0
    assert found.events == (GameEvent.PLAYER_FOUND,)
    assert game_over.state is GameState.GAME_OVER
    assert game_over.events == (GameEvent.GAME_OVER,)
    assert repeated.events == ()


def test_missing_target_decays_alert_to_suspicious_then_unaware(app_config) -> None:
    engine = StealthGameEngine(immediate_config(app_config))
    engine.update(observation(0.0), now=0.0)
    engine.update(observation(1.3), now=1.3)
    lost = engine.update(observation(1.4, visible=False), now=1.4)
    suspicious = engine.update(observation(3.2, visible=False), now=3.2)
    unaware = engine.update(observation(4.6, visible=False), now=4.6)

    assert GameEvent.PLAYER_LOST in lost.events
    assert suspicious.state is GameState.SUSPICIOUS
    assert unaware.state is GameState.UNAWARE
    assert unaware.suspicion == pytest.approx(0.0)
    assert unaware.events == (GameEvent.RETURNED_TO_UNAWARE,)


def test_detection_confirm_and_lost_grace_debounce(app_config) -> None:
    engine = StealthGameEngine(app_config.stealth_game)
    assert not engine.update(observation(0.0), now=0.0).target_visible
    assert not engine.update(observation(0.1), now=0.1).target_visible
    assert engine.update(observation(0.2), now=0.2).target_visible
    within_grace = engine.update(observation(0.3, visible=False), now=0.3)
    lost = engine.update(observation(0.7, visible=False), now=0.7)
    repeated = engine.update(observation(0.8, visible=False), now=0.8)
    assert within_grace.target_visible
    assert GameEvent.PLAYER_LOST in lost.events
    assert GameEvent.PLAYER_LOST not in repeated.events


def test_reset_restores_clean_round(app_config) -> None:
    engine = StealthGameEngine(immediate_config(app_config))
    engine.update(observation(0.0), now=0.0)
    engine.update(observation(2.0), now=2.0)
    assert engine.suspicion > 0
    engine.reset(now=3.0)
    update = engine.update(observation(3.1, visible=False), now=3.1)
    assert update.state is GameState.UNAWARE
    assert update.suspicion == 0.0
    assert not update.target_visible
    assert update.events == ()
