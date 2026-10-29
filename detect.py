from __future__ import annotations

import argparse
from pathlib import Path

import cv2
from ultralytics import YOLO


ROOT = Path(__file__).resolve().parent
DEFAULT_WEIGHTS = (
    ROOT / "runs" / "object-detection" / "iot-components" / "weights" / "best.pt"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect objects and show per-class counts from a webcam or video."
    )
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument(
        "--source",
        default="0",
        help="Webcam index (default: 0), video path, or image path",
    )
    parser.add_argument("--conf", type=float, default=0.5)
    parser.add_argument("--iou", type=float, default=0.25)
    args = parser.parse_args()
    if not 0 <= args.conf <= 1:
        parser.error("--conf must be between 0 and 1")
    if not 0 <= args.iou <= 1:
        parser.error("--iou must be between 0 and 1")
    if not args.weights.is_file():
        raise FileNotFoundError(
            f"Model weights not found: {args.weights}. Run train.py first."
        )

    source: int | str = int(args.source) if args.source.isdecimal() else args.source
    model = YOLO(str(args.weights))
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"Could not open camera or video source: {args.source}")

    try:
        while True:
            success, frame = capture.read()
            if not success:
                if isinstance(source, int):
                    raise RuntimeError("Camera did not return a frame")
                break

            result = model.predict(
                frame, conf=args.conf, iou=args.iou, verbose=False
            )[0]
            annotated = result.plot()
            counts: dict[int, int] = {}
            if result.boxes is not None:
                for class_id in result.boxes.cls.int().tolist():
                    counts[class_id] = counts.get(class_id, 0) + 1

            names = result.names
            lines = [
                f"{names[class_id]}: {count}"
                for class_id, count in sorted(counts.items())
            ]
            if not lines:
                lines = ["No objects detected"]
            for line_number, text in enumerate(lines):
                cv2.putText(
                    annotated,
                    text,
                    (12, 30 + line_number * 28),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.75,
                    (0, 255, 0),
                    2,
                    cv2.LINE_AA,
                )

            cv2.imshow("IoT object detection - press q to quit", annotated)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        capture.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
