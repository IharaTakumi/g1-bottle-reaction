from __future__ import annotations

import importlib
import math
import time
from typing import Any

import numpy as np

from .frames import RgbdFrame
from .sources import FrameSource, _validate_bgr


DEFAULT_GAME_PORT = 55558
DEFAULT_SAFETY_PORT = 55559


class TeleImagerZmqPublisher:
    """Publish images through TeleImager's public latest-frame ZMQ API.

    The game payload is a standard WebP image with a lossless binary alpha
    channel. RGB can remain bandwidth-efficient and lossy, while the receiver
    reapplies alpha after decoding so hidden pixels stay exactly black. Both
    the TeleImager version pinned by xr_teleoperate and current TeleImager
    expose ``ZMQ_PublisherManager``. Import remains lazy so this project never
    selects or replaces that package.
    """

    codec = "WebP+alpha"
    safety_codec = "JPEG"

    def __init__(
        self,
        *,
        game_port: int = DEFAULT_GAME_PORT,
        safety_port: int | None = None,
        bind_host: str = "127.0.0.1",
        webp_quality: int = 80,
        manager: Any | None = None,
    ) -> None:
        _validate_port(game_port, "game port")
        if safety_port is not None:
            _validate_port(safety_port, "safety port")
            if safety_port == game_port:
                raise ValueError("game and safety TeleImager ports must differ")
        if isinstance(webp_quality, bool) or not isinstance(webp_quality, int):
            raise ValueError("WebP quality must be an integer between 1 and 100")
        if not 1 <= webp_quality <= 100:
            raise ValueError("WebP quality must be between 1 and 100")
        if not bind_host or not bind_host.strip():
            raise ValueError("TeleImager bind host is required")
        self.game_port = game_port
        self.safety_port = safety_port
        self.bind_host = bind_host
        self.webp_quality = webp_quality
        self._manager = manager
        self._open = False

    def open(self) -> None:
        if self._open:
            return
        _verify_webp_alpha_support()
        if self._manager is None:
            manager_class = _load_teleimager_class("ZMQ_PublisherManager")
            self._manager = manager_class.get_instance()
        self._open = True

    def publish(self, game_bgr: np.ndarray, safety_bgr: np.ndarray | None = None) -> None:
        if not self._open or self._manager is None:
            raise RuntimeError("TeleImager publisher is not open")
        if self.safety_port is not None and safety_bgr is None:
            raise RuntimeError("a safety frame is required when safety publishing is enabled")

        # Validate and encode every requested payload before publishing either
        # one. A bad safety frame must not leave a half-published pair behind.
        game_payload = _encode_game_webp(game_bgr, self.webp_quality)
        safety_payload = (
            _encode_safety_jpeg(safety_bgr, self.webp_quality)
            if safety_bgr is not None and self.safety_port is not None
            else None
        )
        self._manager.publish(game_payload, self.game_port, self.bind_host)
        if self.safety_port is not None and safety_payload is not None:
            self._manager.publish(safety_payload, self.safety_port, self.bind_host)

    def close(self) -> None:
        manager, self._manager = self._manager, None
        was_open, self._open = self._open, False
        if was_open and manager is not None:
            manager.close()


