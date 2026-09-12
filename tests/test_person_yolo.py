from __future__ import annotations

import time
import threading

import numpy as np
import pytest

from g1_bottle_reaction.game_vision.app import build_parser, main
from g1_bottle_reaction.game_vision.dual import FrameState, compose, validate_args
from g1_bottle_reaction.game_vision.person_yolo import (
    Banana, Detection, Person, PersonWorker, TransitionLogger, filter_bananas,
    filter_people, load_banana_confidence, visible_detection,
)


def test_person_and_confidence_filtering_multiple():
    people = filter_people([[1, 2, 30, 40, .8, 0], [4, 5, 60, 70, .25, 0],
                            [1, 2, 30, 40, .9, 2], [1, 2, 30, 40, .24, 0],
                            [1, 2, 30, 40, float('nan'), 0]], .25)
    assert len(people) == 2
    assert people[0].confidence == .8
    assert filter_people([], .25) == ()


def test_banana_uses_configured_confidence_without_becoming_person():
    rows = [[1, 2, 30, 40, .24, 46], [4, 5, 60, 70, .25, 46],
            [8, 9, 80, 90, .9, 0], [1, 2, 30, 40, float('nan'), 46]]
    bananas = filter_bananas(rows, .25)
    assert len(bananas) == 1 and bananas[0].confidence == .25
    assert len(filter_people(rows, .25)) == 1


def test_banana_confidence_config_and_override():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    args = build_parser().parse_args(['--source', 'dual'])
    assert load_banana_confidence(args, root) == .25
    args = build_parser().parse_args(['--source', 'dual', '--banana-confidence', '.42'])
    assert load_banana_confidence(args, root) == .42


def test_stale_results_and_lost_camera_clear_detection():
    result = Detection((Person((1, 2, 10, 20), .6),), stamp=10, status="RUNNING",
                       bananas=(Banana((2, 3, 8, 9), .7),))
    assert visible_detection(result, True, 10.2).people
    assert visible_detection(result, True, 10.2).bananas
    assert not visible_detection(result, True, 10.6).people
    assert not visible_detection(result, True, 10.6).bananas
    assert visible_detection(result, False, 10.1).status == "STALE"


def test_overlay_does_not_change_usb_or_input_frames():
    frame = np.full((540, 960, 3), 80, np.uint8)
    states = {key: FrameState(frame=frame, stamp=10, error="") for key in ('g1', 'usb')}
    result = Detection((Person((200, 200, 400, 500), .8),), 10, (540, 960), 12, 15, "RUNNING")
    plain = compose(states, 'dual', 10.1, usb_rotate=180)
    with_boxes = compose(states, 'dual', 10.1, usb_rotate=180, detection=result)
    no_boxes = compose(states, 'dual', 10.1, usb_rotate=180, detection=result, boxes=False)
    assert np.array_equal(plain[:, 640:], with_boxes[:, 640:])
    assert not np.array_equal(plain[:, :640], with_boxes[:, :640])
    assert not np.array_equal(with_boxes[180:, :640], no_boxes[180:, :640])
    assert np.all(frame == 80)


@pytest.mark.parametrize('shape', [(360, 640), (480, 640), (640, 360)])
@pytest.mark.parametrize('mode', ['dual', 'g1'])
def test_diagnostics_never_cover_any_camera_pixels(shape, mode):
    from g1_bottle_reaction.game_vision.dual import letterbox
    h, w = shape
    frame = np.full((h, w, 3), (70, 100, 130), np.uint8)
    frame[:12] = (255, 0, 0)
    frame[-12:] = (0, 0, 255)
    states = {key: FrameState(frame=frame, stamp=10, error='') for key in ('g1', 'usb')}
    result = Detection(stamp=10, shape=shape, status='RUNNING')
    out = compose(states, mode, 10.1, detection=result, found_label='FOUND - WAITING FOR CLEAR')
    width = 640 if mode == 'dual' else 1280
    assert np.array_equal(out[78:438, :width], letterbox(frame, width, 360))


