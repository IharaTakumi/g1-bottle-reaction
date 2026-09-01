from __future__ import annotations

from g1_bottle_reaction.audio.music_tracker import MusicState, MusicStateTracker
from g1_bottle_reaction.state.events import ReactionEvent


def start_music(tracker: MusicStateTracker) -> None:
    tracker.update(0.7, now=0.0)
    update = tracker.update(0.7, now=1.0)
    assert update.event is ReactionEvent.MUSIC_STARTED


def test_below_threshold_does_nothing(app_config) -> None:
    tracker = MusicStateTracker(app_config.audio.music_tracking)
    assert tracker.update(0.1, now=0.0).state is MusicState.QUIET
    assert tracker.update(0.49, now=2.0).event is None


def test_single_threshold_crossing_is_only_candidate(app_config) -> None:
    tracker = MusicStateTracker(app_config.audio.music_tracking)
    update = tracker.update(0.7, now=0.0)
    assert update.state is MusicState.MUSIC_CANDIDATE
    assert update.event is None
    assert tracker.update(0.1, now=0.5).state is MusicState.QUIET


def test_confirm_duration_emits_music_started_once(app_config) -> None:
    tracker = MusicStateTracker(app_config.audio.music_tracking)
    tracker.update(0.7, now=0.0)
    assert tracker.update(0.8, now=0.9).event is None
    started = tracker.update(0.8, now=1.0)
    assert started.state is MusicState.MUSIC_PLAYING
    assert started.event is ReactionEvent.MUSIC_STARTED
    assert tracker.update(0.9, now=2.0).event is None
    assert tracker.update(0.9, now=10.0).event is None


def test_playing_hysteresis_and_confirmed_stop(app_config) -> None:
    tracker = MusicStateTracker(app_config.audio.music_tracking)
    start_music(tracker)
    assert tracker.update(0.4, now=2.0).state is MusicState.MUSIC_PLAYING
    assert tracker.update(0.2, now=3.0).event is None
    assert tracker.update(0.4, now=4.0).state is MusicState.MUSIC_PLAYING
    tracker.update(0.2, now=5.0)
    stopped = tracker.update(0.1, now=7.0)
    assert stopped.event is ReactionEvent.MUSIC_STOPPED
    assert stopped.state is MusicState.COOLDOWN


def test_cooldown_blocks_then_allows_new_start(app_config) -> None:
    tracker = MusicStateTracker(app_config.audio.music_tracking)
    start_music(tracker)
    tracker.update(0.1, now=2.0)
    tracker.update(0.1, now=4.0)
    assert tracker.update(0.9, now=5.0).state is MusicState.COOLDOWN
    assert tracker.update(0.9, now=11.9).event is None
    candidate = tracker.update(0.9, now=12.0)
    assert candidate.state is MusicState.MUSIC_CANDIDATE
    restarted = tracker.update(0.9, now=13.0)
    assert restarted.event is ReactionEvent.MUSIC_STARTED