class TeleImagerProcessedFrameSource(FrameSource):
    """Receive a PC2-filtered game image and optional raw safety image.

    TeleImager's BGR decoder keeps its last decoded array after a network
    interruption. We therefore subscribe to raw encoded payloads, accept each
    payload object only once, and decode synchronously. No packet means no new
    frame; after the short timeout the viewer closes instead of replaying stale
    imagery as though it were live.
    """

    label = "G1 CAMERA"
    is_prefiltered_game = True

    def __init__(
        self,
        host: str = "192.168.123.164",
        *,
        port: int = DEFAULT_GAME_PORT,
        safety_port: int | None = None,
        connect_timeout_seconds: float = 3.0,
        stale_timeout_seconds: float = 0.5,
        poll_interval_seconds: float = 0.005,
        manager: Any | None = None,
    ) -> None:
        if not host or not host.strip():
            raise ValueError("G1 processed stream host is required")
        _validate_port(port, "G1 processed stream port")
        if safety_port is not None:
            _validate_port(safety_port, "G1 safety stream port")
            if safety_port == port:
                raise ValueError("game and safety TeleImager ports must differ")
        for name, value in (
            ("connect timeout", connect_timeout_seconds),
            ("stale timeout", stale_timeout_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"G1 processed stream {name} must be positive and finite")
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("G1 processed stream poll interval must be positive and finite")
        self.host = host
        self.port = port
        self.safety_port = safety_port
        self.connect_timeout_seconds = connect_timeout_seconds
        self.stale_timeout_seconds = stale_timeout_seconds
        self.poll_interval_seconds = max(0.001, poll_interval_seconds)
        self._manager = manager
        self._open = False
        self._sequence = 0
        self._last_payload_by_port: dict[int, object] = {}

    def open(self) -> None:
        if self._open:
            return
        if self._manager is None:
            manager_class = _load_teleimager_class("ZMQ_SubscriberManager")
            self._manager = manager_class.get_instance()
        try:
            # First subscribe creates TeleImager's CONFLATE/HWM=1 receive worker.
            self._manager.subscribe(self.host, self.port, request_bgr=False)
            if self.safety_port is not None:
                self._manager.subscribe(
                    self.host, self.safety_port, request_bgr=False
                )
        except Exception:
            manager, self._manager = self._manager, None
            if manager is not None:
                manager.close()
            raise
        self._open = True

    def read(self) -> RgbdFrame:
        if not self._open or self._manager is None:
            raise RuntimeError("G1 processed stream is not open")
        timeout_seconds = (
            self.connect_timeout_seconds if self._sequence == 0 else self.stale_timeout_seconds
        )
        deadline = time.monotonic() + timeout_seconds
        game_payload = self._wait_for_fresh_payload(
            self.port, deadline, "game", timeout_seconds
        )
        safety_bgr = None
        if self.safety_port is not None:
            safety_payload = self._wait_for_fresh_payload(
                self.safety_port, deadline, "safety", timeout_seconds
            )
            safety_bgr = _decode_safety_image(safety_payload)
        self._sequence += 1
        return RgbdFrame(
            _decode_game_image(game_payload),
            None,
            time.monotonic(),
            self._sequence,
            safety_bgr,
        ).validate()

    def close(self) -> None:
        manager, self._manager = self._manager, None
        was_open, self._open = self._open, False
        self._last_payload_by_port.clear()
        if was_open and manager is not None:
            manager.close()

    def _wait_for_fresh_payload(
        self,
        port: int,
        deadline: float,
        stream_name: str,
        timeout_seconds: float,
    ) -> bytes | bytearray | memoryview:
        assert self._manager is not None
        while time.monotonic() < deadline:
            teleimage = self._manager.subscribe(
                self.host, port, request_bgr=False
            )
            payload = getattr(teleimage, "jpg", None)
            if payload is not None:
                if not isinstance(payload, (bytes, bytearray, memoryview)):
                    raise RuntimeError(
                        f"TeleImager {stream_name} payload is not encoded image bytes"
                    )
                if payload is not self._last_payload_by_port.get(port):
                    self._last_payload_by_port[port] = payload
                    return payload
            time.sleep(self.poll_interval_seconds)
        raise RuntimeError(
            f"no fresh {stream_name} G1 frame from TeleImager "
            f"{self.host}:{port} within {timeout_seconds:.2f}s"
        )


def _load_teleimager_class(name: str) -> Any:
    errors: list[str] = []
    # Current official TeleImager renamed image_client.py to client.py. The
    # low-level public manager API is the same in both supported layouts.
    for module_name in ("teleimager.client", "teleimager.image_client"):
        try:
            module = importlib.import_module(module_name)
        except SystemExit as exc:
            # Current TeleImager exits during import when its native JPEG
            # runtime is absent. Convert that process-wide exit into the same
            # clean, actionable source error as an ordinary missing import.
            errors.append(f"{module_name}: initialization exited ({exc})")
            continue
        except Exception as exc:
            errors.append(f"{module_name}: {exc}")
            continue
        value = getattr(module, name, None)
        if value is not None:
            return value
        errors.append(f"{module_name}: {name} missing")
    raise RuntimeError(
        "an approved Unitree TeleImager environment is required for the processed "
        "G1 stream; do not replace the version pinned by xr_teleoperate. "
        + "; ".join(errors)
    )


def _encode_game_webp(bgr: np.ndarray, quality: int) -> bytes:
    import cv2

    frame = _validate_bgr(bgr)
    # Fully hidden pixels are already exactly zero. Mark every other pixel as
    # visible; classifying a genuinely black visible pixel as hidden is
    # conservative and cannot reveal anything outside the game mask.
    transport_bgr = frame.copy()
    alpha = np.where(np.any(transport_bgr != 0, axis=2), 255, 0).astype(np.uint8)
    # libwebp may omit an all-opaque alpha plane. A single conservative black
    # sentinel guarantees that every valid game payload carries the mask and
    # lets a safety-port mix-up fail closed at the receiver.
    transport_bgr[0, 0] = 0
    alpha[0, 0] = 0
    bgra = np.dstack((transport_bgr, alpha))
    ok, encoded = cv2.imencode(
        ".webp", bgra, [cv2.IMWRITE_WEBP_QUALITY, quality]
    )
    if not ok:
        raise RuntimeError("OpenCV could not encode the processed WebP game frame")
    return encoded.tobytes()


def _verify_webp_alpha_support() -> None:
    """Fail early if this OpenCV build drops or cannot decode WebP alpha."""

    import cv2

    sample = np.zeros((2, 2, 4), dtype=np.uint8)
    sample[:, :, :3] = 120
    sample[:, :, 3] = 255
    sample[0, 0] = 0
    ok, encoded = cv2.imencode(
        ".webp", sample, [cv2.IMWRITE_WEBP_QUALITY, 80]
    )
    decoded = (
        cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED) if ok and encoded is not None else None
    )
    if (
        decoded is None
        or decoded.dtype != np.uint8
        or decoded.shape != sample.shape
        or decoded[0, 0, 3] != 0
        or decoded[1, 1, 3] != 255
    ):
        raise RuntimeError(
            "this OpenCV build does not preserve the required WebP alpha mask"
        )