def test_box_coordinates_follow_camera_viewport():
    frame = np.zeros((360, 640, 3), np.uint8)
    state = FrameState(frame=frame, stamp=10, error='')
    result = Detection((Person((100, 40, 300, 200), .8),), 10, (360, 640), status='RUNNING')
    out = compose({'g1': state}, 'dual', 10.1, detection=result)
    assert tuple(out[78 + 200, 200]) == (0, 255, 255)


def test_banana_box_has_distinct_color():
    frame = np.zeros((360, 640, 3), np.uint8)
    state = FrameState(frame=frame, stamp=10, error='')
    result = Detection(stamp=10, shape=(360, 640), status='RUNNING',
                       bananas=(Banana((100, 40, 300, 200), .8),))
    out = compose({'g1': state}, 'dual', 10.1, detection=result)
    assert tuple(out[78 + 200, 200]) == (255, 80, 255)


def test_transition_logs_only_changes_not_every_confidence():
    logger = TransitionLogger()
    none = Detection(status="RUNNING")
    person = Detection((Person((1, 2, 10, 20), .6),), status="RUNNING")
    assert logger.update(none) is None
    assert 'PERSON DETECTED' in logger.update(person)
    assert logger.update(person) is None
    assert 'PERSON LOST' in logger.update(none)
    assert logger.update(none) is None


def test_banana_transition_logs_without_person_event():
    logger = TransitionLogger()
    none = Detection(status='RUNNING')
    banana = Detection(status='RUNNING', bananas=(Banana((1, 2, 10, 20), .6),))
    assert logger.update(none) is None
    assert 'BANANA DETECTED' in logger.update(banana)
    assert logger.update(banana) is None
    assert 'BANANA LOST' in logger.update(none)


@pytest.mark.parametrize('args', [ ['--yolo-confidence', 'nan'], ['--yolo-confidence', '0'],
                                  ['--yolo-confidence', '1.1'], ['--yolo-fps', '0'],
                                  ['--yolo-fps', 'inf'], ['--banana-confidence', '0'],
                                  ['--banana-confidence', 'nan'], ['--banana-confidence', '1.1'] ])
def test_invalid_yolo_cli(args):
    with pytest.raises(ValueError):
        validate_args(build_parser().parse_args(['--source', 'dual'] + args))


def test_yolo_never_selects_usb_or_legacy_source():
    assert main(['--source', 'usb-lan', '--yolo', '--headless']) == 2
    assert main(['--source', 'synthetic', '--yolo', '--headless']) == 2
    assert build_parser().parse_args([]).yolo is False


def slow_fake_process(connection, options):
    connection.send(('ready', 'mock'))
    try:
        while True:
            frame = connection.recv()
            time.sleep(.16)
            # Echo frame number in confidence, to verify latest source selection.
            value = float(frame[0, 0, 0]) / 255
            connection.send(('result', [[1, 2, 10, 20, value, 0]], 160.0))
    except (EOFError, BrokenPipeError):
        pass


def wait_until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('timed out')


def test_slow_process_latest_only_toggle_and_crash_isolation(tmp_path):
    lock = threading.Lock()
    current = FrameState(frame=np.full((30, 30, 3), 100, np.uint8),
                         stamp=time.monotonic(), count=1, error='')
    reads = []

    def source():
        with lock:
            current.stamp = time.monotonic()
            reads.append(current.count)
            return FrameState(**vars(current))

    worker = PersonWorker(source, tmp_path / 'unused.pt', max_fps=60, target=slow_fake_process)
    try:
        worker.start()
        wait_until(lambda: len(reads) >= 1)
        worker.toggle()  # Discard in-flight result and clear current detections.
        for count in range(2, 101):
            with lock:
                current.frame = np.full((30, 30, 3), 200, np.uint8)
                current.count = count
        wait_until(lambda: worker.count >= 1)
        assert worker.snapshot().status == 'OFF'
        assert not worker.snapshot().people
        worker.toggle()
        wait_until(lambda: worker.snapshot().status == 'RUNNING')
        assert reads[:2] == [1, 100]  # No 99-frame backlog.
        assert worker.snapshot().people[0].confidence == pytest.approx(200/255)
        worker.process.terminate()
        with lock:
            current.count = 101
        wait_until(lambda: worker.snapshot().status == 'ERROR')
        assert current.frame is not None  # Camera state unaffected.
    finally:
        worker.close()
