from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import logging
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from g1_bottle_reaction.custom_motion import (
    ARM_SDK_WEIGHT_INDEX,
    CustomArmMotionController,
    CustomMotionConfig,
    MotionOwnership,
)

from .robot import RobotAdapter, TrackingCommand

LOGGER = logging.getLogger(__name__)

WINDOWS_CHANNEL_CONFIG = """<?xml version="1.0" encoding="UTF-8" ?>
<CycloneDDS>
  <Domain Id="any">
    <General>
      <Interfaces>
        <NetworkInterface address="$__IF_NAME__$" priority="default" multicast="default"/>
      </Interfaces>
    </General>
  </Domain>
</CycloneDDS>
"""

_CHANNEL_CONFIG_PATCH_LOCK = threading.Lock()


def serve_remote_cached_audio():
    """Standalone Python 3.8-compatible SSH server; existing G1 SDK, no files.

    Sent as function source to G1. Only speaker APIs are exposed. The PC keeps
    G1AudioOutput's existing WAV conversion, chunk pacing and error handling.
    """
    import base64
    import contextlib
    import json
    import os
    import resource
    import sys
    import time
    import xml.etree.ElementTree as ET

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.environ.pop("CYCLONEDDS_URI", None)
    channel = client = None
    active = False
    app = "g1_bottle_reaction"
    try:
        with contextlib.redirect_stdout(sys.stderr):
            import unitree_sdk2py.core.channel as channel
            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

            original = channel.ChannelConfigHasInterface
            root = ET.fromstring(original)
            for parent in root.iter():
                for child in list(parent):
                    if child.tag == "Tracing":
                        parent.remove(child)
            channel.ChannelConfigHasInterface = ET.tostring(root, encoding="unicode")
            try:
                channel.ChannelFactoryInitialize(0, "eth0")
            finally:
                channel.ChannelConfigHasInterface = original
            client = AudioClient()
            client.SetTimeout(1.0)
            client.Init()
            deadline = time.monotonic() + 10
            while True:
                code, volume = client.GetVolume()
                if code == 0:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("G1-local GetVolume failed: %s" % code)
                time.sleep(.1)
        print(json.dumps({"status": "ready", "volume": volume}), flush=True)
        while True:
            line = sys.stdin.readline(65537)
            if not line:
                break
            if len(line) > 65536 or not line.endswith("\n"):
                raise ValueError("oversized audio request")
            request = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                if request["operation"] == "PlayStream":
                    pcm = base64.b64decode(request["pcm"], validate=True)
                    if not 0 < len(pcm) <= 16000 or len(pcm) % 2:
                        raise ValueError("invalid PCM chunk")
                    active = True
                    result = client.PlayStream(app, str(request["stream_id"]), pcm)
                elif request["operation"] == "PlayStop":
                    result = client.PlayStop(app)
                    active = False
                else:
                    raise ValueError("unsupported audio operation")
            print(json.dumps({"status": "result", "result": result}), flush=True)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), flush=True)
    finally:
        if client is not None and active:
            with contextlib.redirect_stdout(sys.stderr):
                client.PlayStop(app)

ARM_ACTION_RPC_TIMEOUT_CODE = 3104
ARM_RELEASE_ACTION_ID = 99


@dataclass(frozen=True, slots=True)
class ArmActionSpec:
    action_id: int
    label: str
    requires_release: bool = False


@dataclass(frozen=True, slots=True)
class DdsNetworkSelection:
    platform: str
    interface: str | None
    address: str | None
    mode: str

    @property
    def initializer_value(self) -> str:
        value = self.address if self.mode == "address" else self.interface
        assert value is not None
        return value


