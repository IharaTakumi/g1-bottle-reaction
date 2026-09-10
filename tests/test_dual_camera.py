from __future__ import annotations

import io
import struct
import sys
import time

import cv2
import numpy as np
import pytest

from g1_bottle_reaction.game_vision.app import build_parser
from g1_bottle_reaction.game_vision.camera_ipc import BGR, HEADER, MAGIC, MAX_BYTES, receive, send
from g1_bottle_reaction.game_vision.dual import FrameState, LatestReader, compose, letterbox, validate_args


def test_dual_cli_and_rotation():
    args = build_parser().parse_args(["--source", "dual", "--usb-bind", "10.1.2.2", "--usb-host", "10.1.2.3",
                                     "--start-usb-sender", "--usb-rotate", "180", "--windowed"])
    validate_args(args)
    assert args.usb_rotate == 180 and args.fullscreen is False
    assert args.usb_port == 56000


@pytest.mark.parametrize("arguments", [
    ["--usb-bind", "bad"], ["--usb-bind", "0.0.0.0"], ["--usb-port", "0"],
    ["--duration", "nan"], ["--duration", "-1"], ["--start-usb-sender"],
    ["--vision-preset", "normal"], ["--publish-processed"], ["--max-frames", "0"],
])
def test_invalid_cli(arguments):
    with pytest.raises(ValueError):
        validate_args(build_parser().parse_args(["--source", "dual"] + arguments))


def test_letterbox_preserves_aspect_and_black_bars():
    frame = np.full((100, 200, 3), 255, np.uint8)
    out = letterbox(frame, 200, 200)
    assert out.shape == (200, 200, 3)
    assert np.all(out[50:150] == 255)
    assert not out[:50].any() and not out[150:].any()


def test_stale_usb_blacked_out_while_g1_continues():
    frame = np.full((100, 100, 3), (100, 160, 210), np.uint8)
    states = {"g1": FrameState(frame=frame, stamp=10, error=""),
              "usb": FrameState(frame=frame, stamp=1, error="")}
    out = compose(states, "dual", now=10.1)
    assert out.shape == (540, 1280, 3)
    assert out[350, 320].any()
    assert not out[350, 960].any()
    states["g1"].error = "worker failed"
    states["usb"].stamp = 10
    out = compose(states, "dual", now=10.1)
    assert not out[350, 320].any()
    assert out[350, 960].any()


def test_only_usb_rotates_and_source_arrays_unchanged():
    frame = np.zeros((540, 640, 3), np.uint8)
    frame[400:500, 100:200] = (90, 170, 250)
    states = {k: FrameState(frame=frame, stamp=10, error="") for k in ("g1", "usb")}
    original = frame.copy()
    zero = compose(states, "dual", 10.1, usb_rotate=0)
    rotated = compose(states, "dual", 10.1, usb_rotate=180)
    assert np.array_equal(zero[:, :640], rotated[:, :640])
    expected = letterbox(frame, 640, 360)
    assert np.array_equal(zero[78:438, 640:], expected)
    assert np.array_equal(rotated[78:438, 640:], letterbox(cv2.rotate(frame, cv2.ROTATE_180), 640, 360))
    assert np.array_equal(frame, original)


@pytest.mark.parametrize("mode", ["g1", "usb", "dual"])
def test_display_modes_with_missing_source(mode):
    assert compose({}, mode, 1).shape == (540, 1280, 3)


def test_pipe_roundtrip_bgr_and_jpeg():
    frame = np.full((8, 12, 3), 128, np.uint8)
    stream = io.BytesIO()
    send(stream, frame.tobytes(), 1.5, 12, 8, 1920, 1080, BGR, 9)
    stream.seek(0)
    data, stamp, w, h, ow, oh, encoding, lost = receive(stream)
    assert (stamp, w, h, ow, oh, encoding, lost) == (1.5, 12, 8, 1920, 1080, BGR, 9)
    assert data == frame.tobytes()
    ok, jpeg = cv2.imencode(".jpg", frame)
    assert ok
    stream = io.BytesIO()
    send(stream, jpeg.tobytes(), 3)
    stream.seek(0)
    assert receive(stream)[0] == jpeg.tobytes()


def test_pipe_rejects_partial_and_oversized_frames():
    with pytest.raises(EOFError):
        receive(io.BytesIO(b"partial"))
    with pytest.raises(ValueError):
        receive(io.BytesIO(HEADER.pack(MAGIC, MAX_BYTES + 1, 0, 1, 1, 1, 1, BGR, 0)))
    with pytest.raises(ValueError):
        receive(io.BytesIO(HEADER.pack(MAGIC, 4, 0, 1, 1, 1, 1, BGR, 0)))


def test_latest_slot_drops_old_frames_and_failure_is_local():
    stream = io.BytesIO()
    for i in range(1, 4):
        send(stream, bytes([i] * 12), time.monotonic(), 2, 2, 2, 2, BGR)
    stream.seek(0)
    reader = LatestReader("mock", [])
    reader.process = type("Process", (), {"stdout": stream})()
    reader._read()
    state = reader.snapshot()
    assert state.count == 3 and state.skipped == 2
    assert np.all(state.frame == 3)
    assert "closed" in state.error
    other = LatestReader("other", [])
    assert other.state.error == "WAITING"


def test_failed_process_does_not_kill_other_reader():
    bad = LatestReader("bad", [sys.executable, "-c", "raise SystemExit(2)"])
    idle = LatestReader("idle", [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        bad.start()
        idle.start()
        bad.thread.join(timeout=3)
        assert bad.snapshot().error != "WAITING"
        assert idle.process.poll() is None
    finally:
        bad.close()
        idle.close()
