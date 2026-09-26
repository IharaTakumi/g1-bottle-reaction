from __future__ import annotations

import sys
from pathlib import Path

PATROL = Path(__file__).resolve().parents[1] / "patrol"
sys.path.insert(0, str(PATROL))

from control_ipc import PatrolControlServer, request


class Controller:
    def __init__(self):
        self.paused = False
        self.stopped = False

    def control_status(self):
        return {
            "state": "PAUSED" if self.paused else "FORWARD_OUT",
            "phase": "FORWARD_OUT",
            "paused": self.paused,
            "pause_pending": False,
            "pause_reason": "reaction" if self.paused else None,
            "stopped": self.stopped,
            "error": None,
            "progress_m": .4,
            "remaining_m": 1.6,
        }

    def request_reaction_pause(self, _reason, _timeout):
        self.paused = True
        return self.control_status()

    def resume(self):
        self.paused = False

    def stop(self):
        self.stopped = True
        self.paused = True


def test_minimal_pause_resume_status_stop_protocol(tmp_path):
    controller = Controller()
    socket_path = tmp_path / "patrol.sock"
    server = PatrolControlServer(socket_path, controller)
    server.start()
    try:
        assert request(socket_path, {"operation": "status"})["remaining_m"] == 1.6
        assert request(socket_path, {"operation": "pause"})["paused"] is True
        assert request(socket_path, {"operation": "resume"})["paused"] is False
        stopped = request(socket_path, {"operation": "stop"})
        assert stopped["stopped"] is True
    finally:
        server.close()
    assert not socket_path.exists()
