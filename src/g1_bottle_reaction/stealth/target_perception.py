from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .models import TargetObservation, TargetRole


@dataclass(frozen=True, slots=True)
class RawDetection:
    detector_label: str
    confidence: float
    bbox: tuple[int, int, int, int]


class YoloTargetDetector:
    """YOLO boundary that returns raw detections for one configurable class."""

    def __init__(self, model: str, detector_label: str, confidence_threshold: float) -> None:
        from ultralytics import YOLO

        self._model: Any = YOLO(model)
        self.detector_label = detector_label
        self.confidence_threshold = confidence_threshold

    def detect_all(self, frame: Any) -> tuple[RawDetection, ...]:
        results = self._model.predict(
            frame, conf=self.confidence_threshold, verbose=False
        )
        detections: list[RawDetection] = []
        for result in results:
            for box in result.boxes:
                class_id = int(box.cls.item())
                label = str(result.names[class_id])
                if label != self.detector_label:
                    continue
                detections.append(
                    RawDetection(
                        detector_label=label,
                        confidence=float(box.conf.item()),
                        bbox=tuple(int(value) for value in box.xyxy[0].tolist()),
                    )
                )
        return tuple(detections)


class TargetPerception:
    """Maps configurable raw detector labels to the semantic PLAYER role."""

    def __init__(
        self,
        detector: YoloTargetDetector,
        *,
        detector_label: str,
        semantic_role: str,
    ) -> None:
        self.detector = detector
        self.detector_label = detector_label
        self.semantic_role = TargetRole(semantic_role)

    def observe(self, frame: Any, *, timestamp: float) -> TargetObservation:
        height, width = frame.shape[:2]
        return self.map_detections(
            self.detector.detect_all(frame),
            frame_width=width,
            frame_height=height,
            timestamp=timestamp,
        )

    def map_detections(
        self,
        detections: Iterable[RawDetection],
        *,
        frame_width: int,
        frame_height: int,
        timestamp: float,
    ) -> TargetObservation:
        candidates = tuple(
            detection
            for detection in detections
            if detection.detector_label == self.detector_label
        )
        if not candidates:
            return TargetObservation.missing(
                timestamp=timestamp,
                raw_detector_label=self.detector_label,
                semantic_role=self.semantic_role,
            )
        selected = max(
            candidates,
            key=lambda item: _selection_score(
                item, frame_width=frame_width, frame_height=frame_height
            ),
        )
        x1, y1, x2, y2 = selected.bbox
        center_x = ((x1 + x2) / frame_width) - 1.0
        center_y = ((y1 + y2) / frame_height) - 1.0
        area = max(0, x2 - x1) * max(0, y2 - y1)
        area_ratio = area / float(frame_width * frame_height)
        return TargetObservation(
            visible=True,
            confidence=selected.confidence,
            bbox=selected.bbox,
            center_x_normalized=_clamp(center_x, -1.0, 1.0),
            center_y_normalized=_clamp(center_y, -1.0, 1.0),
            bbox_area_ratio=_clamp(area_ratio, 0.0, 1.0),
            timestamp=timestamp,
            raw_detector_label=selected.detector_label,
            semantic_role=self.semantic_role,
        )


def _selection_score(
    detection: RawDetection, *, frame_width: int, frame_height: int
) -> float:
    x1, y1, x2, y2 = detection.bbox
    area_ratio = (
        max(0, x2 - x1) * max(0, y2 - y1) / float(frame_width * frame_height)
    )
    center_x = ((x1 + x2) / frame_width) - 1.0
    centrality = 1.0 - min(1.0, abs(center_x))
    return 0.50 * area_ratio + 0.35 * detection.confidence + 0.15 * centrality


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))

