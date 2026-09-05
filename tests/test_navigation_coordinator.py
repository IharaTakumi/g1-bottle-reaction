from __future__ import annotations

from dataclasses import replace

import pytest

from g1_bottle_reaction.adapters.mock_navigation import MockNavigationAdapter
from g1_bottle_reaction.adapters.mock_robot import MockRobotAdapter
from g1_bottle_reaction.adapters.navigation import NavigationConnectionError
from g1_bottle_reaction.adapters.speech import MuteSpeechBackend, SpeechBackend
from g1_bottle_reaction.navigation.coordinator import NavigationCoordinator
from g1_bottle_reaction.navigation.models import NavigationState, NavigationStatus
from g1_bottle_reaction.reactions.engine import (
    ReactionCompletionError,
    ReactionEngine,
    ReactionLifecycleState,
)
from g1_bottle_reaction.state.events import ReactionEvent


def _actions(navigation: MockNavigationAdapter) -> list[str]:
    return [record.action for record in navigation.command_history]


def _coordinated_engine(
    app_config,
    navigation: MockNavigationAdapter,
    *,
    robot: MockRobotAdapter | None = None,
    speech: SpeechBackend | None = None,
    start_worker: bool = False,
) -> tuple[ReactionEngine, NavigationCoordinator]:
    coordinator = NavigationCoordinator(
        navigation,
        command_timeout_s=0.1,
        status_poll_interval_s=0.001,
    )
    engine = ReactionEngine(
        replace(app_config.reaction, cooldown_seconds=0.0),
        robot or MockRobotAdapter(),
        speech or MuteSpeechBackend(),
        sleep=lambda _: None,
        start_worker=start_worker,
        lifecycle_observer=coordinator,
        motion_completion_timeout_s=0.1,
    )
    return engine, coordinator


def test_reaction_pauses_patrol_and_resumes_only_after_completion(app_config) -> None:
    navigation = MockNavigationAdapter()
    navigation.start_patrol("outer-loop")
    engine, coordinator = _coordinated_engine(app_config, navigation)

    decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=0.0)

    assert decision.job is not None
    assert decision.job.state is ReactionLifecycleState.COMPLETED
    assert navigation.status().state is NavigationState.PATROLLING
    assert _actions(navigation) == ["start_patrol", "pause", "resume"]
    engine.close()
    coordinator.close()


def test_cooldown_rejection_does_not_touch_navigation(app_config) -> None:
    navigation = MockNavigationAdapter()
    navigation.start_patrol("outer-loop")
    coordinator = NavigationCoordinator(
        navigation,
        command_timeout_s=0.1,
        status_poll_interval_s=0.001,
    )
    engine = ReactionEngine(
        app_config.reaction,
        MockRobotAdapter(),
        MuteSpeechBackend(),
        sleep=lambda _: None,
        start_worker=False,
        lifecycle_observer=coordinator,
        motion_completion_timeout_s=0.1,
    )

    assert engine.handle(ReactionEvent.FOUND, encounter_count=1, now=0.0).accepted
    before = list(navigation.command_history)
    rejected = engine.handle(ReactionEvent.NEAR, encounter_count=1, now=0.1)

    assert not rejected.accepted
    assert rejected.job is None
    assert navigation.command_history == before
    engine.close()
    coordinator.close()


def test_operator_pause_is_not_owned_or_resumed_by_reaction(app_config) -> None:
    navigation = MockNavigationAdapter()
    navigation.start_patrol("outer-loop")
    navigation.pause("operator")
    engine, coordinator = _coordinated_engine(app_config, navigation)

    decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=0.0)

    assert decision.job is not None and decision.job.successful
    status = navigation.status()
    assert status.state is NavigationState.PAUSED
    assert status.pause_reason == "operator"
    assert _actions(navigation) == ["start_patrol", "pause"]
    engine.close()
    coordinator.close()


