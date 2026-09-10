#!/usr/bin/env python3
"""One cached WAV through existing G1AudioOutput; no synthesis or motion APIs."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import resource
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", type=Path)
    parser.add_argument("--serve", action="store_true", help="keep one AudioClient for successive local WAV requests")
    parser.add_argument("--check-volume", action="store_true", help="check audio readiness/volume; read-only unless --volume is given")
    parser.add_argument("--volume", type=int, choices=range(101), help="explicitly set volume; otherwise preserve it")
    parser.add_argument("--interface")
    args = parser.parse_args()
    if not args.wav and not args.check_volume and not args.serve:
        parser.error("--wav, --serve or --check-volume is required")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    os.environ.pop("CYCLONEDDS_URI", None)
    native = ROOT / ".runtime/cyclonedds"
    if (native / "lib/libddsc.so").is_file():
        os.environ["CYCLONEDDS_HOME"] = str(native)
    from g1_camera_minimal import inspect_interfaces, select_interface
    from g1_bottle_reaction.adapters.g1_robot import UnitreeSdkRuntime
    from g1_bottle_reaction.adapters.g1_audio import G1AudioOutput

    class AudioRuntime(UnitreeSdkRuntime):
        def initialize_channel(self, network_interface, network_address=None):
            if network_address is not None:
                raise ValueError("Only verified wired interface mode is supported")
            # Reuse the proven process-local trace-free initializer, no SDK edits.
            initialize, _ = self.load_camera_symbols()
            initialize(0, network_interface)

        def create_audio_client(self, network_interface, timeout, network_address=None):
            client = super().create_audio_client(network_interface, timeout, network_address)
            # A newly created DDS writer may not yet have discovered the server.
            # Retry only this read-only query; never retry a PlayStream request.
            deadline = time.monotonic() + 10
            while True:
                code, volume = client.GetVolume()
                if code == 0:
                    print(f"G1 audio ready; current volume: {volume}", flush=True)
                    return client
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"G1 audio readiness GetVolume failed: {code}")
                time.sleep(.1)

    interface, _ = select_interface(inspect_interfaces(), args.interface)
    # Existing converter + official AudioClient. Keep the G1's current volume.
    # 0.5s PCM chunks with >=0.5s pacing avoid truncating multi-second cached WAVs.
    output = G1AudioOutput(interface, volume=args.volume, timeout_seconds=1., chunk_bytes=16000,
                           chunk_delay_seconds=.5, runtime=AudioRuntime())
    if args.serve:
        with contextlib.redirect_stdout(sys.stderr):
            output.initialize()
        print(json.dumps({"status": "ready"}), flush=True)
        for line in sys.stdin:
            path = Path(json.loads(line)["wav"])
            with contextlib.redirect_stdout(sys.stderr):
                output.play_wav(path)
            print(json.dumps({"status": "played"}), flush=True)
        return
    if args.check_volume:
        output.initialize()
        return
    output.play_wav(args.wav)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"G1 cached audio error: {exc}", file=sys.stderr, flush=True)
        sys.exit(2)
