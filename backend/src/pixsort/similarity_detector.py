"""Pluggable local visual similarity embeddings and robust geometric matching.

Uses a two-stage approach:
1. Fast first-stage candidate generation using lightweight thumbnail embeddings
   and aspect-ratio sanity checks.
2. High-precision second-stage verification using scale-normalized SIFT keypoints
   and RANSAC homography estimation.
3. Cohesive clustering that prevents transitive chaining / drift from grouping
   unrelated photos together.
"""

from collections import defaultdict
from itertools import combinations
from typing import Iterable, Protocol

import cv2
import numpy as np
from PIL import Image, ImageOps

from .config import AnalysisConfig
from .models import ImageRecord


class ImageEmbedder(Protocol):
    def embed(self, path: str) -> np.ndarray: ...


class ThumbnailEmbedder:
    """Lightweight 32x32 color-spatial feature embedder with in-memory caching."""

    def __init__(self) -> None:
        self._cache: dict[str, np.ndarray] = {}

    def embed(self, path: str) -> np.ndarray:
        if path in self._cache:
            return self._cache[path]
        try:
            with Image.open(path) as image:
                oriented = ImageOps.exif_transpose(image).convert("RGB")
                resized = oriented.resize((32, 32), Image.Resampling.BILINEAR)
                values = np.asarray(resized, dtype=np.float32) / 255.0
            features = np.concatenate([values.mean(axis=(0, 1)), values.reshape(-1)])
            norm = np.linalg.norm(features)
            emb = features / norm if norm else features
        except (OSError, ValueError):
            emb = np.zeros(32 * 32 * 3 + 3, dtype=np.float32)
        self._cache[path] = emb
        return emb


class LocalFeatureMatcher:
    """SIFT feature matcher with scale normalization, caching, and RANSAC verification."""

    def __init__(self, min_inliers: int, max_dimension: int = 800) -> None:
        self.min_inliers = min_inliers
        self.max_dimension = max_dimension
        self._sift = cv2.SIFT_create()
        self._cache: dict[str, tuple[list[cv2.KeyPoint], np.ndarray | None]] = {}

    def describe(self, path: str) -> tuple[list[cv2.KeyPoint], np.ndarray | None]:
        if path in self._cache:
            return self._cache[path]
        try:
            with Image.open(path) as image:
                oriented = ImageOps.exif_transpose(image).convert("L")
                width, height = oriented.size
                scale = min(1.0, self.max_dimension / max(width, height))
                if scale < 1.0:
                    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                    oriented = oriented.resize(new_size, Image.Resampling.BILINEAR)
                gray = np.asarray(oriented, dtype=np.uint8)
            kp, des = self._sift.detectAndCompute(gray, None)
            result = (kp or [], des)
        except (OSError, ValueError):
            result = ([], None)
        self._cache[path] = result
        return result

    def match_details(
        self,
        left: tuple[list[cv2.KeyPoint], np.ndarray | None],
        right: tuple[list[cv2.KeyPoint], np.ndarray | None],
    ) -> tuple[bool, int, float]:
        """Returns (is_match, inlier_count, inlier_ratio)."""
        left_keypoints, left_descriptors = left
        right_keypoints, right_descriptors = right
        if left_descriptors is None or right_descriptors is None:
            return False, 0, 0.0
        if len(left_keypoints) < 4 or len(right_keypoints) < 4:
            return False, 0, 0.0

        try:
            matches = cv2.BFMatcher().knnMatch(left_descriptors, right_descriptors, k=2)
        except cv2.error:
            return False, 0, 0.0

        good = [
            first
            for pair in matches
            if len(pair) == 2
            for first, second in [pair]
            if first.distance < 0.75 * second.distance
        ]
        if len(good) < 4:
            return False, 0, 0.0

        source = np.float32([left_keypoints[item.queryIdx].pt for item in good]).reshape(
            -1, 1, 2
        )
        target = np.float32([right_keypoints[item.trainIdx].pt for item in good]).reshape(
            -1, 1, 2
        )
        _, mask = cv2.findHomography(source, target, cv2.RANSAC, 5.0)
        if mask is None:
            return False, 0, 0.0
        inliers = int(mask.sum())
        inlier_ratio = inliers / max(1, len(good))
        is_matched = inliers >= self.min_inliers and inlier_ratio >= 0.18
        return is_matched, inliers, inlier_ratio

    def is_match(
        self,
        left: tuple[list[cv2.KeyPoint], np.ndarray | None],
        right: tuple[list[cv2.KeyPoint], np.ndarray | None],
    ) -> bool:
        matched, _, _ = self.match_details(left, right)
        return matched


