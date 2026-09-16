"""Two independent camera processes, two single latest-frame slots, one GUI."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import ipaddress
import inspect
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
    if args.banana_confidence is not None and (not math.isfinite(args.banana_confidence)
                                                or not 0 < args.banana_confidence <= 1):
        raise ValueError("--banana-confidence must be in (0, 1]")
    if args.plushie_confidence is not None and (not math.isfinite(args.plushie_confidence)
                                                 or not 0 < args.plushie_confidence <= 1):
        raise ValueError("--plushie-confidence must be in (0, 1]")
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
    if not 1024 <= args.g1_camera_port <= 65535:
        raise ValueError("--g1-camera-port must be 1024..65535")
    if not math.isfinite(args.g1_camera_fps) or not 0 < args.g1_camera_fps <= 60:
        raise ValueError("--g1-camera-fps must be in (0, 60]")
    if args.source != "dual" and (args.no_usb_camera or args.g1_camera_transport != "direct-dds"):
        raise ValueError("--no-usb-camera and --g1-camera-transport apply only to --source dual")
    if args.no_usb_camera and args.start_usb_sender:
        raise ValueError("--no-usb-camera cannot be combined with --start-usb-sender")
    if (args.source == "dual" and not args.no_usb_camera
            and args.g1_camera_transport == "ssh-rtp"
            and args.g1_camera_port == args.usb_port):
        raise ValueError("G1 and USB camera RTP ports must be different")
    if args.g1_camera_transport in {"ssh-rtp", "ssh-jpeg"} and host.is_loopback:
        raise ValueError("SSH G1 camera transport requires a non-loopback --usb-host")
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        raise ValueError("--duration must be finite and positive")
    if args.max_frames is not None and args.max_frames <= 0:
        raise ValueError("--max-frames must be positive")
    if not math.isfinite(args.motiondecode_timeout) or args.motiondecode_timeout <= 0:
        raise ValueError("--motiondecode-timeout must be finite and positive")
    if args.robot in {"g1", "g1-ssh"}:
        if not args.found_audio:
            raise ValueError("real robot adapters require --found-audio")
        if not args.enable_real_robot or args.g1_motion != "safe-actions":
            raise ValueError(
                "Real G1 reaction requires --enable-real-robot "
                "--g1-motion safe-actions"
            )
        if args.robot == "g1-ssh" and not args.execute_real_action:
            raise ValueError("--robot g1-ssh requires --execute-real-action")
    elif args.robot == "motiondecode":
        if not args.found_audio:
            raise ValueError("MotionDecode reactions require --found-audio")
        if args.g1_motion != "disabled":
            raise ValueError("MotionDecode does not use --g1-motion")
        if args.enable_real_robot and not args.confirm_site_ready:
            raise ValueError(
                "Real MotionDecode requires --enable-real-robot --confirm-site-ready"
            )
        if args.enable_real_robot and not args.quiet_mode:
            raise ValueError("Real plushie reaction requires --quiet-mode")
    elif args.enable_real_robot or args.g1_motion != "disabled":
        raise ValueError("Real robot safety flags require --robot g1")
    if args.confirm_site_ready and args.robot != "motiondecode":
        raise ValueError("--confirm-site-ready is restricted to --robot motiondecode")
    if args.execute_real_action and args.robot != "g1-ssh":
        raise ValueError("--execute-real-action is restricted to --robot g1-ssh")
    if any((args.publish_processed, args.publish_safety, args.vision_preset, args.fog_mode,
            args.fov_scale is not None, args.fov_feather is not None, args.config,
            args.display, args.network_address)):
        raise ValueError("dual/usb-lan are plain RGB viewers; processing and legacy DDS address flags do not apply")


def resolve_g1_route(local_address, host_address, requested_interface=None):
    """Resolve and validate one direct local route without changing networking."""
    route = subprocess.run(["ip", "-j", "route", "get", str(host_address)],
                           check=True, capture_output=True, text=True)
    routes = json.loads(route.stdout)
    if not routes or routes[0].get("gateway") or routes[0].get("prefsrc") != str(local_address):
        raise RuntimeError("G1 must have a direct route from --usb-bind; no network settings were changed")
    interface = routes[0].get("dev", "")
    if not interface or (requested_interface and interface != requested_interface):
        raise RuntimeError("G1 route does not use --network-interface; no network settings were changed")
    return interface


def resolve_ssh_target(args):
    """Use the G1 address for SSH unless an explicit alias/target was supplied."""
    if args.ssh_target:
        return args.ssh_target
    host = ipaddress.IPv4Address(args.usb_host)
    return "g1" if host.is_loopback else f"unitree@{host}"


def start_sender(args, ssh_target=None, route_interface=None):
    if route_interface is None:
        route_interface = resolve_g1_route(
            args.usb_bind, args.usb_host, args.network_interface
        )
    remote = (ROOT / "tools/g1_usb_send.py").read_text()
    argv = ["python3", "-u", "-B", "-c", remote, "--watch-stdin", "--dest", args.usb_bind,
            "--bind", args.usb_host, "--port", str(args.usb_port), "--width", str(args.usb_width),
            "--device", args.usb_device]
    if args.duration:
        argv += ["--duration", str(math.ceil(args.duration) + 60)]
    ssh = ["ssh", "-T", "-o", "StrictHostKeyChecking=yes"]
    if args.ssh_control:
        ssh += ["-S", args.ssh_control, "-o", "BatchMode=yes"]
    ssh += ["--", ssh_target or resolve_ssh_target(args), shlex.join(argv)]
    # stdin EOF is the robot-side supervisor's cleanup signal.
    return subprocess.Popen(ssh, stdin=subprocess.PIPE)


def start_g1_camera_sender(args, ssh_target=None):
    """Start receive-only VideoClient on G1 and send its JPEG over RTP."""
    from g1_bottle_reaction.adapters.g1_robot import serve_remote_camera_sender

    source = inspect.getsource(serve_remote_camera_sender) + "\nserve_remote_camera_sender()\n"
    argv = [
        "python3", "-u", "-B", "-c", source,
        "--dest", args.usb_bind,
        "--bind", args.usb_host,
        "--port", str(args.g1_camera_port),
        "--fps", str(args.g1_camera_fps),
    ]
    if args.duration:
        argv += ["--duration", str(math.ceil(args.duration) + 60)]
    ssh = ["ssh", "-T", "-o", "StrictHostKeyChecking=yes"]
    if args.ssh_control:
        ssh += ["-S", args.ssh_control, "-o", "BatchMode=yes"]
    ssh += ["--", ssh_target or resolve_ssh_target(args), shlex.join(argv)]
    return subprocess.Popen(ssh, stdin=subprocess.PIPE)


def run(args):
    validate_args(args)
    ssh_target = resolve_ssh_target(args)
    route_interface = None
    g1_host = ipaddress.IPv4Address(args.usb_host)
    if not g1_host.is_loopback and (args.source == "dual" or args.start_usb_sender):
        route_interface = resolve_g1_route(
            args.usb_bind, args.usb_host, args.network_interface
        )
    found_settings = banana_settings = plushie_settings = None
    banana_confidence = plushie_confidence = None
    if args.yolo:
        from .person_yolo import load_banana_confidence, load_plushie_confidence
        banana_confidence = load_banana_confidence(args, ROOT)
        plushie_confidence = load_plushie_confidence(args, ROOT)
    if args.found_audio:
        from .found_audio import (
            load_banana_settings,
            load_plushie_settings,
            load_quiet_gain_db,
            load_settings,
        )
        found_settings = load_settings(args, ROOT)
        banana_settings = load_banana_settings(ROOT, banana_confidence, found_settings.output)
        plushie_settings = load_plushie_settings(ROOT, plushie_confidence, found_settings.output)
        quiet_gain_db = load_quiet_gain_db(ROOT) if args.quiet_mode else None
    if not args.headless and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("A desktop session is required; otherwise use --headless")
    readers = {}
    yolo = None
    reaction = gate = banana_gate = plushie_gate = None
    found_label = None
    boxes = True
    detection = None
    usb_sender = g1_sender = None
    opened = False
    stopped_sender_reported = False
    window = "G1 Built-in Camera" if args.no_usb_camera else "G1 + USB Head Camera"
    mode = "g1" if args.no_usb_camera else ("dual" if args.source == "dual" else "usb")
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
            from .found_audio import (
                AttenuatedWavOutput,
                ConsoleWavOutput,
                FoundGate,
                FoundReactionController,
                select_audio_trigger,
                select_plushie_only_trigger,
            )
            from g1_bottle_reaction.adapters.cached_audio import G1SshAudioOutput, LinuxAplayOutput
            from g1_bottle_reaction.adapters.g1_robot import G1RobotAdapter
            from g1_bottle_reaction.adapters.g1_ssh_safe_action import SshG1SafeActionAdapter
            from g1_bottle_reaction.adapters.mock_robot import MockRobotAdapter
            from g1_bottle_reaction.config.loader import load_config
            from g1_bottle_reaction.state.events import ReactionEvent

            gate = FoundGate(found_settings.duration, found_settings.grace, found_settings.cooldown,
                             found_settings.confidence, found_settings.rearm_absence)
            banana_gate = FoundGate(banana_settings.duration, banana_settings.grace,
                                    banana_settings.cooldown, banana_settings.confidence,
                                    banana_settings.rearm_absence, object_attribute="bananas")
            plushie_gate = FoundGate(plushie_settings.duration, plushie_settings.grace,
                                     plushie_settings.cooldown, plushie_settings.confidence,
                                     plushie_settings.rearm_absence,
                                     object_attribute="plushies",
                                     absence_blocking_attributes=("people",),
                                     absence_overlap=(
                                         plushie_settings.absence_blocking_overlap
                                     ))
            if found_settings.output == "g1":
                output = G1SshAudioOutput(ssh_target, args.ssh_control)
            elif found_settings.output == "pc":
                output = LinuxAplayOutput()
            else:
                output = ConsoleWavOutput()
            if args.quiet_mode:
                output = AttenuatedWavOutput(output, quiet_gain_db)
            core_config = load_config(ROOT / "config/default.yaml")
            if args.robot == "g1":
                robot = G1RobotAdapter(
                    args.network_interface or "",
                    enabled=args.enable_real_robot,
                    motion_mode=args.g1_motion,
                    timeout_seconds=core_config.g1.client_timeout_seconds,
                    release_delay_seconds=core_config.g1.arm_release_delay_seconds,
                )
                robot.initialize()
            elif args.robot == "g1-ssh":
                robot = SshG1SafeActionAdapter(
                    ssh_target,
                    args.ssh_control,
                    enabled=args.enable_real_robot,
                    motion_mode=args.g1_motion,
                    execute_real_action=args.execute_real_action,
                )
                robot.initialize()
            elif args.robot == "motiondecode":
                from g1_bottle_reaction.adapters.motiondecode_reaction import (
                    MotionDecodeReactionAdapter,
                )

                robot = MotionDecodeReactionAdapter(
                    args.motiondecode_repository,
                    real=args.enable_real_robot,
                    enabled=args.enable_real_robot,
                    transport=args.motiondecode_transport,
                    ssh_target=ssh_target,
                    ssh_control=args.ssh_control,
                    timeout_seconds=args.motiondecode_timeout,
                    resident=True,
                    socket_path=args.motiondecode_socket,
                    fallback=MockRobotAdapter(),
                )
            else:
                robot = MockRobotAdapter()
            motion_overrides = (
                {"plushie": "motiondecode:surprise"}
                if args.robot == "motiondecode"
                else None
            )
            reaction = FoundReactionController(
                {
                    "person": found_settings,
                    "banana": banana_settings,
                    "plushie": plushie_settings,
                },
                output,
                robot,
                base_reaction=core_config.reaction.items[ReactionEvent.FOUND.value],
                cooldown_seconds=core_config.reaction.cooldown_seconds,
                motion_overrides=motion_overrides,
                speech_delay_overrides={"plushie": 0.0},
            )
            print(f"FOUND REACTION: robot={args.robot}, output={found_settings.output}, "
                  f"motion=notice, files={len(found_settings.sounds)}, "
                  f"duration={gate.duration}s, grace={gate.grace}s, cooldown={gate.cooldown}s, "
                  f"rearm absence={gate.rearm_absence}s; ONCE UNTIL PERSON LEAVES", flush=True)
            print(f"BANANA AUDIO: files={len(banana_settings.sounds)}, duration={banana_gate.duration}s, "
                  f"priority=PERSON; ONCE UNTIL BANANA LEAVES", flush=True)
            print(f"PLUSHIE AUDIO: files={len(plushie_settings.sounds)}, duration={plushie_gate.duration}s, "
                  f"YOLO class=77 teddy bear; motion="
                  f"{motion_overrides['plushie'] if motion_overrides else 'notice'}; "
                  f"ONCE UNTIL PLUSHIE LEAVES", flush=True)
            print(
                f"QUIET MODE: {'ON' if args.quiet_mode else 'OFF'}"
                + (f" ({quiet_gain_db:g} dB, audio only)" if args.quiet_mode else ""),
                flush=True,
            )
        if args.source == "dual":
            if args.g1_camera_transport == "ssh-rtp":
                cmd = [args.gst_python, "-B", helper, "rtp", "--label", "G1",
                       "--bind", args.usb_bind, "--port", str(args.g1_camera_port)]
            elif args.g1_camera_transport == "ssh-jpeg":
                cmd = [
                    sys.executable, "-B", helper, "ssh-jpeg",
                    "--ssh-target", ssh_target,
                    "--fps", str(args.g1_camera_fps),
                    "--duration", str(math.ceil(args.duration) + 60 if args.duration else 3600),
                ]
                if args.ssh_control:
                    cmd += ["--ssh-control", args.ssh_control]
            else:
                cmd = [sys.executable, "-B", helper, "g1"]
                camera_interface = args.network_interface or route_interface
                if camera_interface:
                    cmd += ["--interface", camera_interface]
                if not g1_host.is_loopback:
                    cmd += ["--g1-ip", str(g1_host)]
            readers["g1"] = LatestReader("G1", cmd)
        if not args.no_usb_camera:
            readers["usb"] = LatestReader(
                "USB", [args.gst_python, "-B", helper, "rtp", "--label", "USB",
                        "--bind", args.usb_bind, "--port", str(args.usb_port)])
        for reader in readers.values():
            reader.start()
        if args.yolo:
            from .person_yolo import PersonWorker, TransitionLogger, visible_detection
            yolo = PersonWorker(lambda: readers["g1"].snapshot(consume=False), args.yolo_model,
                                args.yolo_confidence, banana_confidence, args.yolo_fps,
                                ROOT / ".runtime/yolo", plushie_confidence=plushie_confidence)
            transitions = TransitionLogger()
            yolo.start()
        if args.start_usb_sender:
            usb_sender = start_sender(args, ssh_target, route_interface)
        if args.g1_camera_transport == "ssh-rtp":
            g1_sender = start_g1_camera_sender(args, ssh_target)
        if not args.headless:
            cv2.namedWindow(window, cv2.WINDOW_NORMAL)
            opened = True
            cv2.resizeWindow(window, 1280, 540)
            cv2.setWindowProperty(window, cv2.WND_PROP_FULLSCREEN,
                                 cv2.WINDOW_FULLSCREEN if fullscreen else cv2.WINDOW_NORMAL)
            topmost = getattr(cv2, "WND_PROP_TOPMOST", None)
            if topmost is not None:
                try:
                    cv2.setWindowProperty(window, topmost, 1)
                except cv2.error:
                    pass
        controls = "1=G1 f=fullscreen q/Esc=exit" if args.no_usb_camera else \
            "1=G1 2=USB 3=dual f=fullscreen q/Esc=exit"
        print(f"{controls}; local age is NOT capture-to-display latency", flush=True)
        if yolo:
            print("y=YOLO ON/OFF b=boxes ON/OFF; PERSON+BANANA+PLUSHIE ON G1 ONLY; "
                  f"REACTION TARGET={args.reaction_target.upper()}; "
                  f"ROBOT={args.robot.upper()}", flush=True)
        if (args.start_usb_sender or args.g1_camera_transport in {"ssh-rtp", "ssh-jpeg"}
                or (found_settings and found_settings.output == "g1")):
            print(f"G1 SSH TARGET: {ssh_target}", flush=True)
        while True:
            now = time.monotonic()
            states = {key: r.snapshot() for key, r in readers.items()}
            if yolo:
                g1 = states["g1"]
                raw_detection = yolo.snapshot()
                detection = visible_detection(
                    raw_detection,
                    g1.frame is not None and not g1.error and now - g1.stamp <= .5,
                    now,
                )
                message = transitions.update(detection)
                if message:
                    print(message, flush=True)
                if gate:
                    # Keep existing person/banana freshness unchanged. Plushie
                    # gets its configured dropout grace so the measured low-FPS
                    # wireless path can provide two distinct positive frames;
                    # FoundGate still rejects replaying one result.
                    plushie_detection = visible_detection(
                        raw_detection,
                        g1.frame is not None
                        and not g1.error
                        and now - g1.stamp <= plushie_gate.grace,
                        now,
                        max_age=plushie_gate.grace,
                    )
                    busy = reaction.busy or bool(reaction.audio_error)
                    if args.robot == "motiondecode":
                        trigger = select_plushie_only_trigger(
                            plushie_detection,
                            now,
                            plushie_gate,
                            audio_busy=busy,
                        )
                    else:
                        trigger = select_audio_trigger(
                            detection,
                            now,
                            gate,
                            banana_gate,
                            plushie_gate,
                            audio_busy=busy,
                            plushie_result=plushie_detection,
                            reaction_target=args.reaction_target,
                        )
                    if trigger == "person":
                        if reaction.trigger(trigger, now):
                            print(f"PERSON REACTION TRIGGER: cooldown {gate.cooldown:.2f}s", flush=True)
                    elif trigger == "banana":
                        if reaction.trigger(trigger, now):
                            print(f"BANANA REACTION TRIGGER: cooldown {banana_gate.cooldown:.2f}s", flush=True)
                    elif trigger == "plushie":
                        if reaction.trigger(trigger, now):
                            print(
                                "PLUSHIE REACTION TRIGGER: "
                                f"first_detection_monotonic={plushie_gate.confirmed_start:.6f}; "
                                f"confirmed_monotonic={now:.6f}; "
                                f"cooldown={plushie_gate.cooldown:.2f}s",
                                flush=True,
                            )
                    found_label = ("AUDIO ERROR (disabled)" if reaction.audio_error else
                                   f"P {gate.compact_label(now)} | B {banana_gate.compact_label(now)} | "
                                   f"T {plushie_gate.compact_label(now)}")
                    if reaction.motion_error:
                        found_label = f"MOTION ERROR (disabled) | {found_label}"
            live = all(s.frame is not None and not s.error and now - s.stamp <= 0.5 for s in states.values())
            if live:
                both_since = both_since if both_since is not None else now
                continuous = max(continuous, now - both_since)
            else:
                both_since = None
            if usb_sender is not None and usb_sender.poll() is not None and not stopped_sender_reported:
                print(f"USB sender exited: {usb_sender.returncode}; other camera continues", flush=True)
                stopped_sender_reported = True
            if g1_sender is not None and g1_sender.poll() is not None:
                print(f"G1 camera sender exited: {g1_sender.returncode}", flush=True)
                g1_sender = None
            if opened:
                cv2.imshow(window, compose(states, mode, now, usb_rotate=args.usb_rotate,
                                          detection=detection, boxes=boxes, found_label=found_label))
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), ord("Q"), 27) or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    break
                if not args.no_usb_camera and key in (ord("1"), ord("2"), ord("3")):
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
            if reaction:
                reaction.close()
        finally:
            try:
                for sender in (usb_sender, g1_sender):
                    if sender is None:
                        continue
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
