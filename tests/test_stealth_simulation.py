from __future__ import annotations

from g1_bottle_reaction.adapters.mock_robot import MockRobotAdapter
from g1_bottle_reaction.adapters.speech import MuteSpeechBackend
from g1_bottle_reaction.app import StealthGameApp, run_stealth_simulation
from g1_bottle_reaction.stealth.models import GameEvent, GameState


def test_stealth_simulation_completes_full_round_without_camera_or_yolo(
    app_config,
) -> None:
    app = StealthGameApp(app_config, MockRobotAdapter(), MuteSpeechBackend())
    outputs = run_stealth_simulation(app, realtime_scale=0.0)
    events = [event for update in outputs for event in update.events]
    assert outputs[-1].state is GameState.GAME_OVER
    assert GameEvent.SUSPICION_STARTED in events
    assert GameEvent.RETURNED_TO_UNAWARE in events
    assert GameEvent.ALERT_STARTED in events
    assert GameEvent.PLAYER_FOUND in events
    assert events.count(GameEvent.GAME_OVER) == 1
