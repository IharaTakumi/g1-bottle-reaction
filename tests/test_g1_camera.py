from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from g1_bottle_reaction.vision.camera import (
    G1CameraSource,
    OpenCVCameraSource,
    TeleImagerCameraSource,
    validate_bgr_frame,
)
from g1_bottle_reaction.main import _create_camera_source, build_parser


def _jpeg_bytes() -> bytes:
    image = np.zeros((8, 12, 3), dtype=np.uint8)
    image[:, :] = (20, 100, 220)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


class FakeVideoClient:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = 0

    def GetImageSample(self):
        self.calls += 1
        return self.responses.pop(0)


class FakeRuntime:
    def __init__(self, client: FakeVideoClient) -> None:
        self.client = client
        self.calls: list[tuple] = []

    def create_video_client(self, *args):
        self.calls.append(args)
        return self.client


def test_video_client_jpeg_returns_bgr_contract() -> None:
    client = FakeVideoClient([(0, list(_jpeg_bytes()))])
    runtime = FakeRuntime(client)
    source = G1CameraSource(
        "イーサネット 3", timeout_seconds=2.5, runtime=runtime
    )
    source.open()
    frame = source.read()
    source.close()
    assert runtime.calls == [("イーサネット 3", 2.5)]
    assert client.calls == 1
    assert frame.shape == (8, 12, 3)
    assert frame.dtype == np.uint8
    # OpenCV IMREAD_COLOR contract is BGR; JPEG is lossy, so use tolerance.
    assert np.allclose(frame[4, 6], (20, 100, 220), atol=4)


def test_explicit_network_address_reaches_video_runtime() -> None:
    runtime = FakeRuntime(FakeVideoClient([(0, _jpeg_bytes())]))
    source = G1CameraSource(
        None,
        network_address="192.168.123.222",
        timeout_seconds=3.0,
        runtime=runtime,
    )
    source.open()
    assert runtime.calls == [(None, 3.0, "192.168.123.222")]


def test_video_client_nonzero_return_code_has_clear_error() -> None:
    source = G1CameraSource(
        "eth-test",
        read_attempts=1,
        runtime=FakeRuntime(FakeVideoClient([(3104, [])])),
    )
    source.open()
    with pytest.raises(RuntimeError, match="return code 3104"):
        source.read()


def test_video_client_decode_failure_has_clear_error() -> None:
    source = G1CameraSource(
        "eth-test",
        read_attempts=1,
        runtime=FakeRuntime(FakeVideoClient([(0, b"not-a-jpeg")])),
    )
    source.open()
    with pytest.raises(RuntimeError, match="could not decode"):
        source.read()


def test_video_client_temporary_failure_is_retried() -> None:
    client = FakeVideoClient([(1, []), (0, _jpeg_bytes())])
    source = G1CameraSource(
        "eth-test",
        read_attempts=2,
        retry_delay_seconds=0,
        runtime=FakeRuntime(client),
    )
    source.open()
    frame = source.read()
    assert frame.shape == (8, 12, 3)
    assert client.calls == 2


def test_opencv_camera_path_keeps_existing_contract(monkeypatch) -> None:
    expected = np.zeros((4, 6, 3), dtype=np.uint8)

    class FakeCapture:
        released = False

        def isOpened(self):
            return True

        def read(self):
            return True, expected

        def release(self):
            self.released = True

    capture = FakeCapture()
    monkeypatch.setitem(
        sys.modules,
        "cv2",
        SimpleNamespace(VideoCapture=lambda device: capture),
    )
    source = OpenCVCameraSource(7)
    source.open()
    assert source.read() is expected
    source.close()
    assert capture.released


def test_bgr_contract_rejects_wrong_shape_and_dtype() -> None:
    with pytest.raises(RuntimeError, match="HxWx3 uint8"):
        validate_bgr_frame(np.zeros((8, 12), dtype=np.uint8))
    with pytest.raises(RuntimeError, match="HxWx3 uint8"):
        validate_bgr_frame(np.zeros((8, 12, 3), dtype=np.float32))


def test_g1_camera_module_import_does_not_require_unitree_or_teleimager() -> None:
    module = importlib.import_module("g1_bottle_reaction.vision.camera")
    assert module.G1CameraSource is not None
    assert module.TeleImagerCameraSource is not None


def test_teleimager_is_explicit_legacy_source() -> None:
    assert "Legacy" in TeleImagerCameraSource.__doc__


def test_g1_camera_cli_builds_video_client_source(app_config) -> None:
    args = build_parser().parse_args(
        [
            "--game",
            "stealth-phone",
            "--robot",
            "mock",
            "--camera-source",
            "g1",
            "--network-interface",
            "イーサネット 3",
            "--speech",
            "console",
        ]
    )
    source = _create_camera_source(args, app_config)
    assert isinstance(source, G1CameraSource)
    assert source.network_interface == "イーサネット 3"
