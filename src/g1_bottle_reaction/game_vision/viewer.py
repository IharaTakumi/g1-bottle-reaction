from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True, slots=True)
class HudState:
    fps: float
    clear_m: float
    max_m: float
    fog_mode: str
    fov_scale: float
    preset: str
    depth_active: bool
    fail_closed: bool
    prefiltered: bool = False
    fov_enabled: bool = True
    show_fps: bool = True


def compose_view(
    game_bgr: np.ndarray,
    safety_bgr: np.ndarray,
    *,
    mode: str,
    safety_inset_scale: float,
) -> np.ndarray:
    """Compose game/safety pixels without opening a GUI window."""

    if mode not in {"game", "safety", "both"}:
        raise ValueError("display mode must be game, safety, or both")
    _validate_pair(game_bgr, safety_bgr)
    if mode == "game":
        return game_bgr.copy()
    if mode == "safety":
        return safety_bgr.copy()
    if not 0.1 <= safety_inset_scale <= 0.5:
        raise ValueError("safety inset scale must be between 0.1 and 0.5")

    import cv2

    canvas = game_bgr.copy()
    height, width = canvas.shape[:2]
    inset_width = max(1, int(round(width * safety_inset_scale)))
    inset_height = max(1, int(round(inset_width * height / width)))
    inset = cv2.resize(safety_bgr, (inset_width, inset_height), interpolation=cv2.INTER_AREA)
    border = max(2, width // 320)
    margin = max(border + 2, width // 80)
    x0 = width - inset_width - margin - border
    y0 = margin
    x1 = x0 + inset_width + border * 2
    y1 = y0 + inset_height + border * 2
    canvas[y0:y1, x0:x1] = (0, 0, 220)
    canvas[
        y0 + border : y0 + border + inset_height,
        x0 + border : x0 + border + inset_width,
    ] = inset
    return canvas


def draw_hud(frame: np.ndarray, state: HudState, *, mode: str) -> np.ndarray:
    """Draw a compact post-filter HUD and conspicuous safety/depth labels."""

    import cv2

    output = frame.copy()
    height, width = output.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(0.42, min(width, height) / 900.0)
    thickness = max(1, int(round(scale * 2)))
    pad = max(8, width // 80)
    line_height = max(19, int(round(28 * scale)))
    if state.prefiltered and mode in {"game", "both"}:
        # The sender burns the authoritative range/FOV HUD into the display
        # image after filtering. Keep receiver/link status away from that
        # bottom bar so the values are neither obscured nor duplicated.
        prefix = f"LINK FPS {state.fps:4.1f}    " if state.show_fps else ""
        labels = [(prefix + "REMOTE FILTERED G1 STREAM", (235, 235, 235))]
        if mode == "both":
            labels.append(("RED INSET: UNFILTERED SAFETY", (30, 30, 255)))
        for index, (text, color) in enumerate(labels):
            (text_width, text_height), _ = cv2.getTextSize(
                text, font, scale, thickness
            )
            y0 = pad + index * (line_height + 4)
            cv2.rectangle(
                output,
                (pad - 3, y0 - 3),
                (pad + text_width + 6, y0 + text_height + 8),
                (0, 0, 0),
                -1,
            )
            cv2.putText(
                output,
                text,
                (pad, y0 + text_height + 2),
                font,
                scale,
                color,
                thickness,
                cv2.LINE_AA,
            )
        return output

    lines: list[tuple[str, tuple[int, int, int]]] = []
    prefix = f"FPS {state.fps:4.1f}    " if state.show_fps else ""
    if mode == "safety":
        lines.append((prefix + "UNFILTERED SAFETY VIEW", (30, 30, 255)))
    elif state.prefiltered:
        lines.append((prefix + "REMOTE FILTERED G1 STREAM", (235, 235, 235)))
    elif state.depth_active:
        lines.append(
            (
                prefix
                + f"RANGE {state.clear_m:.2f}-{state.max_m:.2f} m  {state.fog_mode.upper()}",
                (235, 235, 235),
            )
        )
    elif state.fail_closed:
        lines.append((prefix + "DEPTH UNAVAILABLE - GAME VIEW CLOSED", (30, 30, 255)))
    else:
        lines.append((prefix + "NO DEPTH - DISTANCE FOG OFF", (0, 210, 255)))
    if mode == "safety" and state.prefiltered:
        second_line = "GAME FILTER SETTINGS CONTROLLED ON PC2"
    elif mode == "safety":
        fov = f"FOV {state.fov_scale * 100:.0f}%" if state.fov_enabled else "FOV OFF"
        second_line = (
            f"GAME-ONLY SETTINGS: RANGE {state.clear_m:.2f}-{state.max_m:.2f} m  {fov}"
        )
    elif state.prefiltered:
        second_line = "FILTER SETTINGS CONTROLLED ON PC2"
    elif state.fov_enabled:
        second_line = f"FOV {state.fov_scale * 100:.0f}%  {state.preset.upper()}"
    else:
        second_line = f"FOV OFF  {state.preset.upper()}"
    lines.append((second_line, (235, 235, 235)))

    bar_height = pad * 2 + line_height * len(lines)
    overlay = output.copy()
    cv2.rectangle(overlay, (0, height - bar_height), (width, height), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.65, output, 0.35, 0, output)
    for index, (text, color) in enumerate(lines):
        y = height - bar_height + pad + line_height * (index + 1) - 4
        cv2.putText(output, text, (pad, y), font, scale, color, thickness, cv2.LINE_AA)

    if mode in {"safety", "both"}:
        label = (
            "SAFETY VIEW - UNFILTERED"
            if mode == "safety"
            else "SAFETY INSET - UNFILTERED"
        )
        (text_width, text_height), _ = cv2.getTextSize(label, font, scale, thickness)
        cv2.rectangle(
            output,
            (pad - 3, pad - 3),
            (pad + text_width + 6, pad + text_height + 8),
            (0, 0, 180),
            -1,
        )
        cv2.putText(
            output,
            label,
            (pad, pad + text_height + 2),
            font,
            scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )
    return output


class OpenCVViewer:
    """Thin OpenCV window wrapper kept out of the processing pipeline."""

    def __init__(self, *, fullscreen: bool, window_name: str = "G1 GAME VISION") -> None:
        self.fullscreen = fullscreen
        self.window_name = window_name
        self._cv2: Any | None = None
        self._open = False

    def open(self) -> None:
        if self._open:
            return
        import cv2

        self._cv2 = cv2
        cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
        self._open = True
        self._apply_fullscreen()

    def show(self, frame: np.ndarray) -> int:
        if not self._open or self._cv2 is None:
            raise RuntimeError("viewer is not open")
        self._cv2.imshow(self.window_name, frame)
        return self._cv2.waitKey(1) & 0xFF

    def toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        if self._open:
            self._apply_fullscreen()

    def close(self) -> None:
        if self._open and self._cv2 is not None:
            self._cv2.destroyWindow(self.window_name)
        self._open = False

    def _apply_fullscreen(self) -> None:
        assert self._cv2 is not None
        value = (
            self._cv2.WINDOW_FULLSCREEN
            if self.fullscreen
            else self._cv2.WINDOW_NORMAL
        )
        self._cv2.setWindowProperty(
            self.window_name, self._cv2.WND_PROP_FULLSCREEN, value
        )


def _validate_pair(game_bgr: np.ndarray, safety_bgr: np.ndarray) -> None:
    for name, frame in (("game", game_bgr), ("safety", safety_bgr)):
        if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError(f"{name} frame must be HxWx3 uint8 BGR")
    if game_bgr.shape != safety_bgr.shape:
        raise ValueError("game and safety frames must have matching dimensions")
