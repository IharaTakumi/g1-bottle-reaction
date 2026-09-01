from __future__ import annotations

import logging
import sys
from typing import Any

from .robot import RobotAdapter

LOGGER = logging.getLogger(__name__)


class G1RobotAdapter(RobotAdapter):
    """Opt-in integration boundary for a future Ubuntu G1 host.

    Unitree imports intentionally happen only in ``initialize``. Named prototype
    motions are no-ops because their official G1 API mappings have not been
    verified on hardware.
    """

    def __init__(self, network_interface: str, *, enabled: bool = False) -> None:
        if not enabled:
            raise RuntimeError(
                "Real robot control is disabled; pass --enable-real-robot explicitly"
            )
        if sys.platform == "win32":
            raise RuntimeError("G1RobotAdapter is not supported on Windows")
        if not network_interface:
            raise ValueError("A network interface is required for G1 mode")
        self.network_interface = network_interface
        self._loco_client: Any | None = None
        self._audio_client: Any | None = None

    def initialize(self) -> None:
        try:
            # Official SDK symbols; kept here so importing this module is safe
            # without unitree_sdk2py installed.
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
            from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
        except ImportError as exc:
            raise RuntimeError(
                "unitree_sdk2_python is required on the Ubuntu G1 host"
            ) from exc

        ChannelFactoryInitialize(0, self.network_interface)
        self._loco_client = LocoClient()
        self._audio_client = AudioClient()
        # Client Init/timeout/domain setup must be completed against the exact
        # SDK release and robot firmware before this adapter is enabled.

    def play_motion(self, motion: str) -> None:
        # Do not infer mappings from abstract reactions such as guard or notice
        # to physical SDK calls. WaveHand exists, but no mapping is assumed here.
        LOGGER.warning("Unverified G1 motion '%s' is a safe no-op", motion)

