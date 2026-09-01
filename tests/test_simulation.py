from __future__ import annotations

import importlib

from g1_bottle_reaction.adapters.mock_robot import MockRobotAdapter
from g1_bottle_reaction.adapters.speech import ConsoleSpeechBackend
from g1_bottle_reaction.app import BottleReactionApp, run_simulation
from g1_bottle_reaction.state.events import BottleEvent


def test_simulation_smoke(app_config) -> None:
    robot = MockRobotAdapter()
    app = BottleReactionApp(app_config, robot, ConsoleSpeechBackend())
    updates = run_simulation(app)
    events = [item.event for item in updates if item.event is not None]
    assert events == [
        BottleEvent.FOUND,
        BottleEvent.NEAR,
        BottleEvent.TOO_CLOSE,
        BottleEvent.NEAR,
        BottleEvent.LOST,
        BottleEvent.FOUND_AGAIN,
    ]
    assert robot.motions == [
        "notice",
        "reach_forward",
        "guard",
        "reach_forward",
        "look_around",
        "surprise",
    ]


def test_g1_module_imports_without_unitree_sdk() -> None:
    module = importlib.import_module("g1_bottle_reaction.adapters.g1_robot")
    assert module.G1RobotAdapter is not None
