"""Shared local control-plane timing; no hardware or background activity."""

from dataclasses import dataclass
import math
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ControlTiming:
    heartbeat_interval_s: float
    lease_timeout_s: float
    ipc_read_timeout_s: float


def load_control_timing() -> ControlTiming:
    with Path(__file__).with_suffix(".yaml").open(encoding="utf-8") as stream:
        timing = ControlTiming(**yaml.safe_load(stream))
    values = vars(timing).values()
    if not all(type(value) in (int, float) and math.isfinite(value) and value > 0
               for value in values):
        raise ValueError("control timing must be finite and positive")
    if not timing.heartbeat_interval_s < timing.lease_timeout_s <= 0.40:
        raise ValueError("heartbeat interval < lease timeout <= 0.40 s required")
    if timing.ipc_read_timeout_s >= timing.lease_timeout_s:
        raise ValueError("IPC deadline must be shorter than the lease")
    return timing
