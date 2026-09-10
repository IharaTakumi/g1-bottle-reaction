from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from g1_bottle_reaction.game_vision.transport import (
    TeleImagerProcessedFrameSource,
    TeleImagerZmqPublisher,
    _decode_game_image,
    _load_teleimager_class,
)


class FakeTeleImagerBus:
    """Mimic TeleImager's latest-payload ring, including object identity."""

    def __init__(self) -> None:
        self.messages: dict[tuple[str, int], bytes | None] = {}
        self.publish_calls: list[tuple[int, str]] = []
        self.subscribe_calls: list[tuple[str, int, bool]] = []
        self.close_calls = 0

    def publish(self, data, port, host="0.0.0.0") -> None:
        assert isinstance(data, bytes)
        self.messages[(host, port)] = data
        self.publish_calls.append((port, host))

    def subscribe(self, host, port, request_bgr=False):
        self.subscribe_calls.append((host, port, request_bgr))
        assert request_bgr is False
        return SimpleNamespace(jpg=self.messages.get(("0.0.0.0", port)))

    def close(self) -> None:
        self.close_calls += 1


def _publisher(bus: FakeTeleImagerBus, *, safety: bool = False):
    return TeleImagerZmqPublisher(
        game_port=45558,
        safety_port=45559 if safety else None,
        bind_host="0.0.0.0",
        webp_quality=90,
        manager=bus,
    )


def _receiver(bus: FakeTeleImagerBus, *, safety: bool = False):
    return TeleImagerProcessedFrameSource(
        "192.0.2.10",
        port=45558,
        safety_port=45559 if safety else None,
        connect_timeout_seconds=0.05,
        stale_timeout_seconds=0.02,
        poll_interval_seconds=0.001,
        manager=bus,
    )


def test_processed_webp_round_trip_reapplies_hidden_alpha_exactly() -> None:
    bus = FakeTeleImagerBus()
    publisher = _publisher(bus)
    receiver = _receiver(bus)
    expected = np.full((32, 40, 3), (20, 100, 220), dtype=np.uint8)
    expected[:, 25:] = 0

    receiver.open()
    publisher.open()
    publisher.publish(expected)
    frame = receiver.read()
    publisher.close()
    receiver.close()

    encoded = np.frombuffer(bus.messages[("0.0.0.0", 45558)], dtype=np.uint8)
    transported = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    assert transported.shape == (32, 40, 4)
    assert frame.depth_m is None
    assert receiver.is_prefiltered_game
    assert frame.bgr.shape == expected.shape
    assert np.count_nonzero(frame.bgr[:, 25:]) == 0
    assert np.allclose(frame.bgr[16, 10], expected[16, 10], atol=12)
    assert bus.publish_calls == [(45558, "0.0.0.0")]
    assert all(not request_bgr for _, _, request_bgr in bus.subscribe_calls)
    assert bus.close_calls == 2


def test_same_cached_payload_is_not_counted_as_a_fresh_frame() -> None:
    bus = FakeTeleImagerBus()
    publisher = _publisher(bus)
    receiver = _receiver(bus)
    publisher.open()
    receiver.open()
    image = np.full((8, 10, 3), 100, dtype=np.uint8)
    publisher.publish(image)
    assert receiver.read().sequence == 1

    with pytest.raises(RuntimeError, match="no fresh game G1 frame"):
        receiver.read()

    # A newly published packet is accepted even when its decoded content is
    # identical to the previous frame.
    publisher.publish(image)
    assert receiver.read().sequence == 2
    publisher.close()
    receiver.close()


def test_disconnect_none_never_replays_the_last_frame() -> None:
    bus = FakeTeleImagerBus()
    publisher = _publisher(bus)
    receiver = _receiver(bus)
    publisher.open()
    receiver.open()
    publisher.publish(np.full((8, 10, 3), 90, dtype=np.uint8))
    receiver.read()
    bus.messages[("0.0.0.0", 45558)] = None
    with pytest.raises(RuntimeError, match="no fresh game G1 frame"):
        receiver.read()
    publisher.close()
    receiver.close()