class UnitreeSdkRuntime:
    """Lazy, process-wide owner of official Unitree SDK imports and DDS setup."""

    def __init__(
        self,
        symbol_loader: Callable[[], tuple[Any, Any]] | None = None,
        *,
        channel_module_loader: Callable[[], Any] | None = None,
        video_client_loader: Callable[[], Any] | None = None,
        arm_action_client_loader: Callable[[], Any] | None = None,
        arm_sdk_transport_loader: Callable[[], Any] | None = None,
        interface_resolver: Callable[[str], str] | None = None,
        platform_name: str | None = None,
    ):
        self._symbol_loader = symbol_loader
        self._channel_module_loader = channel_module_loader
        self._video_client_loader = video_client_loader
        self._arm_action_client_loader = arm_action_client_loader
        self._arm_sdk_transport_loader = arm_sdk_transport_loader
        self._interface_resolver = interface_resolver
        self._platform_name = platform_name or sys.platform
        self._symbols: tuple[Any, Any] | None = None
        self._network_selection: DdsNetworkSelection | None = None
        self._lock = threading.Lock()

    def _load_symbols(self) -> tuple[Any, Any]:
        if self._symbols is not None:
            return self._symbols
        if self._symbol_loader is not None:
            self._symbols = self._symbol_loader()
            return self._symbols
        try:
            # Keep every direct Unitree import in this one integration boundary.
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
        except ImportError as exc:
            raise RuntimeError(
                "unitree_sdk2_python is required in the G1 environment"
            ) from exc
        self._symbols = ChannelFactoryInitialize, AudioClient
        return self._symbols

    def _load_channel_module(self) -> Any:
        if self._channel_module_loader is not None:
            return self._channel_module_loader()
        try:
            import unitree_sdk2py.core.channel as channel_module
        except ImportError as exc:
            raise RuntimeError(
                "unitree_sdk2_python is required in the G1 environment"
            ) from exc
        return channel_module

    def load_readonly_low_state_type(self) -> Any:
        """Load the G1 state schema without initializing channels or clients."""
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

        return LowState_

    def load_readonly_slam_types(self) -> tuple[Any, Any]:
        """Schemas for observed SLAM telemetry only; no service initialization."""
        from unitree_sdk2py.idl.nav_msgs.msg.dds_ import Odometry_
        from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_

        return Odometry_, String_

    def load_readonly_navigation_types(self) -> dict[str, Any]:
        """Telemetry schemas only; never initialize a channel or RPC client."""
        from unitree_sdk2py.idl.nav_msgs.msg.dds_ import Odometry_
        from unitree_sdk2py.idl.sensor_msgs.msg.dds_ import PointCloud2_
        from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_

        return {"odometry": Odometry_, "cloud": PointCloud2_,
                "string": String_, "odomstate": SportModeState_}

    def _load_video_client_type(self) -> Any:
        if self._video_client_loader is not None:
            return self._video_client_loader()
        try:
            from unitree_sdk2py.go2.video.video_client import VideoClient
        except ImportError as exc:
            raise RuntimeError(
                "unitree_sdk2_python with VideoClient is required in the G1 environment"
            ) from exc
        return VideoClient

    def load_camera_symbols(self) -> tuple[Any, Any]:
        """Camera-only symbols with process-local, trace-free named-NIC setup.

        CycloneDDS 0.10.2 config tracing can abort in do_print_uint32_bitset
        on this Ubuntu build. Preserve explicit NIC selection; omit tracing.
        SDK files, OS configuration and other runtime entry points are untouched.
        """
        channel = self._load_channel_module()

        def initialize(domain: int, interface: str) -> None:
            import xml.etree.ElementTree as ET

            with _CHANNEL_CONFIG_PATCH_LOCK:
                original = channel.ChannelConfigHasInterface
                root = ET.fromstring(original)
                for parent in root.iter():
                    for child in list(parent):
                        if child.tag == "Tracing":
                            parent.remove(child)
                channel.ChannelConfigHasInterface = ET.tostring(root, encoding="unicode")
                try:
                    channel.ChannelFactoryInitialize(domain, interface)
                finally:
                    channel.ChannelConfigHasInterface = original

        return initialize, self._load_video_client_type()

    def _load_arm_action_client_type(self) -> Any:
        if self._arm_action_client_loader is not None:
            return self._arm_action_client_loader()
        try:
            from unitree_sdk2py.g1.arm.g1_arm_action_client import (
                G1ArmActionClient,
            )
        except ImportError as exc:
            raise RuntimeError(
                "unitree_sdk2_python with G1ArmActionClient is required in the "
                "G1 environment"
            ) from exc
        return G1ArmActionClient

    def initialize_channel(
        self,
        network_interface: str | None,
        network_address: str | None = None,
    ) -> None:
        selection = self._select_network(network_interface, network_address)
        with self._lock:
            if self._network_selection is not None:
                if self._network_selection != selection:
                    raise RuntimeError(
                        "Unitree DDS is already initialized with "
                        f"{self._network_selection}"
                    )
                return
            initializer, _ = self._load_symbols()
            if selection.mode == "address":
                self._initialize_windows_channel(initializer, selection)
            else:
                initializer(0, selection.initializer_value)
            self._network_selection = selection

    def _select_network(
        self,
        network_interface: str | None,
        network_address: str | None,
    ) -> DdsNetworkSelection:
        if self._platform_name == "win32":
            address = (
                normalize_ipv4_address(network_address)
                if network_address
                else (self._interface_resolver or resolve_windows_interface_ipv4)(
                    _require_interface(network_interface)
                )
            )
            return DdsNetworkSelection(
                platform="Windows",
                interface=network_interface or None,
                address=address,
                mode="address",
            )
        if network_address:
            raise ValueError("--network-address is supported only on Windows")
        return DdsNetworkSelection(
            platform=self._platform_name,
            interface=_require_interface(network_interface),
            address=None,
            mode="name",
        )

    def _initialize_windows_channel(
        self, initializer: Callable[[int, str], Any], selection: DdsNetworkSelection
    ) -> None:
        print("[G1 DDS] platform=Windows", flush=True)
        print(f"[G1 DDS] interface={selection.interface or '-'}", flush=True)
        print(f"[G1 DDS] address={selection.address}", flush=True)
        print("[G1 DDS] mode=address", flush=True)
        channel_module = self._load_channel_module()
        if not hasattr(channel_module, "ChannelConfigHasInterface"):
            raise RuntimeError(
                "Installed unitree_sdk2_python has no ChannelConfigHasInterface"
            )
        with _CHANNEL_CONFIG_PATCH_LOCK:
            original_config = channel_module.ChannelConfigHasInterface
            channel_module.ChannelConfigHasInterface = WINDOWS_CHANNEL_CONFIG
            try:
                try:
                    initializer(0, selection.initializer_value)
                except Exception as exc:
                    raise RuntimeError(
                        "Unitree DDS initialization failed for Windows IPv4 "
                        f"{selection.initializer_value}: {exc}"
                    ) from exc
            finally:
                channel_module.ChannelConfigHasInterface = original_config

    def create_audio_client(
        self,
        network_interface: str | None,
        timeout: float,
        network_address: str | None = None,
    ) -> Any:
        self.initialize_channel(network_interface, network_address)
        _, audio_client_type = self._load_symbols()
        client = audio_client_type()
        client.SetTimeout(timeout)
        client.Init()
        return client

    def create_arm_action_client(
        self,
        network_interface: str | None,
        timeout: float,
        network_address: str | None = None,
    ) -> Any:
        self.initialize_channel(network_interface, network_address)
        client_type = self._load_arm_action_client_type()
        client = client_type()
        client.SetTimeout(timeout)
        client.Init()
        return client

    def create_video_client(
        self,
        network_interface: str | None,
        timeout: float,
        network_address: str | None = None,
    ) -> Any:
        self.initialize_channel(network_interface, network_address)
        client_type = self._load_video_client_type()
        client = client_type()
        client.SetTimeout(timeout)
        client.Init()
        return client

    def create_arm_sdk_transport(
        self,
        network_interface: str | None,
        network_address: str | None = None,
    ) -> Any:
        self.initialize_channel(network_interface, network_address)
        if self._arm_sdk_transport_loader is not None:
            return self._arm_sdk_transport_loader()
        try:
            from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
            from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
            from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
            from unitree_sdk2py.utils.crc import CRC
        except ImportError as exc:
            raise RuntimeError(
                "unitree_sdk2_python arm_sdk DDS support is required in the G1 environment"
            ) from exc
        return UnitreeArmSdkTransport(
            ChannelPublisher,
            ChannelSubscriber,
            LowCmd_,
            LowState_,
            unitree_hg_msg_dds__LowCmd_,
            CRC,
        )


