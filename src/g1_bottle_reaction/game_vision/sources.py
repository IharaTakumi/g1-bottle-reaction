from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
import time
from typing import Any

import numpy as np

from .frames import RgbdFrame


class EndOfStream(RuntimeError):
    """Raised when a finite recording has no more frames."""


class FrameSource(ABC):
    """Source contract dedicated to game vision; independent of vision.CameraSource."""

    label = "CAMERA"
    is_g1_rgb_only = False
    is_prefiltered_game = False

    @abstractmethod
    def open(self) -> None:
        pass

    @abstractmethod
    def read(self) -> RgbdFrame:
        pass

    @abstractmethod
    def close(self) -> None:
        pass


class OpenCVFrameSource(FrameSource):
    """Webcam or ordinary RGB video source used by the hardware-free prototype."""

    def __init__(
        self,
        source: int | str | Path,
        *,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        loop: bool = False,
        capture_factory: Any | None = None,
    ) -> None:
        self.source = str(source) if isinstance(source, Path) else source
        self.width = width
        self.height = height
        self.fps = fps
        self.loop = loop
        self._capture_factory = capture_factory
        self._capture: Any | None = None
        self._sequence = 0
        self._is_camera = isinstance(source, int)
        self._video_fps = float(fps)
        self._next_frame_at: float | None = None
        self.label = "WEBCAM" if self._is_camera else "VIDEO"

    def open(self) -> None:
        if self._capture is not None:
            return
        import cv2

        factory = self._capture_factory or cv2.VideoCapture
        capture = factory(self.source)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(f"could not open {self.label.lower()} source {self.source!r}")
        if self._is_camera:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            capture.set(cv2.CAP_PROP_FPS, self.fps)
            # Backends may ignore this, but a one-frame buffer is the preferred
            # low-latency setting when it is supported.
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            get = getattr(capture, "get", None)
            source_fps = float(get(cv2.CAP_PROP_FPS)) if callable(get) else 0.0
            if np.isfinite(source_fps) and source_fps > 0:
                self._video_fps = source_fps
        self._capture = capture
        self._next_frame_at = None

    def read(self) -> RgbdFrame:
        if self._capture is None:
            raise RuntimeError(f"{self.label.lower()} source is not open")
        ok, bgr = self._capture.read()
        if (not ok or bgr is None) and self.loop and not self._is_camera:
            import cv2

            self._capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self._next_frame_at = None
            ok, bgr = self._capture.read()
        if not ok or bgr is None:
            if self._is_camera:
                raise RuntimeError(f"camera {self.source!r} frame capture failed")
            raise EndOfStream(f"video {self.source!r} reached end of stream")
        bgr = _validate_bgr(bgr)
        if not self._is_camera:
            now = time.monotonic()
            if self._next_frame_at is not None and now < self._next_frame_at:
                time.sleep(self._next_frame_at - now)
            self._next_frame_at = (
                max(now, self._next_frame_at or now) + 1.0 / self._video_fps
            )
        self._sequence += 1
        return RgbdFrame(bgr, None, time.monotonic(), self._sequence)

    def close(self) -> None:
        capture, self._capture = self._capture, None
        self._next_frame_at = None
        if capture is not None:
            capture.release()


class G1RgbFrameSource(FrameSource):
    """Official videohub VideoClient adapter.

    VideoClient provides JPEG/BGR only. It is useful for the safety view and
    connectivity checks, but cannot activate distance fog without a depth
    transport. The application therefore fails the game image closed by
    default for this source.
    """

    label = "G1 RGB SAFETY CAMERA"
    is_g1_rgb_only = True

    def __init__(
        self,
        network_interface: str | None,
        *,
        network_address: str | None = None,
        timeout_seconds: float = 3.0,
        camera_source: Any | None = None,
    ) -> None:
        self.network_interface = network_interface
        self.network_address = network_address
        self.timeout_seconds = timeout_seconds
        self._camera_source = camera_source
        self._sequence = 0

    def open(self) -> None:
        if self._camera_source is None:
            # Keep every Unitree SDK import behind the existing lazy adapter.
            from g1_bottle_reaction.vision.camera import G1CameraSource

            self._camera_source = G1CameraSource(
                self.network_interface,
                network_address=self.network_address,
                timeout_seconds=self.timeout_seconds,
                read_attempts=2,
                retry_delay_seconds=0.03,
            )
        self._camera_source.open()

    def read(self) -> RgbdFrame:
        if self._camera_source is None:
            raise RuntimeError("G1 RGB source is not open")
        bgr = _validate_bgr(self._camera_source.read())
        self._sequence += 1
        return RgbdFrame(bgr, None, time.monotonic(), self._sequence)

    def close(self) -> None:
        source, self._camera_source = self._camera_source, None
        if source is not None:
            source.close()


