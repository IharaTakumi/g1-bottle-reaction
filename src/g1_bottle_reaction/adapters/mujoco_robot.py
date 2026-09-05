from __future__ import annotations

from dataclasses import replace
import importlib
import logging
from pathlib import Path
import queue
import threading
import time
from typing import Any

from g1_bottle_reaction.simulation.motion import (
    AnimationPlayer,
    MotionAnimation,
    MotionKeyframe,
    MotionLibrary,
    default_motion_config_path,
    load_motion_library,
    sanitize_pose,
)

from .robot import RobotAdapter

LOGGER = logging.getLogger(__name__)
TRACKING_JOINT = "waist_yaw_joint"


def default_g1_model_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "models"
        / "unitree_mujoco"
        / "unitree_robots"
        / "g1"
        / "scene_29dof.xml"
    )


class MujocoRobotAdapter(RobotAdapter):
    """Non-physical G1 reaction viewer using kinematic qpos animation."""

    def __init__(
        self,
        *,
        model_path: str | Path | None = None,
        motion_config_path: str | Path | None = None,
        launch_viewer: bool = True,
    ) -> None:
        self.model_path = Path(model_path or default_g1_model_path()).resolve()
        self.motion_config_path = Path(
            motion_config_path or default_motion_config_path()
        ).resolve()
        self.launch_viewer = launch_viewer
        self.library = load_motion_library(self.motion_config_path)
        self._mujoco: Any | None = None
        self._model: Any | None = None
        self._data: Any | None = None
        self._viewer: Any | None = None
        self._joint_qpos_addresses: dict[str, int] = {}
        self._joint_limits: dict[str, tuple[float, float] | None] = {}
        self._stand_qpos: Any | None = None
        self._attention_yaw = 0.0
        self._attention_lock = threading.Lock()
        self._commands: queue.Queue[str | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._viewer_closed = threading.Event()
        self._queue_lock = threading.Lock()
        self._warned: set[str] = set()
        self._error: Exception | None = None

    @property
    def available_motions(self) -> tuple[str, ...]:
        return self.library.names

    @property
    def model(self) -> Any | None:
        return self._model

    @property
    def animation_state(self) -> str:
        if self._worker is None:
            return "CLOSED"
        return "READY" if self._idle.is_set() else "PLAYING"

    def initialize(self) -> None:
        if self._worker is not None:
            raise RuntimeError("MujocoRobotAdapter is already initialized")
        if not self.model_path.is_file():
            raise RuntimeError(
                f"Official Unitree G1 model not found: {self.model_path}. "
                "Run: powershell -ExecutionPolicy Bypass -File scripts\\setup_g1_model.ps1"
            )
        mujoco, viewer_module = _import_mujoco()
        try:
            model = mujoco.MjModel.from_xml_path(str(self.model_path))
            data = mujoco.MjData(model)
        except Exception as exc:
            raise RuntimeError(
                f"Could not load MuJoCo G1 model {self.model_path}: {exc}"
            ) from exc

        self._mujoco = mujoco
        self._model = model
        self._data = data
        limits, addresses = _joint_metadata(mujoco, model)
        self._joint_qpos_addresses = addresses
        self._joint_limits = limits
        if TRACKING_JOINT not in addresses:
            self._warn_once(
                f"MuJoCo tracking joint '{TRACKING_JOINT}' is unavailable; "
                "continuous tracking will be ignored"
            )
        self.library = _sanitized_library(self.library, limits, self._warn_once)
        self._stand_qpos = model.qpos0.copy()
        self._write_pose(self.library.stand_pose)
        mujoco.mj_forward(model, data)

        if self.launch_viewer:
            try:
                self._viewer = viewer_module.launch_passive(model, data)
            except Exception as exc:
                self._clear_runtime()
                raise RuntimeError(f"Could not open MuJoCo viewer: {exc}") from exc

        self._stop.clear()
        self._error = None
        self._viewer_closed.clear()
        self._worker = threading.Thread(
            target=self._run,
            name="mujoco-animation-worker",
            daemon=True,
        )
        self._worker.start()
        print("Virtual G1: ready", flush=True)
        print(f"Model: {self.model_path}", flush=True)

    def play_motion(self, motion: str) -> None:
        if self._worker is None:
            raise RuntimeError("MujocoRobotAdapter is not initialized")
        if self._stop.is_set():
            LOGGER.warning("MuJoCo viewer is closed; motion '%s' was ignored", motion)
            return
        self.library.animation(motion)
        with self._queue_lock:
            self._idle.clear()
            self._commands.put(motion)

    def set_attention_yaw(self, yaw_radians: float) -> None:
        """Apply preview-only waist yaw; no physical G1 command is issued."""

        with self._attention_lock:
            self._attention_yaw = float(yaw_radians)

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        ready = self._idle.wait(timeout)
        if self._error is not None:
            raise RuntimeError(f"MuJoCo animation failed: {self._error}") from self._error
        return ready

    def wait_for_motion_complete(self, motion: str, timeout: float | None = None) -> bool:
        del motion
        return self.wait_for_idle(timeout)

    def wait_until_viewer_closed(self) -> None:
        if self._viewer is None:
            raise RuntimeError("MuJoCo viewer was not launched")
        try:
            while not self._viewer_closed.wait(0.2):
                pass
        except KeyboardInterrupt:
            return

    def close(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            self._stop.set()
            self._commands.put(None)
            worker.join(timeout=3)
        viewer = self._viewer
        self._viewer = None
        if viewer is not None:
            try:
                viewer.close()
            except Exception:
                LOGGER.debug("MuJoCo viewer close failed", exc_info=True)
        self._viewer_closed.set()
        self._idle.set()
        self._clear_runtime()

    def _run(self) -> None:
        assert self._mujoco is not None
        assert self._model is not None
        assert self._data is not None
        interval = 1.0 / self.library.fps
        player = AnimationPlayer(self.library)
        try:
            while not self._stop.is_set():
                tick_started = time.monotonic()
                if player.current_motion is None:
                    try:
                        command = self._commands.get_nowait()
                    except queue.Empty:
                        command = ""
                    if command is None:
                        return
                    if command:
                        player.start(command, now=tick_started)
                        print(f"[MOTION START] {command}", flush=True)

                pose, completed = player.update(now=tick_started)
                self._sync_pose(pose)
                if completed is not None:
                    print(f"[MOTION END] {completed}", flush=True)
                    with self._queue_lock:
                        if self._commands.empty():
                            self._idle.set()

                viewer = self._viewer
                if viewer is not None and not viewer.is_running():
                    self._viewer_closed.set()
                    self._stop.set()
                    return
                remaining = interval - (time.monotonic() - tick_started)
                if remaining > 0:
                    self._stop.wait(remaining)
        except Exception as exc:
            self._error = exc
            LOGGER.exception("MuJoCo animation worker failed")
            self._stop.set()
        finally:
            self._viewer_closed.set()
            self._idle.set()

    def _sync_pose(self, pose: dict[str, float]) -> None:
        assert self._mujoco is not None
        assert self._model is not None
        assert self._data is not None
        viewer = self._viewer
        if viewer is None:
            self._write_pose(pose)
            self._mujoco.mj_forward(self._model, self._data)
            return
        with viewer.lock():
            self._write_pose(pose)
            self._mujoco.mj_forward(self._model, self._data)
        viewer.sync()

    def _write_pose(self, pose: dict[str, float]) -> None:
        assert self._data is not None
        assert self._stand_qpos is not None or self._model is not None
        if self._stand_qpos is None:
            base = self._model.qpos0
        else:
            base = self._stand_qpos
        self._data.qpos[:] = base
        self._data.qvel[:] = 0
        for name, value in pose.items():
            self._data.qpos[self._joint_qpos_addresses[name]] = value
        yaw_address = self._joint_qpos_addresses.get(TRACKING_JOINT)
        if yaw_address is not None:
            with self._attention_lock:
                attention_yaw = self._attention_yaw
            value = compose_tracking_yaw(
                pose,
                attention_yaw=attention_yaw,
                joint_limits=self._joint_limits,
            )
            if value is not None:
                self._data.qpos[yaw_address] = value

    def _warn_once(self, message: str) -> None:
        if message not in self._warned:
            self._warned.add(message)
            LOGGER.warning(message)

    def _clear_runtime(self) -> None:
        self._mujoco = None
        self._model = None
        self._data = None
        self._stand_qpos = None
        self._joint_qpos_addresses = {}
        self._joint_limits = {}
        with self._attention_lock:
            self._attention_yaw = 0.0


def _import_mujoco():
    try:
        mujoco = importlib.import_module("mujoco")
        viewer = importlib.import_module("mujoco.viewer")
    except ImportError as exc:
        raise RuntimeError(
            'MuJoCo support requires: pip install -e ".[sim]"'
        ) from exc
    return mujoco, viewer


def compose_tracking_yaw(
    pose: dict[str, float],
    *,
    attention_yaw: float,
    joint_limits: dict[str, tuple[float, float] | None],
) -> float | None:
    """Compose base animation yaw plus tracking offset and clamp to model limits."""

    if TRACKING_JOINT not in joint_limits:
        return None
    value = float(pose.get(TRACKING_JOINT, 0.0)) + float(attention_yaw)
    limits = joint_limits[TRACKING_JOINT]
    if limits is not None:
        value = max(limits[0], min(limits[1], value))
    return value


def _joint_metadata(mujoco, model) -> tuple[
    dict[str, tuple[float, float] | None], dict[str, int]
]:
    limits: dict[str, tuple[float, float] | None] = {}
    addresses: dict[str, int] = {}
    scalar_types = {mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE}
    for joint_id in range(model.njnt):
        if model.jnt_type[joint_id] not in scalar_types:
            continue
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, joint_id)
        if not name:
            continue
        addresses[name] = int(model.jnt_qposadr[joint_id])
        if bool(model.jnt_limited[joint_id]):
            limits[name] = tuple(float(item) for item in model.jnt_range[joint_id])
        else:
            limits[name] = None
    return limits, addresses


def _sanitized_library(
    library: MotionLibrary,
    limits: dict[str, tuple[float, float] | None],
    warn,
) -> MotionLibrary:
    motions: dict[str, MotionAnimation] = {}
    for name, animation in library.motions.items():
        motions[name] = replace(
            animation,
            keyframes=tuple(
                MotionKeyframe(
                    keyframe.time_seconds,
                    sanitize_pose(keyframe.pose, limits, warn=warn),
                )
                for keyframe in animation.keyframes
            ),
        )
    return replace(library, motions=motions)