DEFAULT_UNITREE_RUNTIME = UnitreeSdkRuntime()


class UnitreeArmSdkTransport:
    """Exact DDS flow used by Unitree's G1 arm7 high-level example."""

    def __init__(
        self,
        publisher_type: Any,
        subscriber_type: Any,
        low_cmd_type: Any,
        low_state_type: Any,
        low_cmd_factory: Callable[[], Any],
        crc_type: Any,
    ) -> None:
        self._publisher = publisher_type("rt/arm_sdk", low_cmd_type)
        self._publisher.Init()
        self._subscriber = subscriber_type("rt/lowstate", low_state_type)
        self._state_lock = threading.Lock()
        self._state_event = threading.Event()
        self._low_state: Any | None = None
        self._subscriber.Init(self._on_low_state, 10)
        self._low_cmd_factory = low_cmd_factory
        self._crc = crc_type()

    def _on_low_state(self, message: Any) -> None:
        with self._state_lock:
            self._low_state = message
        self._state_event.set()

    def wait_for_pose(
        self, joint_indices: dict[str, int], timeout_seconds: float
    ) -> dict[str, float] | None:
        if not self._state_event.wait(timeout_seconds):
            return None
        with self._state_lock:
            state = self._low_state
            if state is None:
                return None
            try:
                return {
                    name: float(state.motor_state[index].q)
                    for name, index in joint_indices.items()
                }
            except (AttributeError, IndexError, TypeError, ValueError):
                return None

    def write(
        self,
        positions: dict[str, float] | Any,
        joint_indices: dict[str, int] | Any,
        *,
        weight: float,
        kp: float,
        kd: float,
    ) -> None:
        command = self._low_cmd_factory()
        command.motor_cmd[ARM_SDK_WEIGHT_INDEX].q = float(weight)
        for name, index in joint_indices.items():
            motor = command.motor_cmd[index]
            motor.tau = 0.0
            motor.q = float(positions[name])
            motor.dq = 0.0
            motor.kp = float(kp)
            motor.kd = float(kd)
        command.crc = self._crc.Crc(command)
        self._publisher.Write(command)


