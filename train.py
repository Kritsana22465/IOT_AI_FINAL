from __future__ import annotations

import argparse
import re
import shutil
import tempfile
from pathlib import Path

from ultralytics import YOLO


ROOT = Path(__file__).resolve().parent
DATASET_ROOT = ROOT / "datasetsIOT" / "datasets"
CLASSES = (
    ("bananapi", "Banana Pi"),
    ("relay_module", "Relay module"),
    ("ultrasonic_hc-sr04", "Ultrasonic HC-SR04"),
)
AUGMENTATION_SUFFIX = re.compile(r"_aug_\d+$")


def parse_label_file(label_path: Path) -> list[str]:
    normalized_rows = []
    for line_number, line in enumerate(
        label_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 5:
            raise ValueError(
                f"{label_path}:{line_number}: expected 5 YOLO fields, got {len(fields)}"
            )
        try:
            source_class = float(fields[0])
            center_x, center_y, width, height = map(float, fields[1:])
        except ValueError as error:
            raise ValueError(
                f"{label_path}:{line_number}: label values must be numeric"
            ) from error
        if not source_class.is_integer() or source_class < 0:
            raise ValueError(
                f"{label_path}:{line_number}: class id must be a non-negative integer"
            )
        if not (
            0 <= center_x <= 1
            and 0 <= center_y <= 1
            and 0 < width <= 1
            and 0 < height <= 1
        ):
            raise ValueError(
                f"{label_path}:{line_number}: normalized box values are out of range"
            )
        tolerance = 1e-5
        if (
            center_x - width / 2 < -tolerance
            or center_y - height / 2 < -tolerance
            or center_x + width / 2 > 1 + tolerance
            or center_y + height / 2 > 1 + tolerance
        ):
            raise ValueError(
                f"{label_path}:{line_number}: bounding box extends outside the image"
            )
        width = min(width, 2 * center_x, 2 * (1 - center_x))
        height = min(height, 2 * center_y, 2 * (1 - center_y))
        normalized_rows.append(
            f"{{class_id}} {center_x:.6f} {center_y:.6f} {width:.6f} {height:.6f}"
        )

    if not normalized_rows:
        raise ValueError(f"{label_path}: label file has no object annotations")
    return normalized_rows


def prepare_dataset(source_root: Path, destination: Path) -> dict[str, int]:
    if not source_root.is_dir():
        raise FileNotFoundError(f"Dataset directory not found: {source_root}")

    destination.mkdir(parents=True, exist_ok=False)
    counts = {"train": 0, "val": 0}
    class_names = [name for _, name in CLASSES]

    for class_id, (folder_name, _) in enumerate(CLASSES):
        class_root = source_root / folder_name
        image_dir = class_root / "images"
        label_dir = class_root / "labels"
        if not image_dir.is_dir() or not label_dir.is_dir():
            raise FileNotFoundError(
                f"Expected images and labels folders under {class_root}"
            )

        images = sorted(image_dir.glob("*.jpg"))
        labels = {path.stem: path for path in label_dir.glob("*.txt")}
        image_stems = {path.stem for path in images}
        missing_labels = sorted(image_stems - labels.keys())
        orphan_labels = sorted(labels.keys() - image_stems)
        if missing_labels or orphan_labels:
            raise ValueError(
                f"{folder_name}: missing labels={missing_labels}, "
                f"labels without images={orphan_labels}"
            )
        if not images:
            raise ValueError(f"{folder_name}: no .jpg images found")

        originals = sorted(
            stem for stem in image_stems if not AUGMENTATION_SUFFIX.search(stem)
        )
        if len(originals) < 2:
            raise ValueError(
                f"{folder_name}: at least two original image groups are required"
            )
        validation_group_count = max(1, round(len(originals) * 0.2))
        validation_groups = set(originals[-validation_group_count:])

        for image_path in images:
            group = AUGMENTATION_SUFFIX.sub("", image_path.stem)
            if group in validation_groups and image_path.stem != group:
                continue
            label_path = labels[image_path.stem]
            rows = parse_label_file(label_path)
            split = (
                "val" if group in validation_groups else "train"
            )
            target_stem = f"{folder_name}_{image_path.stem}"
            target_image = destination / "images" / split / f"{target_stem}.jpg"
            target_label = destination / "labels" / split / f"{target_stem}.txt"
            target_image.parent.mkdir(parents=True, exist_ok=True)
            target_label.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(image_path, target_image)
            target_label.write_text(
                "\n".join(row.format(class_id=class_id) for row in rows) + "\n",
                encoding="utf-8",
            )
            counts[split] += 1

    config_lines = [
        f'path: "{destination.as_posix()}"',
        "train: images/train",
        "val: images/val",
        "names:",
    ]
    config_lines.extend(
        f"  {class_id}: {name}" for class_id, name in enumerate(class_names)
    )
    (destination / "data.yaml").write_text(
        "\n".join(config_lines) + "\n", encoding="utf-8"
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate labels, prepare a leakage-safe dataset, and train YOLO."
    )
    parser.add_argument("--data", type=Path, default=DATASET_ROOT)
    parser.add_argument("--model", default="yolo11n.pt")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--imgsz", type=int, default=416)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--device", default=None)
    parser.add_argument("--name", default="iot-components")
    args = parser.parse_args()
    if args.epochs < 1 or args.imgsz < 1 or args.batch < 1:
        parser.error("--epochs, --imgsz, and --batch must be positive")

    project = ROOT / "runs" / "object-detection"
    with tempfile.TemporaryDirectory(prefix="iot-yolo-") as temporary_directory:
        prepared_data = Path(temporary_directory) / "dataset"
        counts = prepare_dataset(args.data.resolve(), prepared_data)
        print(
            f"Label check passed. Prepared {counts['train']} training images "
            f"and {counts['val']} validation originals."
        )
        print("Classes: " + ", ".join(name for _, name in CLASSES))

        model = YOLO(args.model)
        model.train(
            data=str(prepared_data / "data.yaml"),
            epochs=args.epochs,
            imgsz=args.imgsz,
            batch=args.batch,
            workers=0,
            device=args.device,
            patience=10,
            seed=42,
            project=str(project),
            name=args.name,
            exist_ok=True,
        )
    print(f"Training finished. Best weights: {project / args.name / 'weights' / 'best.pt'}")


if __name__ == "__main__":
    main()
