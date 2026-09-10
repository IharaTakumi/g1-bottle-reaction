"""Two independent camera processes, two single latest-frame slots, one GUI."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

from .camera_ipc import BGR, receive

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class FrameState:
    frame: np.ndarray | None = None
    stamp: float = 0
    fps: float = 0
    count: int = 0
    skipped: int = 0
    lost_packets: int = 0
    resolution: str = "unknown"
    error: str = "WAITING"


class LatestReader:
    def __init__(self, label, command):
        self.label, self.command = label, command
        self.state = FrameState()
        self.lock = threading.Lock()
        self.consumed = 0
        self.process = None
        self.thread = None
        self.stopping = False
        self.first = None
        self.last = None

    def start(self):
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONNOUSERSITE="1",
                   PYTHONDONTWRITEBYTECODE="1", GST_REGISTRY="/dev/null", GST_REGISTRY_UPDATE="no")
        # System GI helper must not inherit the venv's Python search path.
        env.pop("PYTHONPATH", None)
        try:
            self.process = subprocess.Popen(self.command, stdout=subprocess.PIPE, env=env)
            self.thread = threading.Thread(target=self._read, daemon=True, name=self.label)
            self.thread.start()
        except OSError as exc:
            self.state.error = str(exc)

    def _read(self):
        stamps = deque(maxlen=90)
        try:
            while not self.stopping:
                data, stamp, w, h, ow, oh, encoding, lost = receive(self.process.stdout)
                if not math.isfinite(stamp) or stamp > time.monotonic() + 1:
                    raise ValueError("Invalid local frame timestamp")
                if encoding == BGR:
                    frame = np.frombuffer(data, np.uint8).reshape(h, w, 3)
                else:
                    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                now = time.monotonic()
                stamps.append(now)
                fps = (len(stamps) - 1) / (stamps[-1] - stamps[0]) if len(stamps) > 1 else 0
                self.first = self.first if self.first is not None else now
                self.last = now
                with self.lock:
                    old = self.state
                    skipped = old.skipped + int(old.count > self.consumed)
                    self.state = FrameState(frame, stamp, fps, old.count + 1, skipped, lost,
                                            f"{ow or frame.shape[1]}x{oh or frame.shape[0]}", "")
        except (EOFError, ValueError, OSError, cv2.error) as exc:
            with self.lock:
                self.state.error = str(exc)

    def snapshot(self, consume=True):
        with self.lock:
            if consume:
                self.consumed = self.state.count
            return FrameState(**vars(self.state))

    def close(self):
        self.stopping = True
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            if self.thread:
                self.thread.join(timeout=2)
            self.process.stdout.close()

    def summary(self):
        state = self.snapshot()
        elapsed = self.last - self.first if self.first is not None and self.last is not None else 0
        return (f"{self.label}: resolution={state.resolution}, frames={state.count}, "
                f"average FPS={(state.count - 1) / elapsed if elapsed else 0:.1f}, "
                f"display skips={state.skipped}, RTP lost packets={state.lost_packets}")


def letterbox(frame, width, height):
    if width <= 0 or height <= 0 or frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("invalid display dimensions")
    result = np.zeros((height, width, 3), np.uint8)
    h, w = frame.shape[:2]
    scale = min(width / w, height / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    x, y = (width - nw) // 2, (height - nh) // 2
    result[y:y + nh, x:x + nw] = resized
    return result


def compose(states, mode, now, stale_seconds=0.5, usb_rotate=0, detection=None, boxes=True, found_label=None):
    selected = ["g1", "usb"] if mode == "dual" else [mode]
    panels = []
    for name in selected:
        state = states.get(name, FrameState(error="NOT ENABLED"))
        width = 640 if len(selected) == 2 else 1280
        age = max(0, now - state.stamp)
        live = state.frame is not None and not state.error and age <= stale_seconds
        frame = cv2.rotate(state.frame, cv2.ROTATE_180) if live and name == "usb" and usb_rotate == 180 else state.frame
        # Header / full camera image / diagnostics are disjoint regions.
        # Never paint status rectangles over the camera's field of view.
        panel = np.zeros((540, width, 3), np.uint8)
        video = panel[78:438]
        info = panel[438:]
        if live:
            video[:] = letterbox(frame, width, video.shape[0])
        label = "G1 Built-in Camera" if name == "g1" else "USB Head Camera"
        color = (80, 255, 80) if live else (60, 60, 255)
        panel[:78] = (20, 20, 20)
        cv2.putText(panel, f"{label} | {'LIVE' if live else 'LOST'} | {state.fps if live else 0:.1f} FPS",
                    (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 1)
        age_label = f"{age * 1000:.0f}ms" if state.frame is not None else "N/A"
        cv2.putText(panel, f"local age {age_label} | skip {state.skipped} | RTP lost {state.lost_packets}",
                    (12, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)
        if not live:
            cv2.putText(panel, f"{label.upper()} LOST", (18, 250), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        if name == "g1" and detection is not None:
            from .person_yolo import draw_detection, visible_detection
            draw_detection(video, visible_detection(detection, live, now), boxes, info_panel=info)
        if name == "g1" and found_label is not None:
            from .found_audio import draw_found_status
            draw_found_status(info, found_label)
        panels.append(panel)
    return np.hstack(panels)


def validate_args(args):
    if args.yolo and args.source != "dual":
        raise ValueError("--yolo is only supported for the G1 camera in --source dual")
    if not math.isfinite(args.yolo_confidence) or not 0 < args.yolo_confidence <= 1:
        raise ValueError("--yolo-confidence must be in (0, 1]")
    if not math.isfinite(args.yolo_fps) or not 0 < args.yolo_fps <= 60:
        raise ValueError("--yolo-fps must be in (0, 60]")
    bind = ipaddress.IPv4Address(args.usb_bind)
    host = ipaddress.IPv4Address(args.usb_host)
    if bind.is_unspecified or bind.is_multicast or host.is_multicast or host.is_unspecified:
        raise ValueError("Use specific unicast LAN addresses")
    if args.start_usb_sender and (bind.is_loopback or host.is_loopback or bind == host):
        raise ValueError("--start-usb-sender requires the verified, distinct Ubuntu/G1 LAN addresses")
    if not 1024 <= args.usb_port <= 65535:
        raise ValueError("--usb-port must be 1024..65535")
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        raise ValueError("--duration must be finite and positive")
    if args.max_frames is not None and args.max_frames <= 0:
        raise ValueError("--max-frames must be positive")
    if any((args.publish_processed, args.publish_safety, args.vision_preset, args.fog_mode,
            args.fov_scale is not None, args.fov_feather is not None, args.config,
            args.display, args.network_address)):
        raise ValueError("dual/usb-lan are plain RGB viewers; processing and legacy DDS address flags do not apply")


def start_sender(args):
    route = subprocess.run(["ip", "-j", "route", "get", args.usb_host],
                           check=True, capture_output=True, text=True)
    routes = json.loads(route.stdout)
    if not routes or routes[0].get("gateway") or routes[0].get("prefsrc") != args.usb_bind:
        raise RuntimeError("G1 must have a direct wired route from --usb-bind; no network settings were changed")
    interface = routes[0].get("dev", "")
    if (Path("/sys/class/net") / interface / "wireless").exists():
        raise RuntimeError("USB sender test requires the existing wired LAN route")
    remote = (ROOT / "tools/g1_usb_send.py").read_text()
    argv = ["python3", "-u", "-B", "-c", remote, "--watch-stdin", "--dest", args.usb_bind,
            "--bind", args.usb_host, "--port", str(args.usb_port), "--width", str(args.usb_width)]
    if args.duration:
        argv += ["--duration", str(math.ceil(args.duration) + 60)]
    ssh = ["ssh", "-T", "-o", "StrictHostKeyChecking=yes"]
    if args.ssh_control:
        ssh += ["-S", args.ssh_control, "-o", "BatchMode=yes"]
    ssh += ["--", args.ssh_target, shlex.join(argv)]
    # stdin EOF is the robot-side supervisor's cleanup signal.
    return subprocess.Popen(ssh, stdin=subprocess.PIPE)


def run(args):
    validate_args(args)
    found_settings = None
    if args.found_audio:
        from .found_audio import load_settings
        found_settings = load_settings(args, ROOT)
    if not args.headless and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("A desktop session is required; otherwise use --headless")
    readers = {}
    yolo = None
    audio = gate = None
    found_label = None
    boxes = True
    detection = None
    sender = None
    opened = False
    stopped_sender_reported = False
    window = "G1 + USB Head Camera"
    mode = "dual" if args.source == "dual" else "usb"
    fullscreen = bool(args.fullscreen)
    helper = str(ROOT / "tools/g1_camera_pipe.py")
    start = time.monotonic()
    last_report = start
    both_since = None
    continuous = 0
    display_count = 0
    report_display_count = 0
    first_display = None
    try:
        if found_settings:
            from .found_audio import FoundGate, SingleAudioWorker
            from g1_bottle_reaction.adapters.cached_audio import G1SshAudioOutput, LinuxAplayOutput
            gate = FoundGate(found_settings.duration, found_settings.grace, found_settings.cooldown,
                             found_settings.confidence, found_settings.rearm_absence)
            if found_settings.output == "g1":
                output = G1SshAudioOutput(args.ssh_target, args.ssh_control)
            else:
                output = LinuxAplayOutput()
            audio = SingleAudioWorker(output, found_settings.sounds)
            print(f"FOUND AUDIO: output={found_settings.output}, files={len(found_settings.sounds)}, "
                  f"duration={gate.duration}s, grace={gate.grace}s, cooldown={gate.cooldown}s, "
                  f"rearm absence={gate.rearm_absence}s; ONCE UNTIL PERSON LEAVES", flush=True)
        if args.source == "dual":
            cmd = [sys.executable, "-B", helper, "g1"]
            if args.network_interface:
                cmd += ["--interface", args.network_interface]
            readers["g1"] = LatestReader("G1", cmd)
        readers["usb"] = LatestReader("USB", [args.gst_python, "-B", helper, "usb",
                                               "--bind", args.usb_bind, "--port", str(args.usb_port)])
        for reader in readers.values():
            reader.start()
        if args.yolo:
            from .person_yolo import PersonWorker, TransitionLogger, visible_detection
            yolo = PersonWorker(lambda: readers["g1"].snapshot(consume=False), args.yolo_model,
                                args.yolo_confidence, args.yolo_fps, ROOT / ".runtime/yolo")
            transitions = TransitionLogger()
            yolo.start()
        if args.start_usb_sender:
            sender = start_sender(args)
        if not args.headless:
            cv2.namedWindow(window, cv2.WINDOW_NORMAL)
            opened = True
            cv2.resizeWindow(window, 1280, 540)
            cv2.setWindowProperty(window, cv2.WND_PROP_FULLSCREEN,
                                 cv2.WINDOW_FULLSCREEN if fullscreen else cv2.WINDOW_NORMAL)
        print("1=G1 2=USB 3=dual f=fullscreen q/Esc=exit; local age is NOT capture-to-display latency", flush=True)
        if yolo:
            print("y=YOLO ON/OFF b=boxes ON/OFF; G1 ONLY; NO ROBOT MOTION COMMANDS", flush=True)
        while True:
            now = time.monotonic()
            states = {key: r.snapshot() for key, r in readers.items()}
            if yolo:
                g1 = states["g1"]
                detection = visible_detection(yolo.snapshot(), g1.frame is not None and not g1.error
                                              and now - g1.stamp <= .5, now)
                message = transitions.update(detection)
                if message:
                    print(message, flush=True)
                if gate:
                    if gate.update(detection, now, audio_busy=audio.busy or bool(audio.error)):
                        if audio.submit():
                            print(f"FOUND TRIGGER: cooldown {gate.cooldown:.2f}s", flush=True)
                    found_label = "AUDIO ERROR (disabled)" if audio.error else gate.label(now)
            live = all(s.frame is not None and not s.error and now - s.stamp <= 0.5 for s in states.values())
            if live:
                both_since = both_since if both_since is not None else now
                continuous = max(continuous, now - both_since)
            else:
                both_since = None
            if sender is not None and sender.poll() is not None and not stopped_sender_reported:
                print(f"USB sender exited: {sender.returncode}; other camera continues", flush=True)
                stopped_sender_reported = True
            if opened:
                cv2.imshow(window, compose(states, mode, now, usb_rotate=args.usb_rotate,
                                          detection=detection, boxes=boxes, found_label=found_label))
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), ord("Q"), 27) or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    break
                if key in (ord("1"), ord("2"), ord("3")):
                    mode = {ord("1"): "g1", ord("2"): "usb", ord("3"): "dual"}[key]
                if key == ord("y") and yolo:
                    print(f"YOLO: {'ON' if yolo.toggle() else 'OFF'}", flush=True)
                if key == ord("b"):
                    boxes = not boxes
                if key in (ord("f"), ord("F")):
                    fullscreen = not fullscreen
                    cv2.setWindowProperty(window, cv2.WND_PROP_FULLSCREEN,
                                         cv2.WINDOW_FULLSCREEN if fullscreen else cv2.WINDOW_NORMAL)
            display_count += 1
            if first_display is None:
                first_display = now
            if now - last_report >= 5:
                for key, s in states.items():
                    age = now - s.stamp if s.frame is not None else float("inf")
                    print(f"{key}: {s.resolution}, FPS={s.fps:.1f}, local_age={age*1000:.1f}ms, "
                          f"frames={s.count}, display_skips={s.skipped}, RTP_lost={s.lost_packets}, "
                          f"status={s.error or ('LIVE' if age <= .5 else 'LOST')}", flush=True)
                if yolo:
                    print(f"display_loop FPS={(display_count-report_display_count)/(now-last_report):.1f}; "
                          f"YOLO FPS={detection.fps:.1f}, inference={detection.inference_ms:.1f}ms, "
                          f"result_age={(now-detection.stamp)*1000 if detection.stamp else float('inf'):.1f}ms, "
                          f"status={detection.status}", flush=True)
                report_display_count = display_count
                last_report = now
            if args.duration and now - start >= args.duration:
                break
            if args.max_frames and display_count >= args.max_frames:
                break
            time.sleep(0.01)
    finally:
        try:
            if sender:
                sender.stdin.close()
                try:
                    sender.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    sender.terminate()
                    try:
                        sender.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        sender.kill()
                        sender.wait()
        finally:
            if audio:
                audio.close()
            if yolo:
                yolo.close()
                print(yolo.summary(), flush=True)
            for reader in readers.values():
                reader.close()
            if opened:
                cv2.destroyAllWindows()
                cv2.waitKey(1)
        for reader in readers.values():
            print(reader.summary(), flush=True)
        print(f"VIEW STOPPED; longest all-camera LIVE interval={continuous:.1f}s", flush=True)
        if first_display is not None:
            print(f"Display loop frames={display_count}; not a unique-camera-frame or monitor-refresh count", flush=True)
    return 0 if readers and all(r.state.count for r in readers.values()) else 2