class G1RobotAdapter(RobotAdapter):
    """Explicitly gated high-level G1 actions; unsupported motions stay no-op."""

    VERIFIED_MOTIONS = {
        "notice": ArmActionSpec(23, "right hand up", requires_release=True),
        "spot_target": ArmActionSpec(26, "high wave"),
    }

    def __init__(
        self,
        network_interface: str,
        *,
        network_address: str | None = None,
        enabled: bool = False,
        motion_mode: str = "disabled",
        timeout_seconds: float = 10.0,
        release_delay_seconds: float = 2.0,
        custom_motion_enabled: bool = False,
        custom_motion_amplitude: str = "small",
        custom_motion_config: CustomMotionConfig | None = None,
        runtime: UnitreeSdkRuntime | Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        custom_controller: CustomArmMotionController | Any | None = None,
    ) -> None:
        if motion_mode not in {"disabled", "safe-actions"}:
            raise ValueError("G1 motion mode must be disabled or safe-actions")
        if motion_mode == "safe-actions" and not enabled:
            raise RuntimeError(
                "Real G1 motion requires both --robot g1 and --enable-real-robot"
            )
        if motion_mode == "safe-actions" and not (
            network_interface or network_address
        ):
            raise ValueError(
                "--network-interface or --network-address is required for real G1 motion"
            )
        if timeout_seconds <= 0:
            raise ValueError("G1 arm action timeout must be positive")
        if release_delay_seconds < 0:
            raise ValueError("G1 arm release delay cannot be negative")
        if custom_motion_enabled and not (
            enabled and motion_mode == "safe-actions"
        ):
            raise RuntimeError(
                "Custom G1 motion requires the existing real-robot safe-actions gates"
            )
        self.network_interface = network_interface
        self.network_address = network_address
        self.enabled = enabled
        self.motion_mode = motion_mode
        self.timeout_seconds = timeout_seconds
        self.release_delay_seconds = release_delay_seconds
        self.custom_motion_enabled = custom_motion_enabled
        self.custom_motion_amplitude = custom_motion_amplitude
        self.custom_motion_config = custom_motion_config
        self.runtime = runtime or DEFAULT_UNITREE_RUNTIME
        self._sleeper = sleeper
        self._arm_action_client: Any | None = None
        self._available_action_ids: frozenset[int] = frozenset()
        self._ownership = MotionOwnership()
        self._custom_controller = custom_controller
        self._preset_completion_unconfirmed = False
        self._last_motion_name: str | None = None
        self._last_motion_started = False

    @property
    def motion_enabled(self) -> bool:
        return self.enabled and self.motion_mode == "safe-actions"

    def initialize(self) -> None:
        if not self.motion_enabled:
            LOGGER.info("G1 motion is disabled; physical motion calls are safe no-ops")
            return
        if self.network_address is None:
            self._arm_action_client = self.runtime.create_arm_action_client(
                self.network_interface, self.timeout_seconds
            )
        else:
            self._arm_action_client = self.runtime.create_arm_action_client(
                self.network_interface,
                self.timeout_seconds,
                self.network_address,
            )
        self._available_action_ids = self._load_available_action_ids(
            self._arm_action_client
        )
        if self.custom_motion_enabled:
            if self.custom_motion_config is None:
                raise ValueError("Custom G1 motion configuration is required")
            if self._custom_controller is None:
                if self.network_address is None:
                    transport = self.runtime.create_arm_sdk_transport(
                        self.network_interface
                    )
                else:
                    transport = self.runtime.create_arm_sdk_transport(
                        self.network_interface, self.network_address
                    )
                self._custom_controller = CustomArmMotionController(
                    self.custom_motion_config,
                    transport,
                    self._ownership,
                )
            self._custom_controller.preflight()
        print("*** REAL G1 MOTION ENABLED ***", flush=True)
        print(
            "[G1 ARM] available action IDs="
            + ",".join(str(item) for item in sorted(self._available_action_ids)),
            flush=True,
        )
        if self.custom_motion_enabled:
            print("*** REAL G1 CUSTOM ARM MOTION ENABLED ***", flush=True)
            print("Motion: custom_notice", flush=True)
            print(f"Amplitude: {self.custom_motion_amplitude}", flush=True)

    def _load_available_action_ids(self, client: Any) -> frozenset[int]:
        result = client.GetActionList()
        if not isinstance(result, tuple) or len(result) < 2:
            raise RuntimeError("Unitree GetActionList returned an invalid response")
        code, payload = result[0], result[1]
        ensure_unitree_success(code, "GetActionList")
        action_ids = extract_action_ids(payload)
        if not action_ids:
            raise RuntimeError("Unitree GetActionList returned no recognizable Action IDs")
        required_ids = {
            spec.action_id for spec in self.VERIFIED_MOTIONS.values()
        } | {ARM_RELEASE_ACTION_ID}
        missing = required_ids - action_ids
        if missing:
            LOGGER.warning(
                "G1 Action List does not contain verified Action IDs: %s",
                ", ".join(str(item) for item in sorted(missing)),
            )
        return frozenset(action_ids)

    def play_motion(self, motion: str) -> None:
        self.play_motion_timed(
            motion, timeline_start=time.monotonic(), timing_debug=False
        )

    def play_motion_timed(
        self,
        motion: str,
        *,
        timeline_start: float,
        timing_debug: bool = False,
    ) -> bool:
        self._last_motion_name = motion
        self._last_motion_started = False
        if motion == "custom_notice":
            if not self.motion_enabled or not self.custom_motion_enabled:
                LOGGER.warning(
                    "G1 custom motion skipped because its additional safety gate is disabled"
                )
                return False
            if self._preset_completion_unconfirmed:
                LOGGER.warning(
                    "G1 custom motion rejected because preset Action completion "
                    "has not been confirmed"
                )
                return False
            if self._custom_controller is None:
                raise RuntimeError("G1 custom motion controller is not initialized")
            started = self._custom_controller.start(
                motion,
                self.custom_motion_amplitude,
                timeline_start=timeline_start,
                timing_debug=timing_debug,
            )
            self._last_motion_started = started
            return started
        action = self.VERIFIED_MOTIONS.get(motion)
        if not self.motion_enabled:
            LOGGER.warning("G1 motion '%s' skipped because motion is disabled", motion)
            return False
        if action is None:
            LOGGER.warning("Unsupported G1 motion '%s' is a safe no-op", motion)
            return False
        if self._arm_action_client is None:
            raise RuntimeError("G1RobotAdapter has not been initialized")
        required_ids = {action.action_id}
        if action.requires_release:
            required_ids.add(ARM_RELEASE_ACTION_ID)
        missing = required_ids - self._available_action_ids
        if missing:
            LOGGER.warning(
                "G1 motion '%s' is a safe no-op because Action IDs %s are unavailable",
                motion,
                ", ".join(str(item) for item in sorted(missing)),
            )
            return False
        if not self._ownership.acquire("preset"):
            LOGGER.warning("Preset G1 arm action skipped because arm control is busy")
            return False
        try:
            # TODO: subscribe to rt/arm/action/state to confirm action start/completion.
            result = self._arm_action_client.ExecuteAction(action.action_id)
            ensure_arm_action_result(result, action)
            self._preset_completion_unconfirmed = (
                unitree_result_code(result) == ARM_ACTION_RPC_TIMEOUT_CODE
                or not action.requires_release
            )
            if action.requires_release:
                self._sleeper(self.release_delay_seconds)
                release_result = self._arm_action_client.ExecuteAction(
                    ARM_RELEASE_ACTION_ID
                )
                ensure_arm_action_result(
                    release_result,
                    ArmActionSpec(ARM_RELEASE_ACTION_ID, "release arm"),
                )
                # A successful release cannot prove that a timed-out action
                # reached its intended physical completion boundary.
                self._preset_completion_unconfirmed = (
                    self._preset_completion_unconfirmed
                    or unitree_result_code(release_result)
                    == ARM_ACTION_RPC_TIMEOUT_CODE
                )
        finally:
            self._ownership.release("preset")
        self._last_motion_started = True
        return True

    def apply_tracking(self, command: TrackingCommand) -> None:
        # Phase 1 deliberately does not translate tracking yaw into real motion.
        del command

    def set_attention_yaw(self, yaw_radians: float) -> None:
        del yaw_radians

    def wait_for_custom_motion(self, timeout: float | None = None) -> bool:
        if self._custom_controller is None:
            return True
        return self._custom_controller.wait(timeout)

    def wait_for_motion_complete(self, motion: str, timeout: float | None = None) -> bool:
        if motion != self._last_motion_name or not self._last_motion_started:
            return False
        if motion == "custom_notice":
            return self.wait_for_custom_motion(timeout)
        if motion in self.VERIFIED_MOTIONS:
            # Until rt/arm/action/state is implemented, only the releasable Action
            # has a conservative completion boundary after a successful release.
            return not self._preset_completion_unconfirmed
        return False

    def close(self) -> None:
        if self._custom_controller is not None:
            self._custom_controller.close()