def test_publisher_and_receiver_fan_out_raw_safety_stream() -> None:
    bus = FakeTeleImagerBus()
    publisher = _publisher(bus, safety=True)
    receiver = _receiver(bus, safety=True)
    game = np.zeros((16, 20, 3), dtype=np.uint8)
    game[:, :8] = (20, 100, 220)
    safety = np.full((16, 20, 3), 210, dtype=np.uint8)
    publisher.open()
    receiver.open()
    publisher.publish(game, safety)
    frame = receiver.read()
    publisher.close()
    receiver.close()

    assert [port for port, _ in bus.publish_calls] == [45558, 45559]
    assert frame.safety_bgr is not None
    assert frame.safety_bgr.shape == safety.shape
    assert np.allclose(frame.safety_bgr[8, 10], safety[8, 10], atol=12)


def test_game_payload_on_safety_port_is_rejected_instead_of_mislabeled() -> None:
    bus = FakeTeleImagerBus()
    publisher = _publisher(bus)
    receiver = _receiver(bus, safety=True)
    publisher.open()
    receiver.open()
    publisher.publish(np.full((8, 10, 3), 100, dtype=np.uint8))
    bus.messages[("0.0.0.0", 45559)] = bus.messages[("0.0.0.0", 45558)]
    with pytest.raises(RuntimeError, match="expected JPEG"):
        receiver.read()
    publisher.close()
    receiver.close()


def test_nonbinary_game_alpha_fails_closed() -> None:
    bgra = np.full((8, 10, 4), 100, dtype=np.uint8)
    bgra[:, :, 3] = 128
    ok, encoded = cv2.imencode(
        ".webp", bgra, [cv2.IMWRITE_WEBP_QUALITY, 90]
    )
    assert ok
    decoded = _decode_game_image(encoded.tobytes())
    assert np.count_nonzero(decoded) == 0


def test_missing_safety_frame_does_not_half_publish_game() -> None:
    bus = FakeTeleImagerBus()
    publisher = _publisher(bus, safety=True)
    publisher.open()
    with pytest.raises(RuntimeError, match="safety frame is required"):
        publisher.publish(np.zeros((8, 10, 3), dtype=np.uint8))
    publisher.close()
    assert bus.publish_calls == []


def test_subscriber_open_failure_closes_started_manager() -> None:
    class FailingBus(FakeTeleImagerBus):
        def subscribe(self, host, port, request_bgr=False):
            raise RuntimeError("worker failed")

    bus = FailingBus()
    receiver = _receiver(bus)
    with pytest.raises(RuntimeError, match="worker failed"):
        receiver.open()
    assert bus.close_calls == 1
    assert receiver._manager is None


def test_teleimager_import_exit_becomes_a_clean_runtime_error(monkeypatch) -> None:
    def exits(module_name):
        raise SystemExit(1)

    monkeypatch.setattr(
        "g1_bottle_reaction.game_vision.transport.importlib.import_module", exits
    )
    with pytest.raises(RuntimeError, match="approved Unitree TeleImager environment"):
        _load_teleimager_class("ZMQ_SubscriberManager")


@pytest.mark.parametrize("port", [0, 65536])
def test_transport_rejects_invalid_ports(port) -> None:
    with pytest.raises(ValueError, match="port"):
        TeleImagerZmqPublisher(game_port=port, manager=FakeTeleImagerBus())


@pytest.mark.parametrize("quality", [0, 101])
def test_transport_rejects_invalid_webp_quality(quality) -> None:
    with pytest.raises(ValueError, match="quality"):
        TeleImagerZmqPublisher(webp_quality=quality, manager=FakeTeleImagerBus())


def test_publisher_default_bind_is_loopback_only() -> None:
    publisher = TeleImagerZmqPublisher(manager=FakeTeleImagerBus())
    assert publisher.bind_host == "127.0.0.1"
