from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class ObjectSignature:
    points: np.ndarray
    descriptors: np.ndarray


def describe_object(image: np.ndarray) -> ObjectSignature | None:
    if not image.size or min(image.shape[:2]) < 16:
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    scale = 320 / max(height, width)
    gray = cv2.resize(
        gray,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
    )
    detector = cv2.SIFT_create(nfeatures=256, contrastThreshold=0.025)
    keypoints, descriptors = detector.detectAndCompute(gray, None)
    if descriptors is None or len(keypoints) < 8:
        return None
    points = np.float32([point.pt for point in keypoints])
    return ObjectSignature(points, descriptors)


def same_object(reference: ObjectSignature, candidate: ObjectSignature) -> bool:
    pairs = cv2.BFMatcher(cv2.NORM_L2).knnMatch(
        reference.descriptors, candidate.descriptors, k=2
    )
    # Use each candidate keypoint at most once, and reject ambiguous matches.
    matches = {}
    for pair in pairs:
        if len(pair) != 2:
            continue
        best, second = pair
        if best.distance < 0.72 * second.distance:
            existing = matches.get(best.trainIdx)
            if existing is None or best.distance < existing.distance:
                matches[best.trainIdx] = best
    if len(matches) < 8:
        return False
    source = np.float32([reference.points[m.queryIdx] for m in matches.values()])
    target = np.float32([candidate.points[m.trainIdx] for m in matches.values()])
    transform, inliers = cv2.estimateAffine2D(
        source, target, method=cv2.RANSAC, ransacReprojThreshold=4.0
    )
    if transform is None or inliers is None:
        return False
    consistent = inliers.ravel().astype(bool)
    count = int(consistent.sum())
    if count < max(7, 0.55 * len(matches)):
        return False
    # A few aligned marks concentrated on one tiny part are insufficient.
    return bool(
        np.linalg.det(transform[:, :2]) > 0
        and cv2.contourArea(cv2.convexHull(source[consistent])) >= 400
        and cv2.contourArea(cv2.convexHull(target[consistent])) >= 400
    )


def match_objects(
    references: tuple[ObjectSignature, ...],
    candidates: list[ObjectSignature | None],
) -> set[int]:
    edges = [
        [
            index
            for index, candidate in enumerate(candidates)
            if candidate is not None and same_object(reference, candidate)
        ]
        for reference in references
    ]
    assignments: dict[int, int] = {}

    def assign(reference_index: int, seen: set[int]) -> bool:
        for candidate_index in edges[reference_index]:
            if candidate_index in seen:
                continue
            seen.add(candidate_index)
            previous = assignments.get(candidate_index)
            if previous is None or assign(previous, seen):
                assignments[candidate_index] = reference_index
                return True
        return False

    for index in range(len(references)):
        assign(index, set())
    return set(assignments)