def _encode_safety_jpeg(bgr: np.ndarray, quality: int) -> bytes:
    import cv2

    frame = _validate_bgr(bgr)
    ok, encoded = cv2.imencode(
        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality]
    )
    if not ok:
        raise RuntimeError("OpenCV could not encode the safety JPEG frame")
    return encoded.tobytes()


def _decode_game_image(payload: bytes | bytearray | memoryview) -> np.ndarray:
    import cv2

    encoded = np.frombuffer(payload, dtype=np.uint8)
    bgra = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    if (
        bgra is None
        or bgra.dtype != np.uint8
        or bgra.ndim != 3
        or bgra.shape[2] != 4
    ):
        raise RuntimeError(
            "TeleImager game payload lacks the required hidden-pixel alpha mask; "
            "check that the receiver is using the game port"
        )
    bgr = np.ascontiguousarray(bgra[:, :, :3])
    # Only an explicitly fully visible pixel may survive. Intermediate or
    # malformed alpha values fail closed as hidden.
    bgr[bgra[:, :, 3] != 255] = 0
    return _validate_bgr(bgr)


def _decode_safety_image(payload: bytes | bytearray | memoryview) -> np.ndarray:
    import cv2

    payload_view = memoryview(payload)
    if len(payload_view) < 3 or payload_view[:3].tobytes() != b"\xff\xd8\xff":
        raise RuntimeError(
            "TeleImager safety payload is not the expected JPEG; check the safety port"
        )
    encoded = np.frombuffer(payload, dtype=np.uint8)
    bgr = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError("TeleImager safety payload is not a decodable image")
    return np.ascontiguousarray(_validate_bgr(bgr))


def _validate_port(port: int, name: str) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"{name} must be an integer between 1 and 65535")
