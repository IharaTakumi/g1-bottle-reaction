from __future__ import annotations

from dataclasses import replace

from g1_bottle_reaction.adapters.mock_robot import MockRobotAdapter
from g1_bottle_reaction.adapters.speech import MuteSpeechBackend
from g1_bottle_reaction.reactions.engine import (
    ReactionEngine,
    ReactionJob,
    ReactionLifecycleState,
)
from g1_bottle_reaction.state.events import ReactionEvent


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def on_accepted(self, job: ReactionJob) -> None:
        self.events.append(("accepted", job.id))

    def before_start(self, job: ReactionJob) -> None:
        self.events.append(("before_start", job.id))

    def on_started(self, job: ReactionJob) -> None:
        self.events.append(("started", job.id))

    def on_completed(self, job: ReactionJob) -> None:
        self.events.append(("completed", job.id))

    def on_failed(self, job: ReactionJob, error: Exception) -> None:
        del error
        self.events.append(("failed", job.id))


def test_reaction_job_exposes_accepted_started_completed_lifecycle(app_config) -> None:
    observer = RecordingObserver()
    engine = ReactionEngine(
        replace(app_config.reaction, cooldown_seconds=0.0),
        MockRobotAdapter(),
        MuteSpeechBackend(),
        sleep=lambda _: None,
        start_worker=False,
        lifecycle_observer=observer,
        motion_completion_timeout_s=0.1,
    )

    decision = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=0.0)

    assert decision.job is not None
    assert decision.job.state is ReactionLifecycleState.COMPLETED
    assert [event for event, _ in observer.events] == [
        "accepted",
        "before_start",
        "started",
        "completed",
    ]
    assert {job_id for _, job_id in observer.events} == {decision.job.id}
    engine.close()


class FailsOnceRobot(MockRobotAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def play_motion(self, motion: str) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("first motion failed")
        super().play_motion(motion)


def test_worker_survives_one_failed_reaction_and_runs_the_next(app_config) -> None:
    observer = RecordingObserver()
    robot = FailsOnceRobot()
    engine = ReactionEngine(
        replace(app_config.reaction, cooldown_seconds=0.0),
        robot,
        MuteSpeechBackend(),
        sleep=lambda _: None,
        start_worker=True,
        lifecycle_observer=observer,
        motion_completion_timeout_s=0.1,
    )

    failed = engine.handle(ReactionEvent.FOUND, encounter_count=1, now=0.0)
    completed = engine.handle(ReactionEvent.NEAR, encounter_count=1, now=1.0)
    assert failed.job is not None and failed.job.wait(1.0)
    assert completed.job is not None and completed.job.wait(1.0)

    assert failed.job.state is ReactionLifecycleState.FAILED
    assert completed.job.state is ReactionLifecycleState.COMPLETED
    assert robot.motions == ["reach_forward"]
    assert [event for event, _ in observer.events].count("failed") == 1
    assert [event for event, _ in observer.events].count("completed") == 1
    engine.close()