def find_visual_groups(
    records: Iterable[ImageRecord],
    config: AnalysisConfig,
    embedder: ImageEmbedder | None = None,
) -> list[list[str]]:
    """Group visually similar images with candidate gating and cohesive clustering."""
    embedder = embedder or ThumbnailEmbedder()
    valid = [record for record in records if record.error is None]
    if len(valid) < 2:
        return []

    record_by_path = {record.path: record for record in valid}
    embeddings = {record.path: embedder.embed(record.path) for record in valid}

    # Step 1: Fast candidate filtering
    candidate_pairs: list[tuple[str, str, float]] = []
    aspect_ratios: dict[str, float] = {}
    for record in valid:
        if record.width and record.height:
            aspect_ratios[record.path] = record.width / max(1, record.height)
        else:
            aspect_ratios[record.path] = 1.0

    for left, right in combinations([r.path for r in valid], 2):
        score = float(np.dot(embeddings[left], embeddings[right]))
        if score < config.visual_similarity_threshold:
            continue

        # Check aspect ratio compatibility unless score is extremely high (e.g. crop)
        ar_left = aspect_ratios.get(left, 1.0)
        ar_right = aspect_ratios.get(right, 1.0)
        ar_ratio = ar_left / ar_right if ar_right > 0 else 1.0
        if (ar_ratio < 0.65 or ar_ratio > 1.55) and score < 0.98:
            continue

        candidate_pairs.append((left, right, score))

    if not candidate_pairs:
        return []

    # Step 2: Lazy SIFT evaluation only for images involved in candidate pairs
    local_matcher = LocalFeatureMatcher(config.visual_min_inliers)
    active_paths = {p for left, right, _ in candidate_pairs for p in (left, right)}
    local_features = {path: local_matcher.describe(path) for path in active_paths}

    # Step 3: Verified pairwise adjacency
    adj: dict[str, set[str]] = defaultdict(set)
    for left, right, _ in candidate_pairs:
        if local_matcher.is_match(local_features[left], local_features[right]):
            adj[left].add(right)
            adj[right].add(left)

    if not adj:
        return []

    # Step 4: Cohesive component clustering (prevents transitive drift)
    visited: set[str] = set()
    groups: list[list[str]] = []

    for node in sorted(adj):
        if node in visited:
            continue
        # Find connected component
        component: list[str] = []
        stack = [node]
        visited.add(node)
        while stack:
            curr = stack.pop()
            component.append(curr)
            for neighbor in adj[curr]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)

        if len(component) < 2:
            continue

        if len(component) == 2:
            groups.append(sorted(component))
            continue

        # For larger components, verify edge density to ensure mutual cohesion
        # rather than arbitrary transitive chaining.
        comp_set = set(component)
        edges = sum(len(adj[item] & comp_set) for item in component) // 2
        max_possible_edges = len(component) * (len(component) - 1) // 2
        density = edges / max_possible_edges if max_possible_edges > 0 else 0.0

        if density >= 0.5:
            groups.append(sorted(component))
        else:
            # If density is low, find maximal cohesive cliques or sub-clusters
            sub_visited: set[str] = set()
            for seed in sorted(component):
                if seed in sub_visited:
                    continue
                # Cluster of seed and its neighbors that are well-connected
                neighbors = sorted(adj[seed] & comp_set)
                cluster = [seed]
                for n in neighbors:
                    # Check if n connects to majority of current cluster
                    if sum(n in adj[c] for c in cluster) >= max(1, len(cluster) // 2):
                        cluster.append(n)
                if len(cluster) >= 2:
                    groups.append(sorted(cluster))
                    sub_visited.update(cluster)

    return sorted(sorted(p) for p in groups if len(p) > 1)
