from __future__ import annotations


def proximity_ratio(
    bounding_box: tuple[float, float, float, float],
    frame_width: int,
    frame_height: int,
) -> float:
    x1, y1, x2, y2 = bounding_box
    box_width = max(0.0, x2 - x1)
    box_height = max(0.0, y2 - y1)
    frame_area = frame_width * frame_height
    if frame_area <= 0:
        return 0.0
    return min(1.0, box_width * box_height / frame_area)

