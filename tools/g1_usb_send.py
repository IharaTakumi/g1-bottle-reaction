#!/usr/bin/env python3
"""G1-side stdlib supervisor for existing GStreamer. No SDK or installs.

May be passed to python3 -c over SSH without creating a robot-side file.
--watch-stdin closes the pipeline when the SSH client disconnects or sends stop.
"""
import argparse
import ipaddress
import os
from pathlib import Path
import select
import shutil
import signal
import resource
import subprocess
import sys
import time

DEVICE = "auto"


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device", default=DEVICE,
                   help="stable /dev/v4l/by-id path, or auto for one non-RealSense USB camera")
    p.add_argument("--dest", required=True, type=ipaddress.IPv4Address)
    p.add_argument("--bind", required=True, type=ipaddress.IPv4Address)
    p.add_argument("--port", type=int, default=56000)
    p.add_argument("--width", type=int, choices=(640, 1280), default=1280)
    p.add_argument("--watch-stdin", action="store_true")
    p.add_argument("--duration", type=int, default=3600)
    return p


def command(args):
    if not 1024 <= args.port <= 65535 or args.duration <= 0:
        raise ValueError("invalid port or duration")
    height = 720 if args.width == 1280 else 480
    return ["gst-launch-1.0", "-e", "-v", "v4l2src", "device=" + args.device,
            "do-timestamp=true", "!",
            "image/jpeg,width=%d,height=%d,framerate=30/1" % (args.width, height), "!",
            "queue", "max-size-buffers=1", "max-size-bytes=0", "max-size-time=0", "leaky=downstream", "!",
            "jpegparse", "!", "rtpjpegpay", "pt=26", "mtu=1200", "!", "udpsink",
            "host=" + str(args.dest), "bind-address=" + str(args.bind), "port=" + str(args.port),
            "sync=false", "async=false"]


def detect_device(requested, by_id=Path("/dev/v4l/by-id")):
    if requested != "auto":
        return Path(requested).resolve(strict=True)
    candidates = sorted(
        path for path in by_id.glob("usb-*-video-index0")
        if "realsense" not in path.name.lower()
    )
    if len(candidates) != 1:
        listed = ", ".join(str(path) for path in candidates) or "none"
        raise RuntimeError(
            "USB camera auto-detection requires exactly one non-RealSense "
            "video-index0 device; found: " + listed
        )
    return candidates[0].resolve(strict=True)


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    args = parser().parse_args()
    device = detect_device(args.device)
    args.device = str(device)
    cmd = command(args)
    sys_device = Path("/sys/class/video4linux") / device.name / "device"
    # Prevent accidental capture of the existing RealSense/videohub camera.
    parents = [sys_device.resolve()] + list(sys_device.resolve().parents)
    usb = next((p for p in parents if (p / "idVendor").exists()), None)
    if usb is None:
        raise RuntimeError("Selected video node is not a USB camera")
    vendor = (usb / "idVendor").read_text().strip().lower()
    product = (usb / "idProduct").read_text().strip().lower()
    if vendor == "8086":
        raise RuntimeError("Refusing to use the G1 RealSense as the head USB camera")
    print("Selected USB camera:", device, "USB ID", vendor + ":" + product, flush=True)
    if not shutil.which("gst-launch-1.0"):
        raise RuntimeError("Existing GStreamer is required; do not install system packages")
    env = dict(os.environ, GST_REGISTRY="/dev/null", GST_REGISTRY_UPDATE="no")
    print("USB sender:", " ".join(cmd), flush=True)
    process = subprocess.Popen(cmd, env=env)
    started = time.monotonic()
    try:
        while process.poll() is None and time.monotonic() - started < args.duration:
            if args.watch_stdin:
                if select.select([sys.stdin], [], [], 0.25)[0]:
                    # EOF or any stop message terminates only this sender.
                    sys.stdin.readline()
                    break
            else:
                time.sleep(0.25)
        code = process.poll()
        return code if code is not None else 0
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print("USB SENDER ERROR:", exc, file=sys.stderr)
        sys.exit(2)
