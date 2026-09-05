from __future__ import annotations

import json

from g1_bottle_reaction.adapters.mock_navigation import MockNavigationAdapter
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


def test_stealth_simulation_can_patrol_pause_react_and_resume_on_windows(
    app_config,
) -> None:
    navigation = MockNavigationAdapter()
    app = StealthGameApp(
        app_config,
        MockRobotAdapter(),
        MuteSpeechBackend(),
        navigation,
    )
    app.start_patrol("outer-loop")

    run_stealth_simulation(app, realtime_scale=0.0)

    actions = [record.action for record in navigation.command_history]
    assert actions[0] == "start_patrol"
    assert "pause" in actions
    assert "resume" in actions
    assert actions[-1] == "stop"
    pause_index = actions.index("pause")
    assert pause_index < actions.index("resume", pause_index)

    records = [
        json.loads(line)
        for line in app_config.event_log.read_text(encoding="utf-8").splitlines()
    ]
    navigation_events = [
        record["event"] for record in records if record["source"] == "navigation"
    ]
    assert "reaction_paused" in navigation_events
    assert any(
        record["event"] == "reaction_completed" and record["auto_resume"]
        for record in records
        if record["source"] == "navigation"
    )
