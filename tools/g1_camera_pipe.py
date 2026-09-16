#!/usr/bin/env python3
"""Isolated G1 SDK or local GStreamer reader; stdout carries framed images."""
import argparse
import contextlib
import ipaddress
import os
from pathlib import Path
import resource
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from g1_bottle_reaction.game_vision.camera_ipc import (
    BGR,
    receive_ssh_jpeg,
    send,
)


def g1(args):
    # Reuse the successful minimal viewer runtime, including trace-free DDS.
    from g1_camera_minimal import CameraRuntime, inspect_interfaces, select_interface
    from g1_bottle_reaction.vision.camera import G1CameraSource
    import cv2

    output = sys.stdout.buffer
    with contextlib.redirect_stdout(sys.stderr):
        nic, ips = select_interface(
            inspect_interfaces(), args.interface, args.g1_ip, allow_wireless=True
        )
        if args.g1_ip is not None:
            from g1_camera_minimal import verify_route
            verify_route(args.g1_ip, nic)
        print("G1 NIC:", nic, "IP:", ips, flush=True)
        source = G1CameraSource(nic, timeout_seconds=0.5, read_attempts=1, runtime=CameraRuntime())
        last = time.monotonic()
        try:
            source.open()
            last = time.monotonic()
            while True:
                try:
                    frame = source.read()
                except RuntimeError:
                    if time.monotonic() - last > 5:
                        raise
                    time.sleep(0.01)
                    continue
                stamp = time.monotonic()
                last = stamp
                oh, ow = frame.shape[:2]
                # Reduce only local display transport, preserving aspect ratio.
                if ow > 960:
                    frame = cv2.resize(frame, (960, round(oh * 960 / ow)), interpolation=cv2.INTER_AREA)
                h, w = frame.shape[:2]
                send(output, frame.tobytes(), stamp, w, h, ow, oh, BGR)
        finally:
            source.close()


def rtp(args):
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    pipeline = Gst.parse_launch(
        'udpsrc address=%s port=%d buffer-size=262144 '
        'caps="application/x-rtp,media=video,encoding-name=JPEG,clock-rate=90000,payload=26" '
        '! rtpjitterbuffer name=jitter latency=30 drop-on-latency=true do-lost=true '
        '! rtpjpegdepay ! jpegparse '
        '! appsink name=frames max-buffers=1 drop=true sync=false' % (args.bind, args.port))
    sink = pipeline.get_by_name("frames")
    jitter = pipeline.get_by_name("jitter")
    bus = pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)
    print("%s receiver: RTP/JPEG, UDP %s:%d, jitter=30ms, appsink=1" %
          (args.label, args.bind, args.port), file=sys.stderr, flush=True)
    try:
        while True:
            error = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if error:
                raise RuntimeError(str(error.parse_error()) if error.type == Gst.MessageType.ERROR else "GStreamer EOS")
            sample = sink.emit("try-pull-sample", 200 * Gst.MSECOND)
            if sample is None:
                continue
            stamp = time.monotonic()
            buffer = sample.get_buffer()
            stats = jitter.get_property("stats")
            lost = int(stats.get_value("num-lost") or 0)
            send(sys.stdout.buffer, buffer.extract_dup(0, buffer.get_size()), stamp, lost=lost)
    finally:
        pipeline.set_state(Gst.State.NULL)


def ssh_jpeg(args, *, popen_factory=subprocess.Popen, output=None):
    """Relay binary-framed JPEGs over SSH into the existing local IPC."""
    import inspect
    from g1_bottle_reaction.adapters.g1_robot import serve_remote_camera_sender

    source = inspect.getsource(serve_remote_camera_sender) + "\nserve_remote_camera_sender()\n"
    remote_argv = [
        "python3", "-u", "-B", "-c", source,
        "--stdout-framed", "--fps", str(args.fps),
        "--duration", str(args.duration),
    ]
    command = [
        "ssh", "-T", "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=2",
        "-o", "ServerAliveCountMax=2",
    ]
    if args.ssh_control:
        command += ["-S", args.ssh_control]
    command += ["--", args.ssh_target, shlex.join(remote_argv)]
    process = popen_factory(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    target = sys.stdout.buffer if output is None else output
    last_sequence = 0
    try:
        while True:
            payload, sequence = receive_ssh_jpeg(process.stdout)
            if sequence <= last_sequence or payload[:2] != b"\xff\xd8":
                raise ValueError("invalid SSH JPEG payload or sequence")
            last_sequence = sequence
            send(target, payload, time.monotonic())
    except EOFError:
        returncode = process.wait(timeout=5)
        if returncode:
            raise RuntimeError(f"SSH JPEG camera exited with code {returncode}")
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if process.stdout is not None:
            process.stdout.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("source", choices=("g1", "usb", "rtp", "ssh-jpeg"))
    p.add_argument("--interface")
    p.add_argument("--g1-ip", type=ipaddress.IPv4Address)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=56000)
    p.add_argument("--label", default="USB")
    p.add_argument("--ssh-target")
    p.add_argument("--ssh-control")
    p.add_argument("--fps", type=float, default=10.0)
    p.add_argument("--duration", type=float, default=3600.0)
    args = p.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if args.source == "ssh-jpeg":
        if not args.ssh_target or not 0 < args.fps <= 60 or args.duration <= 0:
            raise ValueError("invalid SSH JPEG camera options")
        ssh_jpeg(args)
    elif args.source == "g1":
        os.environ.pop("CYCLONEDDS_URI", None)
        native = ROOT / ".runtime/cyclonedds"
        if (native / "lib/libddsc.so").exists():
            os.environ["CYCLONEDDS_HOME"] = str(native)
        g1(args)
    else:
        rtp(args)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    except Exception as exc:
        print("CAMERA WORKER ERROR:", exc, file=sys.stderr, flush=True)
        sys.exit(2)