@pytest.mark.parametrize("terminal_state", ["disconnected", "error", "stopped"])
def test_terminal_navigation_state_never_auto_resumes(
    app_config, terminal_state: str
) -> None:
    navigation = MockNavigationAdapter()
    navigation.start_patrol("outer-loop")
    if terminal_state == "disconnected":
        navigation.disconnect()
    elif terminal_state == "error":
        navigation.inject_error()
    else:
        navigation.stop()
    engine, coordinator = _coordinated_engine(app_config, navigation)

    decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=0.0)

    assert decision.job is not None and decision.job.successful
    assert "resume" not in _actions(navigation)
    engine.close()
    coordinator.close()


class FailingSpeech(SpeechBackend):
    def speak(self, text: str, *, voice_profile: str = "neutral") -> None:
        del text, voice_profile
        raise RuntimeError("speech failed")


def test_reaction_failure_keeps_coordinator_owned_pause(app_config) -> None:
    navigation = MockNavigationAdapter()
    navigation.start_patrol("outer-loop")
    engine, coordinator = _coordinated_engine(
        app_config,
        navigation,
        speech=FailingSpeech(),
        start_worker=True,
    )

    decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=0.0)
    assert decision.job is not None and decision.job.wait(1.0)

    assert decision.job.state is ReactionLifecycleState.FAILED
    assert navigation.status().state is NavigationState.PAUSED
    assert "resume" not in _actions(navigation)
    engine.close()
    coordinator.close()


class UnconfirmedRobot(MockRobotAdapter):
    def wait_for_motion_complete(
        self, motion: str, timeout: float | None = None
    ) -> bool:
        del motion, timeout
        return False


def test_unconfirmed_motion_completion_keeps_patrol_paused(app_config) -> None:
    navigation = MockNavigationAdapter()
    navigation.start_patrol("outer-loop")
    engine, coordinator = _coordinated_engine(
        app_config,
        navigation,
        robot=UnconfirmedRobot(),
        start_worker=True,
    )

    decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=0.0)
    assert decision.job is not None and decision.job.wait(1.0)

    assert decision.job.state is ReactionLifecycleState.FAILED
    assert isinstance(decision.job.error, ReactionCompletionError)
    assert navigation.status().state is NavigationState.PAUSED
    assert "resume" not in _actions(navigation)
    engine.close()
    coordinator.close()


class NeverPausesNavigation(MockNavigationAdapter):
    def pause(self, reason: str = "operator") -> NavigationStatus:
        del reason
        return self.status()


class AdvancingClock:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += duration


def test_reaction_does_not_start_when_pause_is_not_confirmed(app_config) -> None:
    time = AdvancingClock()
    navigation = NeverPausesNavigation(clock=time.clock)
    navigation.start_patrol("outer-loop")
    robot = MockRobotAdapter()
    coordinator = NavigationCoordinator(
        navigation,
        command_timeout_s=0.01,
        status_poll_interval_s=0.005,
        clock=time.clock,
        sleep=time.sleep,
    )
    engine = ReactionEngine(
        replace(app_config.reaction, cooldown_seconds=0.0),
        robot,
        MuteSpeechBackend(),
        sleep=lambda _: None,
        start_worker=True,
        lifecycle_observer=coordinator,
        motion_completion_timeout_s=0.1,
    )

    decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=0.0)
    assert decision.job is not None and decision.job.wait(1.0)

    assert decision.job.state is ReactionLifecycleState.FAILED
    assert isinstance(decision.job.error, TimeoutError)
    assert robot.motions == []
    assert "resume" not in _actions(navigation)
    engine.close()
    coordinator.close()


def test_disconnected_pause_failure_prevents_reaction_start(app_config) -> None:
    class DisconnectingNavigation(MockNavigationAdapter):
        def pause(self, reason: str = "operator") -> NavigationStatus:
            self.disconnect("link lost while pausing")
            raise NavigationConnectionError("link lost while pausing")

    navigation = DisconnectingNavigation()
    navigation.start_patrol("outer-loop")
    robot = MockRobotAdapter()
    engine, coordinator = _coordinated_engine(
        app_config,
        navigation,
        robot=robot,
        start_worker=True,
    )

    decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=0.0)
    assert decision.job is not None and decision.job.wait(1.0)

    assert decision.job.state is ReactionLifecycleState.FAILED
    assert robot.motions == []
    assert navigation.status().state is NavigationState.DISCONNECTED
    engine.close()
    coordinator.close()
