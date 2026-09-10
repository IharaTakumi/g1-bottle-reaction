from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from g1_bottle_reaction.game_vision.sources import (
    EndOfStream,
    G1RgbFrameSource,
    NpzFrameSource,
    OpenCVFrameSource,
    RealSenseFrameSource,
    SyntheticFrameSource,
)


def test_synthetic_source_provides_aligned_depth_and_invalid_stripe() -> None:
    source = SyntheticFrameSource(width=64, height=48, realtime=False)
    source.open()
    frame = source.read().validate()
    source.close()
    assert frame.bgr.shape == (48, 64, 3)
    assert frame.depth_m is not None
    assert frame.depth_m.shape == (48, 64)
    assert np.any(frame.depth_m == 0)
    assert frame.depth_m[0, 0] == pytest.approx(0.5)


def test_npz_recording_returns_color_and_metric_depth(tmp_path) -> None:
    path = tmp_path / "rgbd.npz"
    bgr = np.stack(
        [np.full((3, 4, 3), 10, dtype=np.uint8), np.full((3, 4, 3), 20, dtype=np.uint8)]
    )
    depth = np.stack(
        [np.full((3, 4), 1.0, dtype=np.float32), np.full((3, 4), 3.0, dtype=np.float32)]
    )
    np.savez(path, bgr=bgr, depth_m=depth, fps=np.array(30.0))
    source = NpzFrameSource(path, realtime=False)
    source.open()
    first = source.read()
    second = source.read()
    with pytest.raises(EndOfStream):
        source.read()
    source.close()
    assert first.bgr[0, 0, 0] == 10
    assert second.bgr[0, 0, 0] == 20
    assert first.depth_m is not None and first.depth_m[0, 0] == pytest.approx(1.0)
    assert second.depth_m is not None and second.depth_m[0, 0] == pytest.approx(3.0)


def test_npy_is_rejected_without_masking_the_useful_npz_error(tmp_path) -> None:
    path = tmp_path / "not-an-archive.npy"
    np.save(path, np.zeros((2, 3), dtype=np.uint8))
    source = NpzFrameSource(path, realtime=False)
    with pytest.raises(RuntimeError, match="must contain a 'bgr' array"):
        source.open()


def test_empty_npz_recording_is_rejected_before_looping(tmp_path) -> None:
    path = tmp_path / "empty.npz"
    np.savez(path, bgr=np.empty((0, 3, 4, 3), dtype=np.uint8))
    source = NpzFrameSource(path, loop=True, realtime=False)
    with pytest.raises(RuntimeError, match="at least one frame"):
        source.open()


def test_g1_adapter_remains_explicitly_rgb_only() -> None:
    class FakeCamera:
        opened = False
        closed = False

        def open(self):
            self.opened = True

        def read(self):
            return np.zeros((3, 4, 3), dtype=np.uint8)

        def close(self):
            self.closed = True

    camera = FakeCamera()
    source = G1RgbFrameSource("eth-test", camera_source=camera)
    source.open()
    frame = source.read()
    source.close()
    assert camera.opened and camera.closed
    assert source.is_g1_rgb_only
    assert frame.depth_m is None


def test_webcam_requests_single_frame_buffer_and_keeps_bgr_contract() -> None:
    import cv2

    expected = np.full((3, 4, 3), 9, dtype=np.uint8)

    class FakeCapture:
        released = False

        def __init__(self):
            self.settings = []

        def isOpened(self):
            return True

        def set(self, prop, value):
            self.settings.append((prop, value))
            return True

        def read(self):
            return True, expected

        def release(self):
            self.released = True

    capture = FakeCapture()
    source = OpenCVFrameSource(7, capture_factory=lambda device: capture)
    source.open()
    frame = source.read()
    source.close()
    assert frame.bgr is expected
    assert frame.depth_m is None
    assert (cv2.CAP_PROP_BUFFERSIZE, 1) in capture.settings
    assert capture.released


def test_video_file_is_paced_at_its_recorded_fps(monkeypatch, tmp_path) -> None:
    import cv2

    expected = np.full((3, 4, 3), 9, dtype=np.uint8)

    class FakeCapture:
        def isOpened(self):
            return True

        def get(self, prop):
            assert prop == cv2.CAP_PROP_FPS
            return 20.0

        def read(self):
            return True, expected

        def release(self):
            pass

    sleeps = []
    monkeypatch.setattr(
        "g1_bottle_reaction.game_vision.sources.time.monotonic", lambda: 1.0
    )
    monkeypatch.setattr(
        "g1_bottle_reaction.game_vision.sources.time.sleep", sleeps.append
    )
    source = OpenCVFrameSource(
        tmp_path / "test.mp4", capture_factory=lambda path: FakeCapture()
    )
    source.open()
    source.read()
    source.read()
    source.close()
    assert sleeps == [pytest.approx(0.05)]