class RealSenseFrameSource(FrameSource):
    """Direct D435i source with RealSense Depth-to-Color alignment."""

    label = "REALSENSE CAMERA"

    def __init__(
        self,
        *,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        timeout_ms: int = 3000,
        serial: str | None = None,
        bag_file: str | Path | None = None,
        repeat_bag: bool = False,
        rs_module: Any | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.timeout_ms = timeout_ms
        self.serial = serial
        self.bag_file = Path(bag_file).expanduser() if bag_file is not None else None
        self.repeat_bag = repeat_bag
        self._rs = rs_module
        self._pipeline: Any | None = None
        self._align: Any | None = None
        self._playback: Any | None = None
        self._depth_scale: float | None = None
        self._sequence = 0
        if self.bag_file is not None:
            self.label = "REALSENSE RECORDING"

    def open(self) -> None:
        if self._pipeline is not None:
            return
        if self._rs is None:
            try:
                import pyrealsense2 as rs
            except ImportError as exc:
                raise RuntimeError(
                    "pyrealsense2 is required only for --source realsense; "
                    "use the RealSense binding already approved for this host"
                ) from exc
            self._rs = rs

        rs = self._rs
        pipeline = rs.pipeline()
        config = rs.config()
        if self.bag_file is not None:
            if not self.bag_file.is_file():
                raise RuntimeError(f"RealSense bag file does not exist: {self.bag_file}")
            config.enable_device_from_file(
                str(self.bag_file.resolve()), self.repeat_bag
            )
        else:
            if self.serial:
                config.enable_device(self.serial)
            config.enable_stream(
                rs.stream.color,
                self.width,
                self.height,
                rs.format.bgr8,
                self.fps,
            )
            config.enable_stream(
                rs.stream.depth,
                self.width,
                self.height,
                rs.format.z16,
                self.fps,
            )

        try:
            profile = pipeline.start(config)
            device = profile.get_device()
            depth_sensor = device.first_depth_sensor()
            depth_scale = float(depth_sensor.get_depth_scale())
        except Exception as exc:
            try:
                pipeline.stop()
            except Exception:
                pass
            raise RuntimeError(f"could not start RealSense RGB+Depth pipeline: {exc}") from exc
        if not np.isfinite(depth_scale) or depth_scale <= 0:
            pipeline.stop()
            raise RuntimeError(f"RealSense returned invalid depth scale {depth_scale!r}")

        # Alignment is created once and applied before either numpy array is
        # exposed, so equal source resolutions are never assumed to be aligned.
        try:
            align = rs.align(rs.stream.color)
        except Exception as exc:
            pipeline.stop()
            raise RuntimeError(f"could not initialize RealSense alignment: {exc}") from exc
        self._align = align
        if self.bag_file is not None:
            as_playback = getattr(device, "as_playback", None)
            if callable(as_playback):
                try:
                    self._playback = as_playback()
                except Exception as exc:
                    self._align = None
                    pipeline.stop()
                    raise RuntimeError(
                        f"could not initialize RealSense playback: {exc}"
                    ) from exc
        self._depth_scale = depth_scale
        self._pipeline = pipeline

    def read(self) -> RgbdFrame:
        if self._pipeline is None or self._align is None or self._depth_scale is None:
            raise RuntimeError("RealSense source is not open")
        if self._playback_stopped():
            raise EndOfStream("RealSense recording reached end of stream")
        try:
            frames = self._pipeline.wait_for_frames(self.timeout_ms)
            aligned = self._align.process(frames)
            color_frame = aligned.get_color_frame()
            depth_frame = aligned.get_depth_frame()
        except Exception as exc:
            if self.bag_file is not None and self._playback_stopped():
                raise EndOfStream("RealSense recording reached end of stream") from exc
            raise RuntimeError(f"RealSense frame acquisition failed: {exc}") from exc
        if not color_frame or not depth_frame:
            raise RuntimeError("RealSense aligned frameset did not contain both color and depth")

        color_data = np.asanyarray(color_frame.get_data())
        if self.bag_file is not None:
            profile = getattr(color_frame, "profile", None)
            format_method = getattr(profile, "format", None)
            if not callable(format_method):
                raise RuntimeError("RealSense recording color format is unavailable")
            color_format = format_method()
            if color_format == self._rs.format.rgb8:
                color_data = color_data[:, :, ::-1]
            elif color_format != self._rs.format.bgr8:
                raise RuntimeError(
                    f"unsupported RealSense recording color format: {color_format}"
                )
        bgr = _validate_bgr(color_data)
        raw_depth = np.asanyarray(depth_frame.get_data())
        if raw_depth.ndim != 2:
            raise RuntimeError("RealSense depth frame is not two-dimensional")
        units = self._depth_scale
        get_units = getattr(depth_frame, "get_units", None)
        if callable(get_units):
            frame_units = float(get_units())
            if np.isfinite(frame_units) and frame_units > 0:
                units = frame_units
        depth_m = np.ascontiguousarray(raw_depth, dtype=np.float32) * units
        self._sequence += 1
        return RgbdFrame(
            np.ascontiguousarray(bgr),
            np.ascontiguousarray(depth_m, dtype=np.float32),
            time.monotonic(),
            self._sequence,
        ).validate()

    def close(self) -> None:
        pipeline, self._pipeline = self._pipeline, None
        self._align = None
        self._playback = None
        self._depth_scale = None
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception as exc:
                raise RuntimeError(f"could not stop RealSense pipeline cleanly: {exc}") from exc

    def _playback_stopped(self) -> bool:
        if self._playback is None or self._rs is None:
            return False
        current_status = getattr(self._playback, "current_status", None)
        playback_status = getattr(self._rs, "playback_status", None)
        stopped = getattr(playback_status, "stopped", None)
        if not callable(current_status) or stopped is None:
            return False
        try:
            return current_status() == stopped
        except Exception:
            # Status polling is advisory; acquisition below still reports a
            # useful RealSense error or a confirmed EOF on the next check.
            return False


class NpzFrameSource(FrameSource):
    """Small RGBD recording format for development without a RealSense device.

    The archive contains ``bgr`` (N,H,W,3 uint8), optional ``depth_m``
    (N,H,W), and optional scalar ``fps``. Pickle is never enabled.
    """

    label = "RGBD RECORDING"

    def __init__(
        self,
        path: str | Path,
        *,
        loop: bool = False,
        realtime: bool = True,
    ) -> None:
        self.path = Path(path).expanduser()
        self.loop = loop
        self.realtime = realtime
        self._archive: Any | None = None
        self._bgr: np.ndarray | None = None
        self._depth: np.ndarray | None = None
        self._fps = 30.0
        self._index = 0
        self._next_frame_at: float | None = None

    def open(self) -> None:
        if self._archive is not None:
            return
        if not self.path.is_file():
            raise RuntimeError(f"RGBD recording does not exist: {self.path}")
        archive: Any | None = None
        try:
            archive = np.load(self.path, allow_pickle=False)
            if not hasattr(archive, "files"):
                raise RuntimeError(
                    "RGBD recording must be an .npz archive and must contain a 'bgr' array"
                )
            if "bgr" not in archive:
                raise RuntimeError("RGBD recording must contain a 'bgr' array")
            bgr = np.asarray(archive["bgr"])
            if bgr.ndim == 3:
                bgr = bgr[None, ...]
            if bgr.dtype != np.uint8 or bgr.ndim != 4 or bgr.shape[-1] != 3:
                raise RuntimeError("recording bgr must be N x H x W x 3 uint8")
            if bgr.shape[0] == 0:
                raise RuntimeError("recording bgr must contain at least one frame")
            depth = None
            if "depth_m" in archive:
                depth = np.asarray(archive["depth_m"])
                if depth.ndim == 2:
                    depth = depth[None, ...]
                if depth.ndim != 3 or depth.shape != bgr.shape[:3]:
                    raise RuntimeError("recording depth_m must match bgr frames")
            if "fps" in archive:
                self._fps = float(np.asarray(archive["fps"]).reshape(()))
                if not np.isfinite(self._fps) or self._fps <= 0:
                    raise RuntimeError("recording fps must be positive")
        except Exception:
            close = getattr(archive, "close", None)
            if callable(close):
                close()
            raise
        self._archive = archive
        self._bgr = bgr
        self._depth = depth
        self._index = 0
        self._next_frame_at = None

    def read(self) -> RgbdFrame:
        if self._archive is None or self._bgr is None:
            raise RuntimeError("RGBD recording is not open")
        if self._index >= len(self._bgr):
            if not self.loop:
                raise EndOfStream(f"RGBD recording {self.path} reached end of stream")
            self._index = 0
            self._next_frame_at = None
        if self.realtime:
            now = time.monotonic()
            if self._next_frame_at is not None and now < self._next_frame_at:
                time.sleep(self._next_frame_at - now)
            self._next_frame_at = max(now, self._next_frame_at or now) + 1.0 / self._fps
        index = self._index
        self._index += 1
        depth = None if self._depth is None else np.ascontiguousarray(self._depth[index])
        return RgbdFrame(
            np.ascontiguousarray(self._bgr[index]),
            depth,
            time.monotonic(),
            index + 1,
        ).validate()

    def close(self) -> None:
        archive, self._archive = self._archive, None
        self._bgr = None
        self._depth = None
        self._next_frame_at = None
        close = getattr(archive, "close", None)
        if callable(close):
            close()


class SyntheticFrameSource(FrameSource):
    """Deterministic moving RGBD scene for a zero-hardware smoke test."""

    label = "SYNTHETIC RGBD"

    def __init__(
        self,
        *,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        realtime: bool = True,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.realtime = realtime
        self._open = False
        self._sequence = 0
        self._next_frame_at: float | None = None

    def open(self) -> None:
        self._open = True
        self._next_frame_at = None

    def read(self) -> RgbdFrame:
        if not self._open:
            raise RuntimeError("synthetic source is not open")
        if self.realtime:
            now = time.monotonic()
            if self._next_frame_at is not None and now < self._next_frame_at:
                time.sleep(self._next_frame_at - now)
            self._next_frame_at = max(now, self._next_frame_at or now) + 1.0 / self.fps
        self._sequence += 1
        x = np.linspace(0.0, 1.0, self.width, dtype=np.float32)
        y = np.linspace(0.0, 1.0, self.height, dtype=np.float32)[:, None]
        shift = (self._sequence * 3) % max(1, self.width)
        blue = np.broadcast_to((255 * x).astype(np.uint8), (self.height, self.width))
        green = np.broadcast_to((255 * y).astype(np.uint8), (self.height, self.width))
        red = np.roll(blue, shift, axis=1)
        bgr = np.ascontiguousarray(np.stack((blue, green, red), axis=2))
        depth_m = np.broadcast_to(0.5 + 3.5 * x, (self.height, self.width)).copy()
        # A stable invalid stripe makes fail-closed holes visible in the demo.
        stripe = max(1, self.width // 32)
        depth_m[:, self.width // 2 - stripe : self.width // 2 + stripe] = 0.0
        return RgbdFrame(
            bgr,
            np.ascontiguousarray(depth_m, dtype=np.float32),
            time.monotonic(),
            self._sequence,
        )

    def close(self) -> None:
        self._open = False
        self._next_frame_at = None


def _validate_bgr(bgr: Any) -> np.ndarray:
    if not isinstance(bgr, np.ndarray):
        raise RuntimeError("camera returned a non-numpy frame")
    if bgr.dtype != np.uint8 or bgr.ndim != 3 or bgr.shape[2] != 3:
        raise RuntimeError("camera frame must be an HxWx3 uint8 BGR array")
    if bgr.shape[0] == 0 or bgr.shape[1] == 0:
        raise RuntimeError("camera frame dimensions must be non-zero")
    return np.ascontiguousarray(bgr)
