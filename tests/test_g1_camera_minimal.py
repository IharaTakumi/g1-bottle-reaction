from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from g1_bottle_reaction.adapters.g1_robot import UnitreeSdkRuntime
from g1_bottle_reaction.vision.camera import _decode_jpeg_bgr

spec = importlib.util.spec_from_file_location(
    "g1_camera_minimal", Path(__file__).resolve().parents[1] / "tools/g1_camera_minimal.py"
)
minimal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(minimal)


def nic(name="cable0", wired=True, flags=None, address="10.42.0.2"):
    return {"ifname": name, "wired": wired,
            "flags": ["UP", "LOWER_UP"] if flags is None else flags,
            "addr_info": [{"family": "inet", "scope": "global", "local": address, "prefixlen": 24}]}


def test_cli_and_nonfinite_limits():
    args = minimal.build_parser().parse_args(["--g1-ip", "10.42.0.3", "--duration", "300"])
    assert str(args.g1_ip) == "10.42.0.3"
    assert args.no_frame_timeout == 5
    for value in ("0", "-1", "nan", "inf"):
        with pytest.raises(SystemExit):
            minimal.build_parser().parse_args(["--no-frame-timeout", value])


def test_select_wired_without_hardcoded_subnet():
    args = minimal.build_parser().parse_args(["--g1-ip", "10.42.0.3"])
    assert minimal.select_interface([nic(), nic("wifi0", False)], None, args.g1_ip) == (
        "cable0", ["10.42.0.2/24"])


@pytest.mark.parametrize("interfaces,name", [
    ([nic("wifi0", False)], "wifi0"),
    ([nic(flags=["UP"])], None),
    ([nic(), nic("cable1")], None),
    ([nic()], "missing"),
])
def test_reject_ambiguous_disconnected_or_wireless_nic(interfaces, name):
    with pytest.raises(RuntimeError, match="Cannot uniquely select"):
        minimal.select_interface(interfaces, name)


def test_peer_must_be_on_link_and_not_local_address():
    for peer in ("10.43.0.3", "10.42.0.2"):
        args = minimal.build_parser().parse_args(["--g1-ip", peer])
        with pytest.raises(RuntimeError):
            minimal.select_interface([nic()], None, args.g1_ip)


@pytest.mark.parametrize("route", [[], [{"dev": "wifi0"}], [{"dev": "cable0", "gateway": "10.42.0.1"}]])
def test_route_must_use_selected_cable(monkeypatch, route):
    monkeypatch.setattr(minimal.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=minimal.json.dumps(route)))
    with pytest.raises(RuntimeError, match="direct route"):
        minimal.verify_route("10.42.0.3", "cable0")


def test_jpeg_decode_reuses_bgr_contract():
    frame = np.full((8, 12, 3), (20, 100, 220), dtype=np.uint8)
    ok, data = cv2.imencode(".jpg", frame)
    assert ok
    decoded = _decode_jpeg_bgr(data.tobytes())
    assert decoded.shape == (8, 12, 3)
    assert np.allclose(decoded, frame, atol=4)
    for invalid in (b"", b"not jpeg", None, [999]):
        with pytest.raises(RuntimeError):
            _decode_jpeg_bgr(invalid)


@pytest.mark.parametrize("fail", [False, True])
def test_camera_runtime_only_loads_camera_and_restores_xml(fail):
    original = '<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="$__IF_NAME__$"/>' \
               '</Interfaces></General><Tracing><Verbosity>config</Verbosity></Tracing></Domain></CycloneDDS>'
    calls = []
    channel = SimpleNamespace(ChannelConfigHasInterface=original)

    def initialize(domain, interface):
        calls.append((domain, interface))
        assert "Tracing" not in channel.ChannelConfigHasInterface
        assert 'name="$__IF_NAME__$"' in channel.ChannelConfigHasInterface
        if fail:
            raise RuntimeError("fake DDS failure")

    channel.ChannelFactoryInitialize = initialize
    runtime = UnitreeSdkRuntime(
        channel_module_loader=lambda: channel,
        video_client_loader=lambda: "VIDEO_ONLY",
        symbol_loader=lambda: pytest.fail("must not load audio/control symbols"),
    )
    init, video = runtime.load_camera_symbols()
    assert video == "VIDEO_ONLY"
    if fail:
        with pytest.raises(RuntimeError, match="fake DDS failure"):
            init(0, "cable0")
    else:
        init(0, "cable0")
    assert calls == [(0, "cable0")]
    assert channel.ChannelConfigHasInterface == original


class FakeSource:
    def __init__(self, clock, responses):
        self.clock = clock
        self.responses = iter(responses)
        self.closed = False

    def open(self):
        pass

    def read(self):
        self.clock[0] += 0.25
        response = next(self.responses, None)
        if response is None:
            raise RuntimeError("return code 3104")
        return response

    def close(self):
        self.closed = True


def fake_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(minimal.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(minimal.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    return clock


@pytest.mark.parametrize("initial_frame", [False, True])
def test_no_frame_timeout_initial_and_after_disconnect(monkeypatch, initial_frame):
    clock = fake_clock(monkeypatch)
    responses = [np.zeros((8, 12, 3), np.uint8)] if initial_frame else []
    source = FakeSource(clock, responses)
    args = minimal.build_parser().parse_args(["--headless", "--no-frame-timeout", "1"])
    with pytest.raises(RuntimeError, match="No camera frame received for 1 seconds"):
        minimal.capture(args, source)
    assert source.closed
    assert clock[0] < 1.6


def test_failed_open_also_releases_source(monkeypatch):
    source = FakeSource(fake_clock(monkeypatch), [])
    source.open = lambda: (_ for _ in ()).throw(RuntimeError("DDS initialization failed"))
    with pytest.raises(RuntimeError, match="DDS initialization failed"):
        minimal.capture(minimal.build_parser().parse_args([]), source)
    assert source.closed


def test_display_keys_and_cleanup(monkeypatch):
    clock = fake_clock(monkeypatch)
    source = FakeSource(clock, [np.zeros((8, 12, 3), np.uint8)] * 3)
    keys = iter([ord("f"), ord("q"), 0])
    modes = []
    destroyed = []
    cv = SimpleNamespace(
        WINDOW_NORMAL=0, WINDOW_FULLSCREEN=1, WND_PROP_FULLSCREEN=0, WND_PROP_VISIBLE=4,
        FONT_HERSHEY_SIMPLEX=0, namedWindow=lambda *a: None,
        setWindowProperty=lambda *a: modes.append(a[-1]),
        putText=lambda *a: None, imshow=lambda *a: None,
        waitKey=lambda *a: next(keys), getWindowProperty=lambda *a: 1,
        destroyAllWindows=lambda: destroyed.append(True),
    )
    assert minimal.capture(minimal.build_parser().parse_args([]), source, cv) == 0
    assert modes == [0, 1]
    assert destroyed and source.closed


def test_native_abort_has_human_error():
    if sys.platform != "linux":
        pytest.skip("Linux process signals")
    command = [sys.executable, "-c", "import resource, os, signal; "
               "resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); os.kill(os.getpid(), signal.SIGABRT)"]
    with pytest.raises(RuntimeError, match="SIGABRT"):
        minimal.supervise(command, 1)
