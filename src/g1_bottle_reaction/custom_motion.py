from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from pathlib import Path
import threading
import time
from typing import Callable, Mapping, Protocol

import yaml

LOGGER = logging.getLogger(__name__)

# Verified in Unitree's g1_arm7_sdk_dds_example.py, not inferred.
OFFICIAL_RIGHT_ARM_INDICES = {
    "right_shoulder_pitch": 22,
    "right_shoulder_roll": 23,
    "right_elbow": 25,
}
OFFICIAL_RIGHT_ARM_MODEL_JOINTS = {
    "right_shoulder_pitch": "right_shoulder_pitch_joint",
    "right_shoulder_roll": "right_shoulder_roll_joint",
    "right_elbow": "right_elbow_joint",
}
OFFICIAL_RIGHT_ARM_LIMITS = {
    "right_shoulder_pitch": (-3.0892, 2.6704),
    "right_shoulder_roll": (-2.2515, 1.5882),
    "right_elbow": (-1.0472, 2.0944),
}
ARM_SDK_WEIGHT_INDEX = 29


@dataclass(frozen=True, slots=True)
class CustomJoint:
    name: str
    sdk_index: int
    model_joint: str
    minimum: float
    maximum: float


@dataclass(frozen=True, slots=True)
class RelativeKeyframe:
    time_seconds: float
    offsets: dict[str, float]


@dataclass(frozen=True, slots=True)
class RelativeMotion:
    name: str
    keyframes: tuple[RelativeKeyframe, ...]

    @property
    def duration_seconds(self) -> float:
        return self.keyframes[-1].time_seconds


@dataclass(frozen=True, slots=True)
class CustomMotionConfig:
    update_period_seconds: float
    low_state_timeout_seconds: float
    kp: float
    kd: float
    safety_margin_radians: float
    max_delta_per_step_radians: float
    release_duration_seconds: float
    speech_delay_seconds: float
    joints: dict[str, CustomJoint]
    amplitude_profiles: dict[str, float]
    motions: dict[str, RelativeMotion]


@dataclass(frozen=True, slots=True)
class TrajectorySample:
    time_seconds: float
    positions: dict[str, float]


@dataclass(frozen=True, slots=True)
class TrajectoryReport:
    motion: str
    amplitude: str
    base_pose: dict[str, float]
    samples: tuple[TrajectorySample, ...]
    target_minimums: dict[str, float]
    target_maximums: dict[str, float]
    max_delta_per_step: float

    @property
    def duration_seconds(self) -> float:
        return self.samples[-1].time_seconds


class ArmSdkTransport(Protocol):
    def wait_for_pose(
        self, joint_indices: Mapping[str, int], timeout_seconds: float
    ) -> dict[str, float] | None: ...

    def write(
        self,
        positions: Mapping[str, float],
        joint_indices: Mapping[str, int],
        *,
        weight: float,
        kp: float,
        kd: float,
    ) -> None: ...


