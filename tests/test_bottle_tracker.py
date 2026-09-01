from __future__ import annotations

from g1_bottle_reaction.state.bottle_tracker import BottleTracker
from g1_bottle_reaction.state.events import BottleEvent, BottleState


def detect(tracker: BottleTracker, now: float, ratio: float = 0.02):
    return tracker.update(
        detected=True, confidence=0.9, proximity_ratio=ratio, now=now
    )


def missing(tracker: BottleTracker, now: float):
    return tracker.update(detected=False, now=now)


def confirm_far(tracker: BottleTracker, start: float = 0.0):
    detect(tracker, start)
    return detect(tracker, start + 0.31)


def test_detection_debounce(app_config) -> None:
    tracker = BottleTracker(app_config.tracking)
    assert detect(tracker, 0.0).state is BottleState.NO_BOTTLE
    assert detect(tracker, 0.29).event is None
    update = detect(tracker, 0.31)
    assert update.state is BottleState.FAR
    assert update.event is BottleEvent.FOUND
    assert update.encounter_count == 1


def test_short_miss_does_not_lose_bottle(app_config) -> None:
    tracker = BottleTracker(app_config.tracking)
    confirm_far(tracker)
    assert missing(tracker, 1.0).event is None
    assert missing(tracker, 1.99).state is BottleState.FAR
    assert detect(tracker, 2.0).state is BottleState.FAR


def test_lost_and_found_again(app_config) -> None:
    tracker = BottleTracker(app_config.tracking)
    confirm_far(tracker)
    missing(tracker, 1.0)
    lost = missing(tracker, 2.01)
    assert lost.state is BottleState.LOST
    assert lost.event is BottleEvent.LOST

    detect(tracker, 3.0)
    found_again = detect(tracker, 3.31)
    assert found_again.event is BottleEvent.FOUND_AGAIN
    assert found_again.encounter_count == 2


def test_return_outside_found_again_window_is_found(app_config) -> None:
    tracker = BottleTracker(app_config.tracking)
    confirm_far(tracker)
    missing(tracker, 1.0)
    missing(tracker, 2.01)
    detect(tracker, 11.0)
    returned = detect(tracker, 11.31)
    assert returned.event is BottleEvent.FOUND


def test_proximity_hysteresis(app_config) -> None:
    tracker = BottleTracker(app_config.tracking)
    confirm_far(tracker)
    assert detect(tracker, 1.0, 0.049).state is BottleState.FAR
    near = detect(tracker, 1.1, 0.05)
    assert near.state is BottleState.NEAR
    assert near.event is BottleEvent.NEAR
    assert detect(tracker, 1.2, 0.049).state is BottleState.NEAR
    assert detect(tracker, 1.3, 0.039).state is BottleState.FAR

    too_close = detect(tracker, 1.4, 0.15)
    assert too_close.state is BottleState.TOO_CLOSE
    assert too_close.event is BottleEvent.TOO_CLOSE
    assert detect(tracker, 1.5, 0.13).state is BottleState.TOO_CLOSE
    backing_off = detect(tracker, 1.6, 0.119)
    assert backing_off.state is BottleState.NEAR
    assert backing_off.event is BottleEvent.NEAR


def test_events_are_only_emitted_on_transitions(app_config) -> None:
    tracker = BottleTracker(app_config.tracking)
    confirm_far(tracker)
    assert detect(tracker, 1.0, 0.06).event is BottleEvent.NEAR
    assert detect(tracker, 1.1, 0.07).event is None
    assert detect(tracker, 1.2, 0.08).event is None

