from __future__ import annotations

import argparse
import math
import shutil
import tempfile
from pathlib import Path

from ultralytics import YOLO
from ultralytics.data.utils import check_det_dataset


ROOT = Path(__file__).resolve().parent
DATASET_ROOT = ROOT / "datasets"
CLASS_NAMES = (
    "relay_module",
    "triple_syringe",
    "ultrasonic_hc-sr04",
)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
SPLIT_NAMES = {"train": "train", "val": "val", "test": "test"}


def parse_label_file(label_path: Path, class_count: int) -> tuple[list[str], int]:
    normalized_rows = []
    polygon_count = 0
    for line_number, line in enumerate(
        label_path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 5 and (len(fields) < 7 or len(fields) % 2 == 0):
            raise ValueError(
                f"{label_path}:{line_number}: expected a YOLO box (5 fields) or "
                f"polygon (class id plus at least 3 coordinate pairs), "
                f"got {len(fields)} fields"
            )
        try:
            values = [float(field) for field in fields]
        except ValueError as error:
            raise ValueError(
                f"{label_path}:{line_number}: label values must be numeric"
            ) from error
        if not all(math.isfinite(value) for value in values):
            raise ValueError(
                f"{label_path}:{line_number}: label values must be finite"
            )

        source_class = values[0]
        if not source_class.is_integer() or not 0 <= source_class < class_count:
            raise ValueError(
                f"{label_path}:{line_number}: class id must be an integer "
                f"between 0 and {class_count - 1}"
            )

        if len(fields) == 5:
            center_x, center_y, width, height = values[1:]
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
        else:
            coordinates = values[1:]
            if any(not 0 <= coordinate <= 1 for coordinate in coordinates):
                raise ValueError(
                    f"{label_path}:{line_number}: polygon coordinates are out of range"
                )
            x_coordinates = coordinates[0::2]
            y_coordinates = coordinates[1::2]
            if len(set(zip(x_coordinates, y_coordinates))) < 3:
                raise ValueError(
                    f"{label_path}:{line_number}: polygon needs at least "
                    "three distinct points"
                )
            polygon_area = abs(
                sum(
                    x_coordinates[index] * y_coordinates[(index + 1) % len(x_coordinates)]
                    - x_coordinates[(index + 1) % len(x_coordinates)]
                    * y_coordinates[index]
                    for index in range(len(x_coordinates))
                )
            ) / 2
            if polygon_area <= 1e-8:
                raise ValueError(
                    f"{label_path}:{line_number}: polygon has no area"
                )
            left, right = min(x_coordinates), max(x_coordinates)
            top, bottom = min(y_coordinates), max(y_coordinates)
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0:
                raise ValueError(
                    f"{label_path}:{line_number}: polygon has no area"
                )
            center_x = (left + right) / 2
            center_y = (top + bottom) / 2
            polygon_count += 1

        normalized_rows.append(
            f"{int(source_class)} {center_x:.6f} {center_y:.6f} "
            f"{width:.6f} {height:.6f}"
        )

    return normalized_rows, polygon_count


def _image_directories(split_value: str | list[str]) -> list[Path]:
    values = split_value if isinstance(split_value, list) else [split_value]
    return [Path(value) for value in values]


def _without_suffix(path: Path) -> Path:
    if not path.suffix:
        return path
    return path.parent / path.name[: -len(path.suffix)]


def prepare_dataset(source: Path, destination: Path) -> dict[str, int]:
    data_yaml = source / "data.yaml" if source.is_dir() else source
    if not data_yaml.is_file():
        raise FileNotFoundError(f"Dataset YAML not found: {data_yaml}")

    dataset = check_det_dataset(str(data_yaml.resolve()))
    source_names = dataset["names"]
    if isinstance(source_names, dict):
        source_names = [source_names[index] for index in range(len(source_names))]
    if tuple(source_names) != CLASS_NAMES:
        raise ValueError(
            "Dataset class order does not match the application model mapping. "
            f"Expected {CLASS_NAMES}, got {tuple(source_names)}"
        )

    if not dataset.get("train") or not dataset.get("val"):
        raise ValueError("Dataset YAML must define both train and val image splits")

    destination.mkdir(parents=True, exist_ok=False)
    counts = {"train": 0, "val": 0, "test": 0, "polygons": 0}
    classes_by_split: dict[str, set[int]] = {}
    for source_split, target_split in SPLIT_NAMES.items():
        split_value = dataset.get(source_split)
        if not split_value:
            if source_split == "test":
                continue
            raise ValueError(f"Dataset YAML is missing the {source_split} split")

        for directory_index, image_dir in enumerate(
            _image_directories(split_value)
        ):
            if not image_dir.is_dir():
                raise FileNotFoundError(
                    f"{source_split} image directory not found: {image_dir}"
                )
            label_dir = image_dir.parent / "labels"
            if not label_dir.is_dir():
                raise FileNotFoundError(
                    f"{source_split} label directory not found: {label_dir}"
                )

            images = sorted(
                path
                for path in image_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            )
            image_stems = {
                _without_suffix(path.relative_to(image_dir)) for path in images
            }
            labels = {
                _without_suffix(path.relative_to(label_dir)): path
                for path in label_dir.rglob("*.txt")
            }
            missing_labels = sorted(image_stems - labels.keys())
            orphan_labels = sorted(labels.keys() - image_stems)
            if missing_labels or orphan_labels:
                raise ValueError(
                    f"{source_split}: missing labels={missing_labels[:10]}, "
                    f"labels without images={orphan_labels[:10]}"
                )
            if not images:
                raise ValueError(f"{source_split}: no supported images found")

            split_classes = classes_by_split.setdefault(target_split, set())
            for image_path in images:
                relative_image = image_path.relative_to(image_dir)
                relative_stem = _without_suffix(relative_image)
                relative_label = relative_stem
                label_path = labels[relative_label]
                rows, polygons = parse_label_file(label_path, len(CLASS_NAMES))
                counts["polygons"] += polygons
                split_classes.update(int(row.split()[0]) for row in rows)

                if directory_index:
                    relative_stem = relative_stem.parent / (
                        f"{directory_index}_{relative_stem.name}"
                    )
                target_image = (
                    destination
                    / "images"
                    / target_split
                    / relative_stem.parent
                    / f"{relative_stem.name}{image_path.suffix.lower()}"
                )
                target_label = (
                    destination
                    / "labels"
                    / target_split
                    / relative_stem.parent
                    / f"{relative_stem.name}.txt"
                )
                target_image.parent.mkdir(parents=True, exist_ok=True)
                target_label.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(image_path, target_image)
                target_label.write_text(
                    "\n".join(rows) + ("\n" if rows else ""),
                    encoding="utf-8",
                )
                counts[target_split] += 1

    for split in ("train", "val", "test"):
        if not counts[split]:
            continue
        missing_classes = sorted(set(range(len(CLASS_NAMES))) - classes_by_split[split])
        if missing_classes:
            missing_names = [CLASS_NAMES[class_id] for class_id in missing_classes]
            print(f"Warning: {split} split has no annotations for {missing_names}.")

    config_lines = [
        f'path: "{destination.as_posix()}"',
        "train: images/train",
        "val: images/val",
    ]
    if counts["test"]:
        config_lines.append("test: images/test")
    config_lines.append("names:")
    config_lines.extend(
        f"  {class_id}: {name}" for class_id, name in enumerate(CLASS_NAMES)
    )
    (destination / "data.yaml").write_text(
        "\n".join(config_lines) + "\n", encoding="utf-8"
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a YOLO dataset, convert polygon labels to detection boxes, "
            "train, and evaluate the held-out test split."
        )
    )
    parser.add_argument("--data", type=Path, default=DATASET_ROOT)
    parser.add_argument("--model", default="yolo11n.pt")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--imgsz", type=int, default=416)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--device", default=None)
    parser.add_argument("--name", default="iot-components-v3")
    args = parser.parse_args()
    if args.epochs < 1 or args.imgsz < 1 or args.batch < 1:
        parser.error("--epochs, --imgsz, and --batch must be positive")

    project = ROOT / "runs" / "object-detection"
    with tempfile.TemporaryDirectory(prefix="iot-yolo-") as temporary_directory:
        prepared_data = Path(temporary_directory) / "dataset"
        counts = prepare_dataset(args.data.resolve(), prepared_data)
        print(
            f"Prepared {counts['train']} train, {counts['val']} validation, "
            f"and {counts['test']} test images; converted "
            f"{counts['polygons']} polygon annotations to boxes."
        )
        print("Classes: " + ", ".join(CLASS_NAMES))
        if counts["val"] < len(CLASS_NAMES):
            print(
                "Warning: validation has fewer images than classes; "
                "metrics may not represent every class."
            )
        if counts["test"] and counts["test"] < len(CLASS_NAMES):
            print(
                "Warning: test has fewer images than classes; "
                "test metrics will be limited."
            )

        data_path = str(prepared_data / "data.yaml")
        model = YOLO(args.model)
        model.train(
            data=data_path,
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

        best_weights = project / args.name / "weights" / "best.pt"
        if counts["test"]:
            if not best_weights.is_file():
                raise FileNotFoundError(
                    f"Training did not produce best weights: {best_weights}"
                )
            print("Evaluating best checkpoint on the held-out test split.")
            YOLO(str(best_weights)).val(data=data_path, split="test")

    print(f"Training finished. Best weights: {best_weights}")


if __name__ == "__main__":
    main()
