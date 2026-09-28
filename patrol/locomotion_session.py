"""Fail-closed sender for the owned relay protocol; no automatic re-claim."""
import json
import math
from pathlib import Path
import socket
import threading
import time
import uuid

import yaml


class RelaySession:
    def __init__(self, host, port):
        with Path(__file__).with_name("ownership.yaml").open(encoding="utf-8") as stream:
            self.timeout = yaml.safe_load(stream)["request_timeout_s"]
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("invalid relay request timeout")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.lock = threading.RLock()
        self.failed = False
        self.epoch = self.session = self.generation = None
        self.sequence = -1
        self.enabled = False
        try:
            self.sock.connect((host, port))  # Kernel filters response source endpoint.
            reply = self._exchange("discover")
            self.epoch = self._id(reply, "relay_epoch")
            nonce = str(uuid.uuid4())
            reply = self._exchange("claim", client_nonce=nonce)
            if reply.get("client_nonce") != nonce or reply.get("state") != "MOVEMENT_HELD":
                raise RuntimeError("invalid claim response")
            self.session = self._id(reply, "owner_session")
            self.generation = self._id(reply, "movement_generation")
        except BaseException:
            self.sock.close()
            raise

    @staticmethod
    def _id(reply, key):
        value = reply.get(key)
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise RuntimeError("invalid relay " + key)
        return value

    def _exchange(self, operation, **fields):
        request_id = str(uuid.uuid4())
        packet = {"protocol_version": 2, "operation": operation,
                  "request_id": request_id, **fields}
        if self.epoch is not None:
            packet["relay_epoch"] = self.epoch
        if self.session is not None:
            self.sequence += 1
            packet.update(owner_session=self.session, sequence=self.sequence,
                          movement_generation=self.generation)
        try:
            deadline = time.monotonic() + self.timeout
            self.sock.settimeout(self.timeout)
            self.sock.send(json.dumps(packet, allow_nan=False).encode())
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("relay response deadline exceeded")
                self.sock.settimeout(remaining)
                reply = json.loads(self.sock.recv(4096))
                if not isinstance(reply, dict):
                    raise RuntimeError("invalid relay response")
                if reply.get("request_id") != request_id:
                    continue  # A delayed unrelated reply cannot extend the deadline.
                if (type(reply.get("protocol_version")) is not int or
                        reply["protocol_version"] != 2 or reply.get("accepted") is not True):
                    raise RuntimeError(str(reply.get("error") or "relay rejected protected protocol"))
                if self.epoch is not None and reply.get("relay_epoch") != self.epoch:
                    raise RuntimeError("relay epoch changed")
                if self.session is not None:
                    expected = "MOVEMENT_HELD" if operation == "hold" else "MOVEMENT_ENABLED"
                    if (reply.get("owner_session") != self.session or
                            type(reply.get("sequence")) is not int or
                            reply["sequence"] != self.sequence or reply.get("state") != expected):
                        raise RuntimeError("invalid owned relay response")
                    generation = self._id(reply, "movement_generation")
                    if operation == "move" and generation != self.generation:
                        raise RuntimeError("movement generation changed")
                    if operation in {"enable", "hold"} and generation == self.generation:
                        raise RuntimeError("movement generation was not advanced")
                    self.generation = generation
                return reply
        except BaseException:
            self.failed = True
            self.enabled = False
            raise

    def enable(self):
        with self.lock:
            if self.failed:
                raise RuntimeError("relay session failure latched")
            if not self.enabled:
                self._exchange("enable")
                self.enabled = True

    def move(self, vx, vyaw):
        with self.lock:
            if self.failed or not self.enabled:
                raise RuntimeError("relay movement is not enabled")
            self._exchange("move", velocity={"vx": vx, "vy": 0.0, "vyaw": vyaw})

    def hold(self):
        with self.lock:
            self.enabled = False
            # A failed session may still attempt HOLD, but never enables again.
            self._exchange("hold")

    def close(self):
        self.sock.close()