def ensure_unitree_success(result: Any, operation: str) -> None:
    code = result[0] if isinstance(result, tuple) and result else result
    if code is not None and code != 0:
        raise RuntimeError(f"Unitree {operation} failed with return code {code}")


def ensure_arm_action_result(result: Any, action: ArmActionSpec) -> None:
    code = unitree_result_code(result)
    if code == ARM_ACTION_RPC_TIMEOUT_CODE:
        LOGGER.warning(
            "G1 arm Action %s (%s) returned RPC timeout 3104; the action may have "
            "started, so it will not be retried automatically",
            action.action_id,
            action.label,
        )
        return
    if code is not None and code != 0:
        raise RuntimeError(
            f"Unitree ExecuteAction({action.action_id}, {action.label}) failed "
            f"with return code {code}"
        )


def unitree_result_code(result: Any) -> Any:
    return result[0] if isinstance(result, tuple) and result else result


def extract_action_ids(payload: Any) -> set[int]:
    """Extract Action IDs from current and future GetActionList JSON shapes."""

    action_ids: set[int] = set()

    def add(value: Any) -> None:
        if isinstance(value, bool):
            return
        try:
            action_id = int(value)
        except (TypeError, ValueError):
            return
        if action_id >= 0:
            action_ids.add(action_id)

    def visit(value: Any) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if isinstance(item, (dict, list, tuple, set)):
                    visit(item)
                else:
                    add(item)
            return
        if not isinstance(value, dict):
            add(value)
            return

        recognized = False
        for key in ("id", "action_id", "actionId"):
            if key in value:
                add(value[key])
                recognized = True
        for key in ("actions", "action_list", "actionList", "data"):
            if key in value:
                visit(value[key])
                recognized = True
        if not recognized:
            for key, item in value.items():
                if isinstance(item, (dict, list, tuple, set)):
                    visit(item)
                elif isinstance(key, str) and not key.isdigit():
                    add(item)
                elif isinstance(key, str) and key.isdigit():
                    add(key)

    visit(payload)
    return action_ids


