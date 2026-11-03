#!/usr/bin/env python3
"""Log music detection from the G1 microphone. Receive-only: no robot command.

The microphone is multicast on the G1 internal network, so a small relay on PC2
streams the PCM over SSH and YAMNet runs here.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import wave
from typing import BinaryIO, Callable, TextIO

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from g1_bottle_reaction.audio.models import AudioChunk  # noqa: E402
from g1_bottle_reaction.audio.pipeline import AudioProcessor  # noqa: E402
from g1_bottle_reaction.config.loader import load_config  # noqa: E402

SAMPLE_RATE = 16000
PACKET_BYTES = 5120
MIC_RELAY = """
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("", 5555))
s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
             socket.inet_aton("239.168.123.161") + socket.inet_aton("192.168.123.164"))
while True:
    sys.stdout.buffer.write(s.recv(65536)); sys.stdout.buffer.flush()
"""


def mic_relay_command(target: str, control: str | None) -> list[str]:
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if control:
        command += ["-S", control]
    return command + ["--", target, "python3 -u -c " + shlex.quote(MIC_RELAY)]


def detect(stream: BinaryIO, processor: AudioProcessor, *, duration: float,
           log: TextIO | None = None, recording: wave.Wave_write | None = None,
           clock: Callable[[], float] = time.monotonic) -> dict:
    """Run PCM through the existing pipeline until the duration or end of stream."""
    start = clock(); scores: list[float] = []; labels: Counter[str] = Counter(); events = []
    while clock() - start < duration:
        raw = stream.read(PACKET_BYTES)
        if not raw:
            break
        samples = np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2")
        if recording is not None:
            recording.writeframes(samples.tobytes())
        now = clock()
        update = processor.process_chunk(AudioChunk(samples, SAMPLE_RATE), now=now)
        if update is None:
            continue
        scores.append(update.music_score)
        top = [(item.label, round(item.score, 3)) for item in update.top_predictions[:3]]
        if top:
            labels[top[0][0]] += 1
        event = update.event.value if update.event is not None else None
        if event:
            events.append({"t": round(now - start, 2), "event": event,
                           "score": round(update.music_score, 3)})
            print(f"*** {event} score={update.music_score:.3f} ***", flush=True)
        print(f"{now - start:6.1f}s music={update.music_score:.3f} state={update.state.value:15s} "
              f"rms={update.rms:.4f} peak={update.peak_amplitude:.3f} top={top}", flush=True)
        if log is not None:
            log.write(json.dumps({"t": round(now - start, 3), "music": update.music_score,
                                  "state": update.state.value, "event": event,
                                  "rms": update.rms, "peak": update.peak_amplitude,
                                  "top": top}, ensure_ascii=False) + "\n")
            log.flush()
    return {"inferences": len(scores),
            "music_max": round(max(scores), 3) if scores else None,
            "music_mean": round(float(np.mean(scores)), 3) if scores else None,
            "over_start_threshold": sum(s >= processor.tracker.config.music_start_threshold
                                        for s in scores),
            "top_labels": labels.most_common(5), "events": events}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--ssh-target", required=True)
    value.add_argument("--ssh-control")
    value.add_argument("--duration", type=float, default=30.0)
    value.add_argument("--log", type=Path)
    value.add_argument("--save-wav", type=Path, help="Keep what the microphone heard")
    value.add_argument("--config", type=Path, default=ROOT / "config/default.yaml")
    return value


def main() -> int:
    args = parser().parse_args()
    # YOLO keeps the GPU; YAMNet needs only milliseconds of CPU per inference.
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
    from g1_bottle_reaction.audio.classifiers import YamnetClassifier

    audio = load_config(args.config).audio
    os.chdir(ROOT)
    classifier = YamnetClassifier(model_url=audio.yamnet.model_url, cache_dir=audio.yamnet.cache_dir,
                                  music_labels=audio.yamnet.music_labels, top_n=audio.yamnet.top_n)
    process = subprocess.Popen(mic_relay_command(args.ssh_target, args.ssh_control),
                               stdout=subprocess.PIPE)
    log = args.log.open("w", encoding="utf-8") if args.log else None
    recording = None
    if args.save_wav:
        recording = wave.open(str(args.save_wav), "wb")
        recording.setnchannels(1); recording.setsampwidth(2); recording.setframerate(SAMPLE_RATE)
    try:
        summary = detect(process.stdout, AudioProcessor(audio, classifier),
                         duration=args.duration, log=log, recording=recording)
    finally:
        process.terminate()
        if log is not None:
            log.close()
        if recording is not None:
            recording.close()
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    print("G1_ROBOT_COMMAND_SENT=NONE", flush=True)
    return 0 if summary["inferences"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
