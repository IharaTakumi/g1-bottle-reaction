"""Optional person-only experiment. One in-flight frame; isolated inference process.

No camera, robot, motion, audio, tracking, or distance APIs are used here.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
import multiprocessing as mp
from pathlib import Path
import os
import threading
import time

import cv2


@dataclass(frozen=True)
class Person:
    box: tuple[float, float, float, float]
    confidence: float


@dataclass(frozen=True)
class Detection:
    people: tuple[Person, ...] = ()
    stamp: float = 0  # Source frame's PC receipt time, NOT inference completion.
    shape: tuple[int, int] = (0, 0)
    inference_ms: float = 0
    fps: float = 0
    status: str = "STARTING"


def filter_people(rows, confidence):
    """Defensively filter x1,y1,x2,y2,confidence,class rows to COCO person."""
    import math
    return tuple(Person(tuple(float(v) for v in row[:4]), float(row[4]))
                 for row in rows if len(row) == 6 and all(math.isfinite(float(v)) for v in row)
                 and row[5] == 0 and row[4] >= confidence
                 and row[2] > row[0] and row[3] > row[1])


def inference_process(connection, options):
    """Native CUDA/PyTorch failure cannot take down the camera/display process."""
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    root = Path(options["runtime"])
    for name in ("ultralytics", "matplotlib", "cuda"):
        (root / name).mkdir(parents=True, exist_ok=True)
    os.environ.update(YOLO_CONFIG_DIR=str(root / "ultralytics"),
                      MPLCONFIGDIR=str(root / "matplotlib"),
                      CUDA_CACHE_PATH=str(root / "cuda"), YOLO_AUTOINSTALL="false")
    try:
        import numpy as np
        import torch
        from ultralytics import YOLO
        from ultralytics.utils import SETTINGS
        SETTINGS.update({"sync": False})
        torch.set_num_threads(2)
        model_path = Path(options["model"])
        if not model_path.is_file():
            raise FileNotFoundError(f"Download the trusted COCO model first: {model_path}")
        model = YOLO(str(model_path))
        if model.names.get(0) != "person":
            raise ValueError("Expected COCO class 0 = person")
        device = "0" if torch.cuda.is_available() else "cpu"
        predict_args = dict(conf=options["confidence"], classes=[0], imgsz=640,
                            verbose=False, save=False, max_det=30)
        dummy = np.zeros((540, 960, 3), np.uint8)
        try:
            model.predict(dummy, device=device, **predict_args)
        except Exception as exc:
            if device == "cpu":
                raise
            print(f"CUDA warmup failed; CPU fallback: {exc}", flush=True)
            device = "cpu"
            model = YOLO(str(model_path))
            model.predict(dummy, device=device, **predict_args)
        print(f"YOLO device: {'CUDA / ' + torch.cuda.get_device_name(0) if device == '0' else 'CPU'}", flush=True)
        print(f"Model: {model_path}; confidence={options['confidence']}; classes=[0] person", flush=True)
        connection.send(("ready", device))
        while True:
            frame = connection.recv()
            if frame is None:
                break
            begin = time.monotonic()
            result = model.predict(frame, device=device, **predict_args)[0]
            rows = result.boxes.data.cpu().tolist()  # Also waits for CUDA completion.
            connection.send(("result", rows, (time.monotonic() - begin) * 1000))
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        try:
            connection.send(("error", f"{type(exc).__name__}: {exc}"))
        except (OSError, EOFError):
            pass
    finally:
        connection.close()


class PersonWorker:
    def __init__(self, source, model, confidence=.25, max_fps=15, runtime=None,
                 target=inference_process):
        self.source = source
        self.options = dict(model=str(model), confidence=confidence,
                            runtime=str(runtime or Path(model).parent / ".runtime"))
        self.interval = 1 / max_fps
        self.target = target
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.enabled = True
        self.generation = 0
        self.state = Detection()
        self.device = "STARTING"
        self.error = ""
        self.count = 0
        self.total_ms = 0
        self.first = self.last = None
        self.process = self.thread = self.connection = None

    def start(self):
        context = mp.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=self.target, args=(child, self.options), daemon=True)
        self.process.start()
        child.close()
        self.thread = threading.Thread(target=self._run, name="person-yolo-coordinator", daemon=True)
        self.thread.start()

    def _receive(self):
        while not self.stop.is_set():
            if self.connection.poll(.1):
                message = self.connection.recv()
                if message[0] == "error":
                    raise RuntimeError(message[1])
                return message
            if not self.process.is_alive():
                raise RuntimeError(f"YOLO process exited ({self.process.exitcode})")
        raise EOFError("YOLO stopped")

    def _run(self):
        stamps = deque(maxlen=45)
        try:
            message = self._receive()
            if message[0] != "ready":
                raise RuntimeError("Invalid YOLO startup response")
            self.device = message[1]
            with self.lock:
                if self.enabled:
                    self.state = Detection(status="WAITING CAMERA")
            last_count = -1
            next_time = 0
            while not self.stop.wait(.003):
                with self.lock:
                    enabled, generation = self.enabled, self.generation
                if not enabled or time.monotonic() < next_time:
                    continue
                source = self.source()  # Read latest here, never enqueue display frames.
                if source.frame is None or source.error or source.count == last_count or time.monotonic() - source.stamp > .5:
                    continue
                last_count = source.count
                next_time = time.monotonic() + self.interval
                self.connection.send(source.frame)  # Only coordinator can block, not GUI/cameras.
                response = self._receive()  # Exactly one frame in flight.
                if response[0] != "result":
                    raise RuntimeError("Invalid YOLO result")
                now = time.monotonic()
                stamps.append(now)
                fps = (len(stamps)-1)/(stamps[-1]-stamps[0]) if len(stamps) > 1 else 0
                result = Detection(filter_people(response[1], self.options["confidence"]),
                                   source.stamp, source.frame.shape[:2], response[2], fps, "RUNNING")
                self.count += 1
                self.total_ms += response[2]
                self.first = now if self.first is None else self.first
                self.last = now
                with self.lock:
                    if self.enabled and generation == self.generation:
                        self.state = result
        except Exception as exc:
            if not self.stop.is_set():
                self.error = str(exc)
                print(f"YOLO ERROR: {exc}; cameras continue", flush=True)
                with self.lock:
                    self.state = Detection(status="ERROR")

    def toggle(self):
        with self.lock:
            self.enabled = not self.enabled
            self.generation += 1
            self.state = Detection(status=("ERROR" if self.error else "WAITING") if self.enabled else "OFF")
            return self.enabled

    def snapshot(self):
        with self.lock:
            return self.state if self.enabled else Detection(status="OFF")

    def close(self):
        self.stop.set()
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2)
        if self.thread:
            self.thread.join(timeout=2)
        if self.connection:
            self.connection.close()

    def summary(self):
        seconds = (self.last - self.first) if self.count > 1 else 0
        return (f"YOLO: device={self.device}, count={self.count}, "
                f"average FPS={(self.count-1)/seconds if seconds else 0:.1f}, "
                f"average inference={self.total_ms/self.count if self.count else 0:.1f}ms")


def visible_detection(result, camera_live, now, max_age=.5):
    if result.status != "RUNNING":
        return result
    if not camera_live or now - result.stamp > max_age:
        return replace(result, people=(), status="STALE")
    return result


class TransitionLogger:
    def __init__(self):
        self.previous = None

    def update(self, result):
        state = ("DETECTED" if result.people else "NONE") if result.status == "RUNNING" else result.status
        if state == self.previous:
            return None
        old, self.previous = self.previous, state
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if state == "DETECTED":
            best = max(result.people, key=lambda p: p.confidence)
            return f"[{stamp}] PERSON DETECTED count={len(result.people)} conf={best.confidence:.2f} bbox={best.box}"
        if old == "DETECTED":
            return f"[{stamp}] PERSON LOST reason={state}"
        return None


def draw_detection(panel, result, boxes=True, *, info_panel=None):
    info = panel if info_panel is None else info_panel
    if result.status == "OFF":
        cv2.putText(info, "YOLO: OFF (y=ON)", (12, 22), cv2.FONT_HERSHEY_SIMPLEX, .6, (220, 220, 220), 1)
        return panel
    h, w = panel.shape[:2]
    if result.status == "RUNNING" and boxes:
        sh, sw = result.shape
        scale = min(w/sw, h/sh)
        ox, oy = (w-round(sw*scale))//2, (h-round(sh*scale))//2
        for person in result.people:
            x1, y1, x2, y2 = person.box
            start = (max(0, min(w-1, round(x1*scale+ox))), max(0, min(h-1, round(y1*scale+oy))))
            end = (max(0, min(w-1, round(x2*scale+ox))), max(0, min(h-1, round(y2*scale+oy))))
            cv2.rectangle(panel, start, end, (0, 255, 255), 2)
            cv2.putText(panel, f"PERSON {person.confidence:.2f}", (start[0], max(18, start[1]-6)),
                        cv2.FONT_HERSHEY_SIMPLEX, .6, (0, 255, 255), 2)
    cv2.rectangle(info, (0, 0), (info.shape[1], 75), (20, 20, 20), -1)
    detected = result.status == "RUNNING" and bool(result.people)
    title = "PERSON DETECTED" if detected else ("PERSON: NONE" if result.status == "RUNNING" else "YOLO: " + result.status)
    cv2.putText(info, title, (12, 23), cv2.FONT_HERSHEY_SIMPLEX, .7,
                (0, 255, 255) if detected else (220, 220, 220), 2)
    best = max((p.confidence for p in result.people), default=0)
    cv2.putText(info, f"YOLO: ON | count: {len(result.people)} | best conf: {best:.2f}", (12, 46),
                cv2.FONT_HERSHEY_SIMPLEX, .5, (220, 220, 220), 1)
    cv2.putText(info, f"inference: {result.inference_ms:.1f} ms | YOLO FPS: {result.fps:.1f}", (12, 68),
                cv2.FONT_HERSHEY_SIMPLEX, .5, (220, 220, 220), 1)
    return panel
