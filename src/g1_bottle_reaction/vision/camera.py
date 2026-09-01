from __future__ import annotations

from abc import ABC, abstractmethod
import logging
import time
from typing import Any, Callable

import numpy as np

LOGGER = logging.getLogger(__name__)


class CameraSource(ABC):
    """Source of OpenCV-compatible uint8 BGR frames."""

    @abstractmethod
    def open(self) -> None:
        """Acquire source-side resources."""

    @abstractmethod
    def read(self) -> np.ndarray:
        """Return one HxWx3 uint8 BGR frame or raise a useful error."""

    @abstractmethod
    def close(self) -> None:
        """Release source-side resources."""


class OpenCVCameraSource(CameraSource):
    def __init__(self, device: int = 0) -> None:
        self.device = device
        self._capture: Any | None = None

    def open(self) -> None:
        if self._capture is not None:
            return
        import cv2

        capture = cv2.VideoCapture(self.device)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(f"Could not open OpenCV camera {self.device}")
        self._capture = capture

    def read(self) -> np.ndarray:
        if self._capture is None:
            raise RuntimeError("OpenCV camera source is not open")
        ok, frame = self._capture.read()
        if not ok or frame is None:
            raise RuntimeError(f"OpenCV camera {self.device} frame capture failed")
        return validate_bgr_frame(frame)

    def close(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.release()


class G1CameraSource(CameraSource):
    """G1 standard videohub_pc4 stream through the official VideoClient."""

    def __init__(
        self,
        network_interface: str | None,
        *,
        network_address: str | None = None,
        timeout_seconds: float = 3.0,
        read_attempts: int = 3,
        retry_delay_seconds: float = 0.1,
        runtime: Any | None = None,
    ) -> None:
        if not (network_interface or network_address):
            raise ValueError(
                "--network-interface or --network-address is required for G1 camera"
            )
        if timeout_seconds <= 0:
            raise ValueError("G1 VideoClient timeout must be positive")
        if read_attempts < 1:
            raise ValueError("G1 camera read attempts must be at least 1")
        self.network_interface = network_interface
        self.network_address = network_address
        self.timeout_seconds = timeout_seconds
        self.read_attempts = read_attempts
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self._runtime = runtime
        self._client: Any | None = None

    def open(self) -> None:
        if self._client is not None:
            return
        if self._runtime is None:
            from g1_bottle_reaction.adapters.g1_robot import DEFAULT_UNITREE_RUNTIME

            self._runtime = DEFAULT_UNITREE_RUNTIME
        if self.network_address is None:
            self._client = self._runtime.create_video_client(
                self.network_interface, self.timeout_seconds
            )
        else:
            self._client = self._runtime.create_video_client(
                self.network_interface,
                self.timeout_seconds,
                self.network_address,
            )

    def read(self) -> np.ndarray:
        if self._client is None:
            raise RuntimeError("G1 VideoClient camera source is not open")
        last_error: Exception | None = None
        for attempt in range(1, self.read_attempts + 1):
            try:
                result = self._client.GetImageSample()
                if not isinstance(result, tuple) or len(result) != 2:
                    raise RuntimeError(
                        "VideoClient.GetImageSample returned an invalid response"
                    )
                code, data = result
                if code != 0:
                    raise RuntimeError(
                        f"VideoClient.GetImageSample failed with return code {code}"
                    )
                return _decode_jpeg_bgr(data)
            except Exception as exc:
                last_error = exc
            if attempt < self.read_attempts:
                LOGGER.warning(
                    "G1 VideoClient read failed (%s); retrying %d/%d",
                    last_error,
                    attempt,
                    self.read_attempts,
                )
                time.sleep(self.retry_delay_seconds)
        raise RuntimeError(
            "G1 videohub_pc4 camera is unavailable after "
            f"{self.read_attempts} attempt(s): {last_error}"
        ) from last_error

    def close(self) -> None:
        self._client = None


class TeleImagerCameraSource(CameraSource):
    """Legacy TeleImager source; not the primary G1 camera path."""

    def __init__(
        self,
        host: str,
        *,
        frame_timeout_seconds: float = 3.0,
        reconnect_attempts: int = 3,
        reconnect_delay_seconds: float = 0.25,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not host:
            raise ValueError("TeleImager server IP is required")
        if frame_timeout_seconds <= 0:
            raise ValueError("TeleImager frame timeout must be positive")
        if reconnect_attempts < 1:
            raise ValueError("TeleImager reconnect attempts must be at least 1")
        self.host = host
        self.frame_timeout_seconds = frame_timeout_seconds
        self.reconnect_attempts = reconnect_attempts
        self.reconnect_delay_seconds = max(0.0, reconnect_delay_seconds)
        self._client_factory = client_factory
        self._client: Any | None = None

    def open(self) -> None:
        if self._client is not None:
            return
        factory = self._client_factory
        if factory is None:
            try:
                from teleimager.image_client import ImageClient
            except ImportError as exc:
                raise RuntimeError(
                    "TeleImager is required for --camera-source g1-teleimager"
                ) from exc
            factory = ImageClient
        try:
            self._client = factory(host=self.host, request_bgr=True)
        except Exception as exc:
            raise RuntimeError(
                f"Could not connect to legacy G1 TeleImager at {self.host}: {exc}"
            ) from exc

    def read(self) -> np.ndarray:
        last_error: Exception | None = None
        for attempt in range(1, self.reconnect_attempts + 1):
            try:
                self.open()
                deadline = time.monotonic() + self.frame_timeout_seconds
                while time.monotonic() < deadline:
                    teleimage = self._client.get_head_frame()
                    frame = getattr(teleimage, "bgr", None)
                    if frame is not None:
                        return validate_bgr_frame(frame)
                    time.sleep(0.01)
                last_error = TimeoutError(
                    f"no BGR head-camera frame within {self.frame_timeout_seconds:.1f}s"
                )
            except Exception as exc:
                last_error = exc
            self.close()
            if attempt < self.reconnect_attempts:
                LOGGER.warning(
                    "Legacy TeleImager read failed (%s); reconnecting %d/%d",
                    last_error,
                    attempt,
                    self.reconnect_attempts,
                )
                time.sleep(self.reconnect_delay_seconds)
        raise RuntimeError(
            f"Legacy G1 TeleImager at {self.host} is unavailable after "
            f"{self.reconnect_attempts} attempt(s): {last_error}"
        ) from last_error

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                LOGGER.warning("Legacy TeleImager close failed: %s", exc)


def _decode_jpeg_bgr(data: Any) -> np.ndarray:
    import cv2

    try:
        encoded = bytes(data)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("VideoClient returned invalid JPEG byte data") from exc
    if not encoded:
        raise RuntimeError("VideoClient returned empty JPEG data")
    array = np.frombuffer(encoded, dtype=np.uint8)
    frame = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("OpenCV could not decode the VideoClient JPEG frame")
    return validate_bgr_frame(frame)


def validate_bgr_frame(frame: Any) -> np.ndarray:
    if not isinstance(frame, np.ndarray):
        raise RuntimeError("Camera returned a non-numpy frame")
    if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
        raise RuntimeError(
            "Camera frame must be an HxWx3 uint8 OpenCV BGR array"
        )
    return np.ascontiguousarray(frame)
