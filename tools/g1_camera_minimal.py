#!/usr/bin/env python3
"""Ubuntu wired RGB viewer. Only VideoClient image requests; no robot control.

The parent reports native DDS crashes; all SDK/GUI resources belong to its child.
Run with the dedicated venv; no activation or installed application is required.
"""
from __future__ import annotations

import argparse
from collections import deque
import ipaddress
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def positive(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network-interface", help="verified wired NIC; otherwise require one candidate")
    parser.add_argument("--g1-ip", type=ipaddress.IPv4Address,
                        help="confirmed G1 IP, used only to validate the direct wired route")
    parser.add_argument("--list-interfaces", action="store_true", help="inspect NICs without DDS")
    parser.add_argument("--no-frame-timeout", type=positive, default=5.0)
    parser.add_argument("--duration", type=positive, help="stop after N seconds of display")
    parser.add_argument("--headless", action="store_true", help="measure capture without GUI")
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def inspect_interfaces() -> list[dict]:
    result = subprocess.run(["ip", "-j", "address"], check=True, capture_output=True, text=True)
    interfaces = json.loads(result.stdout)
    for nic in interfaces:
        path = Path("/sys/class/net") / nic["ifname"]
        nic["wired"] = (nic.get("link_type") == "ether"
                        and (path / "device").exists()
                        and not (path / "wireless").exists())
    return interfaces


def select_interface(interfaces: list[dict], name: str | None, peer=None) -> tuple[str, list[str]]:
    candidates = []
    for nic in interfaces:
        if name and nic["ifname"] != name:
            continue
        if not nic.get("wired") or "LOWER_UP" not in nic.get("flags", []):
            continue
        ips = [f'{a["local"]}/{a["prefixlen"]}' for a in nic.get("addr_info", [])
               if a["family"] == "inet" and a.get("scope") == "global"]
        if peer:
            ips = [a for a in ips if peer in ipaddress.ip_interface(a).network
                   and peer != ipaddress.ip_interface(a).ip]
        if ips:
            candidates.append((nic["ifname"], ips))
    if len(candidates) != 1:
        raise RuntimeError("Cannot uniquely select a connected wired NIC with IPv4. "
                           "Use --list-interfaces, connect the cable, then specify "
                           "--network-interface and optionally --g1-ip. No settings were changed.")
    return candidates[0]


def verify_route(peer, interface: str) -> None:
    result = subprocess.run(["ip", "-j", "route", "get", str(peer)],
                            check=True, capture_output=True, text=True)
    routes = json.loads(result.stdout)
    if not routes or routes[0].get("dev") != interface or routes[0].get("gateway"):
        raise RuntimeError("G1 IP does not have a direct route through the selected wired NIC")


class CameraRuntime:
    """Only the two camera symbols, loaded at the existing SDK import boundary."""

    def create_video_client(self, interface, timeout):
        from g1_bottle_reaction.adapters.g1_robot import UnitreeSdkRuntime

        print("[4] DDS initialization ...", flush=True)
        try:
            initialize, video_type = UnitreeSdkRuntime().load_camera_symbols()
            from cyclonedds.internal import DDS
            print(f"DDS native library: {DDS._dll_handle._name}; config tracing: OFF", flush=True)
            initialize(0, interface)
        except Exception as exc:
            raise RuntimeError(f"DDS initialization failed: {exc}") from exc
        print("[4] DDS ........ OK", flush=True)
        print("[5] VideoClient initialization ...", flush=True)
        try:
            client = video_type()
            client.SetTimeout(timeout)
            client.Init()
        except Exception as exc:
            raise RuntimeError(f"VideoClient initialization failed: {exc}") from exc
        print("[5] VideoClient . OK (local initialization; waiting for camera response)", flush=True)
        return client


def capture(args, source, cv=None) -> int:
    """Bounded image reads; FPS is receive/display rate, not sensor latency."""
    window = "G1 Camera Viewer"
    opened = False
    first = None
    last = time.monotonic()
    last_report = last
    stamps = deque(maxlen=120)
    frames = 0
    errors = 0
    max_gap = 0.0
    last_error = "no response"
    fullscreen = args.fullscreen
    try:
        source.open()
        last = time.monotonic()
        print("Waiting frame... q/ESC: exit, f: fullscreen", flush=True)
        while True:
            now = time.monotonic()
            if now - last >= args.no_frame_timeout:
                raise RuntimeError(f"No camera frame received for {args.no_frame_timeout:g} seconds. "
                                   f"Last error: {last_error}")
            try:
                frame = source.read()
            except RuntimeError as exc:
                errors += 1
                last_error = str(exc)
                frame = None
            now = time.monotonic()
            if now - last >= args.no_frame_timeout:
                raise RuntimeError(f"No camera frame received for {args.no_frame_timeout:g} seconds. "
                                   f"Last error: {last_error}")
            if frame is not None:
                if first is None:
                    first = now
                    print(f"[6] First frame . OK; Resolution: {frame.shape[1]}x{frame.shape[0]}", flush=True)
                    if cv is not None:
                        cv.namedWindow(window, cv.WINDOW_NORMAL)
                        opened = True
                        cv.setWindowProperty(window, cv.WND_PROP_FULLSCREEN,
                                             cv.WINDOW_FULLSCREEN if fullscreen else cv.WINDOW_NORMAL)
                else:
                    max_gap = max(max_gap, now - last)
                frames += 1
                last = now
                stamps.append(now)
                fps = (len(stamps) - 1) / (stamps[-1] - stamps[0]) if len(stamps) > 1 else 0.0
                if now - last_report >= 1:
                    print(f"[7] FPS: {fps:.1f}; frames: {frames}; errors: {errors}; "
                          f"max gap: {max_gap:.3f}s", flush=True)
                    last_report = now
                if cv is not None:
                    cv.putText(frame, f"RGB | {fps:.1f} FPS | R3 control", (12, 26),
                               cv.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)
                    cv.imshow(window, frame)
            elif opened:
                # Do not leave a frozen camera picture looking live.
                import numpy as np
                stale = np.zeros((240, 640, 3), dtype=np.uint8)
                cv.putText(stale, "NO FRESH FRAME - STOP WITH R3", (12, 120),
                           cv.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 255), 2)
                cv.imshow(window, stale)
            if opened:
                key = cv.waitKey(1) & 0xFF
                if key in (ord("q"), ord("Q"), 27) or cv.getWindowProperty(window, cv.WND_PROP_VISIBLE) < 1:
                    break
                if key in (ord("f"), ord("F")):
                    fullscreen = not fullscreen
                    cv.setWindowProperty(window, cv.WND_PROP_FULLSCREEN,
                                         cv.WINDOW_FULLSCREEN if fullscreen else cv.WINDOW_NORMAL)
            if first is not None and args.duration and now - first >= args.duration:
                break
            if frame is None:
                time.sleep(0.01)
    finally:
        try:
            source.close()
        finally:
            if opened:
                cv.destroyAllWindows()
                cv.waitKey(1)
    elapsed = time.monotonic() - first if first is not None else 0
    rate = (frames - 1) / elapsed if elapsed > 0 else 0
    print(f"STOPPED: {frames} frames, {elapsed:.1f}s, average {rate:.1f} FPS, "
          f"errors {errors}, max gap {max_gap:.3f}s. End-to-end delay: NOT MEASURED", flush=True)
    return 0