def probe_g1_connection(
    network_interface: str | None,
    *,
    network_address: str | None = None,
    timeout_seconds: float = 10.0,
    runtime: UnitreeSdkRuntime | Any | None = None,
) -> None:
    """Initialize DDS and an AudioClient without sending audio or motion."""

    selected_runtime = runtime or DEFAULT_UNITREE_RUNTIME
    if network_address is None:
        selected_runtime.create_audio_client(network_interface, timeout_seconds)
    else:
        selected_runtime.create_audio_client(
            network_interface, timeout_seconds, network_address
        )


def normalize_ipv4_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid IPv4 network address: {value!r}") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError(f"G1 DDS requires an IPv4 address, not {value!r}")
    if address.is_unspecified or address.is_loopback or address.is_link_local:
        raise ValueError(f"Unusable G1 DDS IPv4 address: {address}")
    return str(address)


def resolve_windows_interface_ipv4(
    interface_alias: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Resolve a Windows InterfaceAlias using built-in Get-NetIPAddress."""

    alias = interface_alias.strip()
    if not alias:
        raise ValueError("--network-interface cannot be empty")
    script = (
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
        "$OutputEncoding=[Console]::OutputEncoding; "
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Select-Object InterfaceAlias,IPAddress,AddressState,SkipAsSource | "
        "ConvertTo-Json -Compress"
    )
    try:
        result = runner(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        payload = json.loads(result.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not resolve Windows network interface {alias!r}: {exc}"
        ) from exc
    records = payload if isinstance(payload, list) else [payload]
    candidates: list[str] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        if str(item.get("InterfaceAlias", "")).casefold() != alias.casefold():
            continue
        try:
            address = normalize_ipv4_address(str(item.get("IPAddress", "")))
        except ValueError:
            continue
        if address not in candidates:
            candidates.append(address)
    if not candidates:
        raise RuntimeError(
            f"No usable IPv4 address was found for Windows interface {alias!r}; "
            "use --network-address to specify it explicitly"
        )
    if len(candidates) > 1:
        raise RuntimeError(
            f"Multiple IPv4 addresses were found for Windows interface {alias!r}: "
            f"{', '.join(candidates)}; use --network-address"
        )
    return candidates[0]


def _require_interface(value: str | None) -> str:
    if value is None or not value.strip():
        raise ValueError(
            "--network-interface or --network-address is required for G1 DDS access"
        )
    return value.strip()