def test_realsense_aligns_depth_to_color_before_numpy_conversion() -> None:
    events: list[str] = []
    color_data = np.full((2, 3, 3), 7, dtype=np.uint8)
    depth_data = np.array([[1000, 1800, 0], [2500, 3000, 500]], dtype=np.uint16)

    class FakeFrame:
        def __init__(self, data, name, units=None):
            self.data = data
            self.name = name
            self.units = units

        def __bool__(self):
            return True

        def get_data(self):
            assert "align" in events
            events.append(f"{self.name}-data")
            return self.data

        def get_units(self):
            assert self.units is not None
            return self.units

    color_frame = FakeFrame(color_data, "color")
    depth_frame = FakeFrame(depth_data, "depth", units=0.001)

    class FakeFrames:
        def get_color_frame(self):
            return color_frame

        def get_depth_frame(self):
            return depth_frame

    class FakeAlign:
        def process(self, frames):
            events.append("align")
            return frames

    class FakeConfig:
        def __init__(self):
            self.streams = []

        def enable_stream(self, *args):
            self.streams.append(args)

        def enable_device(self, serial):
            events.append(f"serial:{serial}")

    class FakePipeline:
        stopped = False

        def start(self, config):
            events.append("start")
            return SimpleNamespace(
                get_device=lambda: SimpleNamespace(
                    first_depth_sensor=lambda: SimpleNamespace(get_depth_scale=lambda: 0.002)
                )
            )

        def wait_for_frames(self, timeout_ms):
            events.append(f"wait:{timeout_ms}")
            return FakeFrames()

        def stop(self):
            self.stopped = True

    pipeline = FakePipeline()
    config = FakeConfig()
    fake_rs = SimpleNamespace(
        stream=SimpleNamespace(color="color", depth="depth"),
        format=SimpleNamespace(bgr8="bgr8", z16="z16"),
        pipeline=lambda: pipeline,
        config=lambda: config,
        align=lambda target: FakeAlign(),
    )
    source = RealSenseFrameSource(
        width=3,
        height=2,
        fps=30,
        timeout_ms=1234,
        serial="D435-TEST",
        rs_module=fake_rs,
    )
    source.open()
    frame = source.read()
    source.close()
    assert config.streams == [
        ("color", 3, 2, "bgr8", 30),
        ("depth", 3, 2, "z16", 30),
    ]
    assert events.index("align") < events.index("color-data")
    assert events.index("align") < events.index("depth-data")
    assert frame.depth_m is not None
    assert np.allclose(frame.depth_m, [[1.0, 1.8, 0.0], [2.5, 3.0, 0.5]])
    assert frame.bgr.shape == (2, 3, 3)
    assert pipeline.stopped


def test_realsense_import_is_lazy_when_binding_is_absent() -> None:
    # Merely constructing the source must not import or require pyrealsense2.
    source = RealSenseFrameSource()
    assert source._pipeline is None


def test_realsense_rgb8_bag_is_converted_to_bgr(tmp_path) -> None:
    class ColorFrame:
        profile = SimpleNamespace(format=lambda: "rgb8")

        def __bool__(self):
            return True

        def get_data(self):
            return np.array([[[10, 20, 30]]], dtype=np.uint8)

    class DepthFrame:
        def __bool__(self):
            return True

        def get_data(self):
            return np.array([[1000]], dtype=np.uint16)

        def get_units(self):
            return 0.001

    frames = SimpleNamespace(
        get_color_frame=lambda: ColorFrame(), get_depth_frame=lambda: DepthFrame()
    )
    source = RealSenseFrameSource(
        bag_file=tmp_path / "recording.bag",
        rs_module=SimpleNamespace(
            format=SimpleNamespace(rgb8="rgb8", bgr8="bgr8")
        ),
    )
    source._pipeline = SimpleNamespace(wait_for_frames=lambda timeout: frames)
    source._align = SimpleNamespace(process=lambda value: value)
    source._depth_scale = 0.001
    frame = source.read()
    assert frame.bgr[0, 0].tolist() == [30, 20, 10]
    assert frame.depth_m is not None and frame.depth_m[0, 0] == pytest.approx(1.0)


def test_realsense_bag_uses_playback_status_for_clean_eof(tmp_path) -> None:
    fake_rs = SimpleNamespace(
        playback_status=SimpleNamespace(stopped="stopped"),
        format=SimpleNamespace(rgb8="rgb8", bgr8="bgr8"),
    )
    source = RealSenseFrameSource(bag_file=tmp_path / "recording.bag", rs_module=fake_rs)
    source._pipeline = SimpleNamespace()
    source._align = SimpleNamespace()
    source._depth_scale = 0.001
    source._playback = SimpleNamespace(current_status=lambda: "stopped")
    with pytest.raises(EndOfStream):
        source.read()


def test_realsense_bag_stops_pipeline_when_playback_setup_fails(tmp_path) -> None:
    bag = tmp_path / "recording.bag"
    bag.write_bytes(b"test")

    class Pipeline:
        stopped = False

        def start(self, config):
            device = SimpleNamespace(
                first_depth_sensor=lambda: SimpleNamespace(get_depth_scale=lambda: 0.001),
                as_playback=lambda: (_ for _ in ()).throw(RuntimeError("bad playback")),
            )
            return SimpleNamespace(get_device=lambda: device)

        def stop(self):
            self.stopped = True

    pipeline = Pipeline()
    fake_rs = SimpleNamespace(
        stream=SimpleNamespace(color="color"),
        pipeline=lambda: pipeline,
        config=lambda: SimpleNamespace(enable_device_from_file=lambda *args: None),
        align=lambda target: SimpleNamespace(),
    )
    source = RealSenseFrameSource(bag_file=bag, rs_module=fake_rs)
    with pytest.raises(RuntimeError, match="initialize RealSense playback"):
        source.open()
    assert pipeline.stopped
    assert source._pipeline is None
