from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .proximity import proximity_ratio


@dataclass(frozen=True, slots=True)
class BottleDetection:
    bounding_box: tuple[int, int, int, int]
    confidence: float
    proximity_ratio: float


class YoloBottleDetector:
    """Lazy Ultralytics detector; constructing core components never loads YOLO."""

    def __init__(self, model: str, confidence_threshold: float) -> None:
        from ultralytics import YOLO

        self._model: Any = YOLO(model)
        self.confidence_threshold = confidence_threshold

    def detect(self, frame: Any) -> BottleDetection | None:
        height, width = frame.shape[:2]
        results = self._model.predict(
            frame, conf=self.confidence_threshold, verbose=False
        )
        candidates: list[BottleDetection] = []
        for result in results:
            names = result.names
            for box in result.boxes:
                class_id = int(box.cls.item())
                if names[class_id] != "bottle":
                    continue
                coords = tuple(int(value) for value in box.xyxy[0].tolist())
                ratio = proximity_ratio(coords, width, height)
                candidates.append(
                    BottleDetection(
                        bounding_box=coords,
                        confidence=float(box.conf.item()),
                        proximity_ratio=ratio,
                    )
                )
        return max(candidates, key=lambda item: item.proximity_ratio, default=None)

