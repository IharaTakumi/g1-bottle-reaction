"""Local-only subprocess fixture: production control plane, recording fake robot."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "patrol")]
from control_ipc import PatrolControlServer
from control_lease import load_control_timing
from locomotion_adapter import DryRunLocomotionAdapter
from patrol_controller import PatrolController
from run_patrol import AlwaysClear
from scripts import run_integrated_demo as supervisor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("patrol", "supervisor"))
    parser.add_argument("socket")
    parser.add_argument("events", type=Path)
    parser.add_argument("lease_id")
    args = parser.parse_args()
    write_lock = threading.Lock()

    def record(event, **details):
        with write_lock, args.events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "time": time.monotonic(), **details}) + "\n")

    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    timing = load_control_timing()
    if args.role == "supervisor":
        supervisor.PATROL_CONTROL_SOCKET = Path(args.socket)
        try:
            supervisor.refresh_patrol_lease(args.lease_id, timing.heartbeat_interval_s)
            supervisor.patrol_request_while_heartbeating(
                {"operation": "resume"}, args.lease_id, timing.heartbeat_interval_s)
            while True:
                supervisor.refresh_patrol_lease(args.lease_id, timing.heartbeat_interval_s)
                record("heartbeat")
                time.sleep(timing.heartbeat_interval_s)
        finally:
            record("supervisor_cleanup")
        return

    class RecordingLocomotion(DryRunLocomotionAdapter):
        def move(self, vx, vyaw=0):
            super().move(vx, vyaw)
            record("move", fault=controller.control_status()["control_fault"])

        def stop(self):
            super().stop()
            record("stop", fault=controller.control_status()["control_fault"])

    controller = PatrolController(
        RecordingLocomotion(), AlwaysClear(), emit=lambda _: None,
        lease_id=args.lease_id, lease_timeout_s=timing.lease_timeout_s)
    server = PatrolControlServer(args.socket, controller)

    def patrol_loop():
        try:
            controller.run(cycles=1)
        except RuntimeError as exc:
            record("fault", error=str(exc), status=controller.control_status())
        finally:
            record("loop_finished")

    loop = threading.Thread(target=patrol_loop, daemon=True)
    try:
        server.start()
        loop.start()
        record("ready")
        # Keep the process and IPC alive after the control loop self-stops,
        # so the parent can inspect the latch and attempt late requests.
        while True:
            time.sleep(.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        loop.join(timeout=2)


if __name__ == "__main__":
    main()
