"""Owned v3 STOP transactions. RPC confirmation is NOT physical stop."""
import json
import math
import socket
import time
import uuid

try:
    from .stop_rpc import RPC_SUCCESS
except ImportError:
    from stop_rpc import RPC_SUCCESS

VERSION = 3


def identifier(value):
    return isinstance(value, str) and str(uuid.UUID(value)) == value


class RelayOwnership:
    """Single receive-loop owner; no reset, release, takeover or legacy fallback."""
    def __init__(self, client, *, watchdog_timeout=.40, allow_reverse=False,
                 clock=time.monotonic):
        self.client = client
        self.clock = clock
        self.watchdog_timeout = watchdog_timeout
        self.allow_reverse = allow_reverse
        self.epoch = str(uuid.uuid4())
        self.session = None
        self.peer = None
        self.generation = str(uuid.uuid4())
        self.sequence = -1
        self.state = "MOVEMENT_HELD"
        self.last_move = None
        self.error = None
        self.stop_rpc_status = None
        self.raw_rpc_code = None

    def _stop_rpc(self):
        self.stop_rpc_status = "STOP_RELAY_RECEIVED"
        self.raw_rpc_code = None
        try:
            raw = self.client.StopMove()
            # Preserve known JSON scalars; arbitrary unknown objects are not wire codes.
            self.raw_rpc_code = raw if (type(raw) in (int, str, bool) or
                (type(raw) is float and math.isfinite(raw))) else None
            if type(raw) is not int or raw != RPC_SUCCESS:
                raise RuntimeError("STOP RPC result unconfirmed: " + repr(raw))
            self.stop_rpc_status = "STOP_RPC_CONFIRMED"
        except Exception as exc:
            self.stop_rpc_status = "STOP_UNCONFIRMED"
            self.state = "FAULT"
            self.error = str(exc)

    def fault(self, reason):
        if self.state == "FAULT":
            return
        self.state = "FAULT"  # Latch before an SDK call that may raise/block.
        self.generation = str(uuid.uuid4())
        self.error = reason
        self._stop_rpc()

    def tick(self):
        if (self.state == "MOVEMENT_ENABLED" and
                self.clock() - self.last_move >= self.watchdog_timeout):
            self.fault("Move watchdog expired")

    def handle(self, packet, peer):
        self.tick()  # Invalid traffic must not starve the existing watchdog.
        request_id = packet.get("request_id") if isinstance(packet, dict) else None
        response = {"protocol_version": VERSION, "relay_epoch": self.epoch,
                    "request_id": request_id, "accepted": False,
                    "operation": packet.get("operation") if isinstance(packet, dict) else None}
        try:
            if (not isinstance(packet, dict) or
                    type(packet.get("protocol_version")) is not int or
                    packet["protocol_version"] != VERSION or not identifier(request_id)):
                raise ValueError("protected protocol required")
            operation = packet.get("operation")
            if operation == "discover":
                response.update(accepted=True, state=self.state)
                return response
            if packet.get("relay_epoch") != self.epoch:
                raise ValueError("wrong relay epoch")
            if self.state == "FAULT":
                raise ValueError("relay fault latched")
            if operation == "claim":
                if self.session is not None or not identifier(packet.get("client_nonce")):
                    raise ValueError("owner exists or invalid claim nonce")
                self.peer = peer
                self.session = str(uuid.uuid4())
                response["client_nonce"] = packet["client_nonce"]
            else:
                if (self.session is None or peer != self.peer or
                        packet.get("owner_session") != self.session):
                    raise ValueError("not current owner")
                sequence = packet.get("sequence")
                if type(sequence) is not int or sequence < 0 or sequence <= self.sequence:
                    raise ValueError("stale or invalid sequence")
                if packet.get("movement_generation") != self.generation:
                    raise ValueError("wrong movement generation")
                if operation not in {"enable", "hold", "move"}:
                    raise ValueError("unknown owned operation")
                if operation == "enable" and self.state != "MOVEMENT_HELD":
                    raise ValueError("movement already enabled")
                if operation == "enable" and self.stop_rpc_status != "STOP_RPC_CONFIRMED":
                    raise ValueError("confirmed STOP required before enable")
                if operation == "hold" and request_id == self.generation:
                    raise ValueError("HOLD must advance movement generation")
                if operation == "move":
                    if self.state != "MOVEMENT_ENABLED":
                        raise ValueError("movement held")
                    velocity = packet.get("velocity")
                    if not isinstance(velocity, dict):
                        raise ValueError("missing velocity")
                    values = tuple(velocity.get(k) for k in ("vx", "vy", "vyaw"))
                    if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
                        raise ValueError("invalid velocity")
                    vx, vy, vyaw = values
                    if (abs(vx) > .30 or abs(vy) > .20 or abs(vyaw) > .50 or
                            (vx < 0 and not self.allow_reverse)):
                        raise ValueError("velocity exceeds relay limits")
                # Commit only after every validation. Rejections change nothing.
                self.sequence = sequence
                if operation == "enable":
                    self.generation = str(uuid.uuid4())
                    self.state = "MOVEMENT_ENABLED"
                    self.stop_rpc_status = None
                    self.last_move = self.clock()  # First Move also has a deadline.
                elif operation == "hold":
                    self.state = "MOVEMENT_HELD"
                    # Bind the new HOLD generation to the existing transaction UUID.
                    # Client can verify it exactly; no second identity is needed.
                    self.generation = request_id
                    self._stop_rpc()
                else:
                    self.last_move = self.clock()
                    try:
                        self.client.Move(vx, vy, vyaw, continous_move=True)
                    except Exception as exc:
                        self.fault("SDK Move exception: " + str(exc))
                        raise RuntimeError(self.error) from exc
            response.update(accepted=self.state != "FAULT", owner_session=self.session,
                            movement_generation=self.generation, state=self.state,
                            sequence=self.sequence)
        except (ValueError, TypeError, AttributeError) as exc:
            response["error"] = str(exc)
        except Exception as exc:
            self.fault("SDK exception: " + str(exc))
            response["error"] = self.error
        response.update(relay_state=self.state, stop_rpc_status=self.stop_rpc_status,
                        raw_rpc_code=self.raw_rpc_code)
        if self.state == "FAULT":
            response["error"] = self.error
        return response


def serve_datagram(sock, ownership):
    """One local/production UDP receive step; SDK is injected by the caller."""
    ownership.tick()
    try:
        payload, peer = sock.recvfrom(4096)
    except (socket.timeout, TimeoutError):  # socket.timeout is distinct on Python 3.8.
        ownership.tick()
        return
    try:
        packet = json.loads(payload)
    except (UnicodeError, ValueError):
        packet = None
    reply = ownership.handle(packet, peer)
    sock.sendto(json.dumps(reply, allow_nan=False).encode(), peer)