def worker(args) -> int:
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    from g1_bottle_reaction.vision.camera import G1CameraSource

    cv = None
    if not args.headless:
        import cv2 as cv
    source = G1CameraSource(args.network_interface, runtime=CameraRuntime(),
                            timeout_seconds=min(0.5, args.no_frame_timeout / 2), read_attempts=1)
    try:
        return capture(args, source, cv)
    except Exception as exc:
        raise RuntimeError(f"Camera capture/display failed: {exc}") from exc


def supervise(command: list[str], no_frame_timeout: float) -> int:
    """Native aborts cannot be caught by Python inside the DDS process."""
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    # A previously sourced DDS XML must not silently override this NIC selection.
    env.pop("CYCLONEDDS_URI", None)
    native = ROOT / ".runtime" / "cyclonedds"
    if (native / "lib" / "libddsc.so").exists():
        env["CYCLONEDDS_HOME"] = str(native)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    pending = b""
    deadline = time.monotonic() + 15
    stage = "DDS / VideoClient startup"
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{stage} timed out; stopped the local viewer process")
                if not selector.select(0.1):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    message = line.decode(errors="replace")
                    print(message, flush=True)
                    if message.startswith(("[5] VideoClient . OK", "[6]", "[7]")):
                        deadline = time.monotonic() + no_frame_timeout + 2
                        stage = "Camera frame acquisition / display"
        if pending:
            print(pending.decode(errors="replace"), flush=True)
        code = process.wait(timeout=2)
        if code < 0:
            raise RuntimeError(f"Viewer native process terminated by {signal.Signals(-code).name}. "
                               "Check the last numbered stage above. DDS/NIC native failure cannot "
                               "be caught in Python. No automatic NIC fallback was attempted.")
        return code
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args._worker:
            return worker(args)
        print("G1 Camera Viewer (RGB only; movement remains on R3)", flush=True)
        print(f"[1] Python: {sys.version.split()[0]}; executable: {sys.executable}; venv: {sys.prefix}", flush=True)
        interfaces = inspect_interfaces()
        if args.list_interfaces:
            for nic in interfaces:
                ips = [a["local"] for a in nic.get("addr_info", []) if a["family"] == "inet"]
                print(nic["ifname"], "wired=" + str(nic["wired"]), nic.get("operstate"), ips)
            return 0
        name, ips = select_interface(interfaces, args.network_interface, args.g1_ip)
        if args.g1_ip:
            verify_route(args.g1_ip, name)
        print(f"[2] selected NIC: {name} (wired, link up)", flush=True)
        print(f"[3] local IP: {', '.join(ips)}", flush=True)
        print(f"G1 IP: {args.g1_ip or 'not specified; DDS discovers the camera service'}; "
              "NIC selection alone does not confirm G1 identity", flush=True)
        if not args.headless and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise RuntimeError("No graphical desktop session. Run in the Ubuntu desktop terminal or use --headless.")
        command = [sys.executable, str(Path(__file__).resolve()), "--_worker",
                   "--network-interface", name, "--no-frame-timeout", str(args.no_frame_timeout)]
        if args.duration:
            command += ["--duration", str(args.duration)]
        if args.headless:
            command.append("--headless")
        if args.fullscreen:
            command.append("--fullscreen")
        return supervise(command, args.no_frame_timeout)
    except KeyboardInterrupt:
        print("Viewer interrupted; local resources released", file=sys.stderr)
        return 130
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"CAMERA ERROR: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
