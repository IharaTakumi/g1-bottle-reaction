#!/home/ubuntu/.venvs/g1-game-vision/bin/python
"""Pure model and log validators for the production Vision runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

import yaml

FRAME = re.compile(
    r"g1: (?P<resolution>\d+x\d+), FPS=(?P<fps>[0-9.]+), "
    r"local_age=(?P<age>[0-9.]+)ms, frames=(?P<frames>\d+), .*"
    r"RTP_lost=(?P<lost>\d+), status=(?P<status>\S+)"
)
YOLO = re.compile(
    r"YOLO FPS=(?P<fps>[0-9.]+), inference=(?P<inference>[0-9.]+)ms, "
    r"result_age=(?P<age>[0-9.]+)ms, status=(?P<status>\S+)"
)
FATAL = ("CAMERA WORKER ERROR:", "YOLO ERROR:", "G1 camera sender exited:")


def load_config(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("VISION_CONFIG_INVALID")
    return data


def verify_model(config: dict) -> dict:
    model = Path(config["model"])
    if not model.is_file() or model.stat().st_size == 0:
        raise ValueError(f"VISION_MODEL_MISSING {model}")
    digest = hashlib.sha256(model.read_bytes()).hexdigest()
    if digest != config["model_sha256"]:
        raise ValueError(f"VISION_MODEL_MISMATCH expected={config['model_sha256']} actual={digest}")
    return {"path": str(model), "sha256": digest, "bytes": model.stat().st_size}


def validate_health(config: dict, log: Path, *, now: float | None = None) -> dict:
    if not log.is_file():
        raise ValueError("VISION_LOG_MISSING")
    now = time.time() if now is None else now
    status_age = max(0.0, now - log.stat().st_mtime)
    if status_age > float(config["maximum_status_age_seconds"]):
        raise ValueError(f"VISION_STATUS_STALE age={status_age:.3f}")
    text = log.read_text(errors="replace")
    for marker in FATAL:
        if marker in text:
            raise ValueError(f"CAMERA_TRANSPORT_NOT_READY fatal={marker}")
    frames = list(FRAME.finditer(text))
    yolo = list(YOLO.finditer(text))
    if not frames:
        raise ValueError("CAMERA_FRAME_NOT_READY")
    if not yolo:
        raise ValueError("YOLO_INFERENCE_NOT_READY")
    frame = frames[-1].groupdict()
    inference = yolo[-1].groupdict()
    numeric = [float(frame["fps"]), float(frame["age"]), float(inference["fps"]),
               float(inference["inference"]), float(inference["age"])]
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("VISION_NONFINITE_METRIC")
    checks = {
        "resolution": frame["resolution"] == config["expected_resolution"],
        "camera_fps": float(frame["fps"]) >= float(config["minimum_ready_fps"]),
        "frame_age": float(frame["age"]) <= float(config["maximum_frame_age_ms"]),
        "frames": int(frame["frames"]) >= int(config["minimum_ready_frames"]),
        "rtp_lost": int(frame["lost"]) <= int(config["maximum_rtp_lost"]),
        "camera_status": frame["status"] == "LIVE",
        "cuda": "YOLO device: CUDA /" in text,
        "model_load": f"Model: {config['model']};" in text,
        "yolo_status": inference["status"] == "RUNNING",
        "warmup_inference": float(inference["inference"]) >= 0.0,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError("VISION_NOT_READY " + ",".join(failed))
    return {
        "resolution": frame["resolution"], "camera_fps": float(frame["fps"]),
        "frame_age_ms": float(frame["age"]), "frames": int(frame["frames"]),
        "rtp_lost": int(frame["lost"]), "yolo_fps": float(inference["fps"]),
        "inference_ms": float(inference["inference"]), "status_age_s": status_age,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("model", "health"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        result = verify_model(config) if args.kind == "model" else validate_health(config, args.log)
    except (KeyError, OSError, ValueError) as exc:
        parser.exit(3, f"{exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