class MotionOwnership:
    """Mutual exclusion between preset arm Actions and custom arm_sdk control."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: str | None = None

    @property
    def owner(self) -> str | None:
        return self._owner

    def acquire(self, owner: str) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        self._owner = owner
        return True

    def release(self, owner: str) -> None:
        if self._owner != owner:
            return
        self._owner = None
        self._lock.release()


def default_custom_motion_config_path() -> Path:
    repository = (
        Path(__file__).resolve().parents[2] / "config" / "custom_g1_motions.yaml"
    )
    if repository.is_file():
        return repository
    return Path(__file__).resolve().parent / "config" / "custom_g1_motions.yaml"


def load_custom_motion_config(
    path: str | Path | None = None,
) -> CustomMotionConfig:
    config_path = Path(path) if path is not None else default_custom_motion_config_path()
    with config_path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    joints = {
        str(name): CustomJoint(
            name=str(name),
            sdk_index=int(item["sdk_index"]),
            model_joint=str(item["model_joint"]),
            minimum=float(item["minimum"]),
            maximum=float(item["maximum"]),
        )
        for name, item in raw["joints"].items()
    }
    if set(joints) != set(OFFICIAL_RIGHT_ARM_INDICES):
        raise ValueError("custom motion may control only the three verified right-arm joints")
    for name, joint in joints.items():
        if joint.sdk_index != OFFICIAL_RIGHT_ARM_INDICES[name]:
            raise ValueError(f"Unverified Unitree SDK joint index for {name}")
        if joint.model_joint != OFFICIAL_RIGHT_ARM_MODEL_JOINTS[name]:
            raise ValueError(f"Unverified official model joint name for {name}")
        if (joint.minimum, joint.maximum) != OFFICIAL_RIGHT_ARM_LIMITS[name]:
            raise ValueError(f"Unverified official joint range for {name}")
        if not joint.minimum < joint.maximum:
            raise ValueError(f"Invalid safe joint definition for {name}")
    motions: dict[str, RelativeMotion] = {}
    for name, item in raw["motions"].items():
        keyframes = tuple(
            RelativeKeyframe(
                time_seconds=float(frame["time"]),
                offsets={str(k): float(v) for k, v in frame["offsets"].items()},
            )
            for frame in item["keyframes"]
        )
        _validate_keyframes(str(name), keyframes, joints)
        motions[str(name)] = RelativeMotion(str(name), keyframes)
    config = CustomMotionConfig(
        update_period_seconds=float(raw["update_period_seconds"]),
        low_state_timeout_seconds=float(raw["low_state_timeout_seconds"]),
        kp=float(raw["kp"]),
        kd=float(raw["kd"]),
        safety_margin_radians=float(raw["safety_margin_radians"]),
        max_delta_per_step_radians=float(raw["max_delta_per_step_radians"]),
        release_duration_seconds=float(raw["release_duration_seconds"]),
        speech_delay_seconds=float(raw["speech_delay_seconds"]),
        joints=joints,
        amplitude_profiles={
            str(name): float(item["scale"])
            for name, item in raw["amplitude_profiles"].items()
        },
        motions=motions,
    )
    _validate_config(config)
    return config


def build_relative_trajectory(
    config: CustomMotionConfig,
    motion_name: str,
    amplitude: str,
    base_pose: Mapping[str, float],
) -> TrajectoryReport:
    try:
        motion = config.motions[motion_name]
        scale = config.amplitude_profiles[amplitude]
    except KeyError as exc:
        raise ValueError(f"Unknown custom motion or amplitude: {exc.args[0]}") from exc
    missing = set(config.joints) - set(base_pose)
    if missing:
        raise RuntimeError("Current LowState pose is missing joints: " + ", ".join(sorted(missing)))
    for name, joint in config.joints.items():
        value = float(base_pose[name])
        safe_low = joint.minimum + config.safety_margin_radians
        safe_high = joint.maximum - config.safety_margin_radians
        if not math.isfinite(value):
            raise RuntimeError(f"Current LowState pose for {name} is not finite")
        if not safe_low <= value <= safe_high:
            raise RuntimeError(
                f"Current LowState pose for {name} is outside the software safety envelope"
            )
    count = math.ceil(motion.duration_seconds / config.update_period_seconds)
    samples: list[TrajectorySample] = []
    previous = {name: float(base_pose[name]) for name in config.joints}
    minimums = dict(previous)
    maximums = dict(previous)
    observed_max_delta = 0.0
    for index in range(count + 1):
        sample_time = min(index * config.update_period_seconds, motion.duration_seconds)
        offsets = _sample_offsets(motion, sample_time)
        positions: dict[str, float] = {}
        for name, joint in config.joints.items():
            requested = float(base_pose[name]) + offsets[name] * scale
            safe_low = joint.minimum + config.safety_margin_radians
            safe_high = joint.maximum - config.safety_margin_radians
            clamped = min(max(requested, safe_low), safe_high)
            delta = clamped - previous[name]
            bounded_delta = min(
                max(delta, -config.max_delta_per_step_radians),
                config.max_delta_per_step_radians,
            )
            value = previous[name] + bounded_delta
            positions[name] = value
            observed_max_delta = max(observed_max_delta, abs(bounded_delta))
            minimums[name] = min(minimums[name], value)
            maximums[name] = max(maximums[name], value)
        samples.append(TrajectorySample(sample_time, positions))
        previous = positions
    if any(
        abs(samples[-1].positions[name] - float(base_pose[name])) > 1e-6
        for name in config.joints
    ):
        raise RuntimeError("Configured trajectory cannot return to base pose within max step delta")
    return TrajectoryReport(
        motion=motion_name,
        amplitude=amplitude,
        base_pose={name: float(base_pose[name]) for name in config.joints},
        samples=tuple(samples),
        target_minimums=minimums,
        target_maximums=maximums,
        max_delta_per_step=observed_max_delta,
    )


class CustomArmMotionController:
    def __init__(
        self,
        config: CustomMotionConfig,
        transport: ArmSdkTransport,
        ownership: MotionOwnership,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.transport = transport
        self.ownership = ownership
        self._sleep = sleep
        self._clock = clock
        self._busy = threading.Lock()
        self._worker: threading.Thread | None = None
        self.error: Exception | None = None

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    def preflight(self) -> dict[str, float]:
        indices = {name: joint.sdk_index for name, joint in self.config.joints.items()}
        pose = self.transport.wait_for_pose(
            indices, self.config.low_state_timeout_seconds
        )
        if pose is None:
            raise RuntimeError("LowState was not received before timeout")
        missing = set(indices) - set(pose)
        if missing:
            raise RuntimeError(
                "Current LowState pose is missing joints: "
                + ", ".join(sorted(missing))
            )
        return pose

    def start(
        self,
        motion: str,
        amplitude: str,
        *,
        timeline_start: float | None = None,
        timing_debug: bool = False,
    ) -> bool:
        if not self._busy.acquire(blocking=False):
            LOGGER.warning("Custom arm motion rejected: controller is busy")
            return False
        if not self.ownership.acquire("custom"):
            self._busy.release()
            LOGGER.warning("Custom arm motion rejected: preset/custom arm control is busy")
            return False
        self.error = None
        self._worker = threading.Thread(
            target=self._run,
            args=(motion, amplitude, timeline_start, timing_debug),
            name="g1-custom-arm-motion",
            daemon=True,
        )
        self._worker.start()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                return False
        if self.error is not None:
            raise RuntimeError(f"Custom arm motion failed: {self.error}") from self.error
        return True

    def close(self) -> None:
        self.wait(3.0)

    def _run(
        self,
        motion: str,
        amplitude: str,
        timeline_start: float | None,
        timing_debug: bool,
    ) -> None:
        acquired = False
        base_pose: dict[str, float] | None = None
        started = timeline_start if timeline_start is not None else self._clock()
        try:
            indices = {name: joint.sdk_index for name, joint in self.config.joints.items()}
            base_pose = self.preflight()
            report = build_relative_trajectory(self.config, motion, amplitude, base_pose)
            pose_reached = self.config.motions[motion].keyframes[1].time_seconds
            return_started = self.config.motions[motion].keyframes[-2].time_seconds
            logged_pose = False
            logged_return = False
            command_started = self._clock()
            for sample in report.samples:
                deadline = command_started + sample.time_seconds
                remaining = deadline - self._clock()
                if remaining > 0:
                    self._sleep(remaining)
                self.transport.write(
                    sample.positions,
                    indices,
                    weight=1.0,
                    kp=self.config.kp,
                    kd=self.config.kd,
                )
                acquired = True
                elapsed = self._clock() - started
                if timing_debug and sample.time_seconds == 0:
                    LOGGER.info("[MOTION] first command t=%.3f", elapsed)
                if timing_debug and not logged_pose and sample.time_seconds >= pose_reached:
                    LOGGER.info("[MOTION] notice pose reached t=%.3f", elapsed)
                    logged_pose = True
                if timing_debug and not logged_return and sample.time_seconds >= return_started:
                    LOGGER.info("[MOTION] return start t=%.3f", elapsed)
                    logged_return = True
        except Exception as exc:
            self.error = exc
            LOGGER.exception("Custom arm motion stopped")
        finally:
            if acquired and base_pose is not None:
                self._release_control(base_pose)
            if timing_debug:
                LOGGER.info("[MOTION] END t=%.3f", self._clock() - started)
            self.ownership.release("custom")
            self._busy.release()

    def _release_control(self, base_pose: Mapping[str, float]) -> None:
        indices = {name: joint.sdk_index for name, joint in self.config.joints.items()}
        steps = max(
            1,
            math.ceil(
                self.config.release_duration_seconds
                / self.config.update_period_seconds
            ),
        )
        try:
            for step in range(steps):
                weight = max(0.0, 1.0 - (step + 1) / steps)
                self.transport.write(
                    base_pose,
                    indices,
                    weight=weight,
                    kp=self.config.kp,
                    kd=self.config.kd,
                )
                self._sleep(self.config.update_period_seconds)
        except Exception:
            LOGGER.exception("Custom arm control release failed")


def format_dry_run(report: TrajectoryReport, config: CustomMotionConfig) -> str:
    lines = [
        "Custom motion dry-run (NO G1 COMMANDS)",
        f"Motion: {report.motion}",
        f"Amplitude: {report.amplitude}",
        f"Base pose: {report.base_pose}",
        f"Duration: {report.duration_seconds:.3f} s",
        f"Number of samples: {len(report.samples)}",
        f"Max delta per step: {report.max_delta_per_step:.6f} rad",
        f"Configured max delta: {config.max_delta_per_step_radians:.6f} rad",
        f"Safety margin: {config.safety_margin_radians:.3f} rad",
        "Joint range validation: OK",
    ]
    for name in config.joints:
        lines.append(
            f"  {name}: offset-range="
            f"[{report.target_minimums[name] - report.base_pose[name]:+.4f}, "
            f"{report.target_maximums[name] - report.base_pose[name]:+.4f}] "
            f"target=[{report.target_minimums[name]:+.4f}, "
            f"{report.target_maximums[name]:+.4f}]"
        )
    return "\n".join(lines)


def _sample_offsets(motion: RelativeMotion, at: float) -> dict[str, float]:
    if at <= motion.keyframes[0].time_seconds:
        return dict(motion.keyframes[0].offsets)
    if at >= motion.duration_seconds:
        return dict(motion.keyframes[-1].offsets)
    for left, right in zip(motion.keyframes, motion.keyframes[1:]):
        if at <= right.time_seconds:
            ratio = (at - left.time_seconds) / (right.time_seconds - left.time_seconds)
            eased = ratio * ratio * (3.0 - 2.0 * ratio)
            return {
                name: left.offsets[name]
                + (right.offsets[name] - left.offsets[name]) * eased
                for name in left.offsets
            }
    return dict(motion.keyframes[-1].offsets)


def _validate_keyframes(
    name: str,
    keyframes: tuple[RelativeKeyframe, ...],
    joints: Mapping[str, CustomJoint],
) -> None:
    if len(keyframes) < 2 or keyframes[0].time_seconds != 0:
        raise ValueError(f"Custom motion '{name}' must start at t=0")
    previous = -1.0
    for frame in keyframes:
        if frame.time_seconds <= previous or set(frame.offsets) != set(joints):
            raise ValueError(f"Invalid keyframe in custom motion '{name}'")
        if any(not math.isfinite(value) for value in frame.offsets.values()):
            raise ValueError(f"Non-finite offset in custom motion '{name}'")
        previous = frame.time_seconds
    if any(abs(value) > 1e-12 for value in keyframes[0].offsets.values()):
        raise ValueError("Custom motion must begin at the current pose")
    if any(abs(value) > 1e-12 for value in keyframes[-1].offsets.values()):
        raise ValueError("Custom motion must return to the current pose")


def _validate_config(config: CustomMotionConfig) -> None:
    positive = (
        config.update_period_seconds,
        config.low_state_timeout_seconds,
        config.kp,
        config.kd,
        config.safety_margin_radians,
        config.max_delta_per_step_radians,
        config.release_duration_seconds,
        config.speech_delay_seconds,
    )
    if any(value <= 0 or not math.isfinite(value) for value in positive):
        raise ValueError("Custom motion safety/timing values must be positive")
    if config.amplitude_profiles.get("small") != 0.25:
        raise ValueError("Real G1 default small amplitude must remain 0.25")
    if any(not 0 < scale <= 1 for scale in config.amplitude_profiles.values()):
        raise ValueError("Custom motion amplitude scales must be in (0, 1]")
    for joint in config.joints.values():
        if joint.maximum - joint.minimum <= 2 * config.safety_margin_radians:
            raise ValueError(f"Safety margin leaves no range for {joint.name}")
