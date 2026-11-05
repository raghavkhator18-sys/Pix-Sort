"""Conservative, explainable photo recommendations with safe defaults.

Classification categories:
- exact_duplicate: Identical SHA-256 byte digest. Safe for automatic keeper recommendation.
- near_duplicate_candidate: Recompressed, resized, or slight crops of the same photo.
- possible_burst: Photos taken in rapid succession or showing the same subject/scene
  with differing expressions/poses.
- similar_only: Photos sharing visual features or scene context.
- quality_review: Single photos flagged for potential blur, exposure, or low resolution.
"""

from math import log1p

from .models import ImageRecord, QualityResult, Recommendation


WARNING_EXPLANATIONS = {
    "possibly_blurry": "Sharpness is below the configured blur threshold.",
    "possibly_underexposed": "Brightness is below the configured dark threshold.",
    "possibly_overexposed": "Brightness is above the configured bright threshold.",
    "low_resolution": "Dimensions or pixel count are below the configured resolution threshold.",
}


def _quality_score(
    path: str,
    records: dict[str, ImageRecord],
    quality: dict[str, QualityResult],
    group_paths: list[str],
) -> float:
    record = records.get(path)
    if record is None:
        return 0.0
    result = quality.get(path)

    raw_sharpness = max(result.sharpness if result and result.sharpness is not None else 0.0, 0.0)
    sharpness = log1p(raw_sharpness)
    max_sharpness = max(
        log1p(
            max(
                (
                    quality.get(item).sharpness
                    if quality.get(item) and quality.get(item).sharpness is not None
                    else 0.0
                ),
                0.0,
            )
        )
        for item in group_paths
    )
    sharpness_score = sharpness / max_sharpness if max_sharpness > 0 else 0.0

    pixels = max((record.width or 0) * (record.height or 0), 0)
    max_pixels = max(
        (records[item].width or 0) * (records[item].height or 0)
        for item in group_paths
        if item in records
    )
    resolution_score = pixels / max_pixels if max_pixels > 0 else 0.0

    brightness = result.mean_brightness if result and result.mean_brightness is not None else 127.5
    exposure_score = max(0.0, 1.0 - abs(brightness - 127.5) / 127.5)

    warning_penalty = 0.0
    if result:
        warning_penalty = min(
            0.35,
            0.12 * sum(
                warning in result.warnings
                for warning in ("possibly_blurry", "possibly_underexposed",
                                "possibly_overexposed", "low_resolution")
            ),
        )

    max_size = max((records[item].size_bytes for item in group_paths if item in records), default=1)
    size_score = record.size_bytes / max_size if max_size > 0 else 0.0

    return round(
        (
            0.4 * sharpness_score
            + 0.3 * resolution_score
            + 0.2 * exposure_score
            + 0.1 * size_score
        )
        * (1.0 - warning_penalty),
        4,
    )


def _ranked_recommendation(
    category: str,
    paths: list[str],
    records: dict[str, ImageRecord],
    quality: dict[str, QualityResult],
    confidence: str,
    uncertainty: list[str],
    auto_remove: bool = True,
) -> Recommendation:
    scores = {
        path: _quality_score(path, records, quality, paths)
        for path in paths
    }
    preferred = max(
        paths,
        key=lambda path: (scores[path], records[path].size_bytes if path in records else 0),
    )
    removal_candidates = [path for path in sorted(paths) if path != preferred] if auto_remove else []

    reasons = [
        "Preferred using sharpness, resolution, exposure, and file-size signals.",
        "This is a review suggestion only; no original file will be changed.",
    ]
    result = quality.get(preferred)
    if result:
        if result.warnings:
            reasons.append("Penalized for: " + ", ".join(result.warnings) + ".")
        if result.sharpness is not None:
            reasons.append(f"Sharpness: {result.sharpness:.2f}.")
        if result.mean_brightness is not None:
            reasons.append(f"Mean brightness: {result.mean_brightness:.2f}.")

    return Recommendation(
        category=category,
        paths=sorted(paths),
        preferred_path=preferred,
        quality_scores=scores,
        reasons=reasons,
        confidence=confidence,
        uncertainty=uncertainty,
        removal_candidate_paths=removal_candidates,
    )


def _similarity_recommendation(
    paths: list[str],
    records: dict[str, ImageRecord],
    quality: dict[str, QualityResult],
) -> Recommendation:
    scores = {path: _quality_score(path, records, quality, paths) for path in paths}
    ranked = sorted(
        paths,
        key=lambda path: (scores[path], records[path].size_bytes if path in records else 0),
        reverse=True,
    )
    preferred = ranked[0]
    runner_up = scores[ranked[1]]
    margin = scores[preferred] - runner_up
    preferred_quality = quality.get(preferred)
    safe = (
        margin >= 0.1
        and preferred_quality is not None
        and not any(
            warning in preferred_quality.warnings
            for warning in ("possibly_blurry", "possibly_underexposed", "possibly_overexposed")
        )
    )
    return Recommendation(
        category="similar_only",
        paths=sorted(paths),
        preferred_path=preferred if safe else None,
        quality_scores=scores,
        reasons=[
            "Visual evidence indicates the same content; ranking uses sharpness, resolution, exposure, and file size."
            if safe
            else "Visual similarity was found, but quality evidence does not establish a safe keeper. "
            "Different angles, expressions, poses, or moments may be meaningful.",
        ],
        confidence="medium" if safe else "low",
        uncertainty=[] if safe else ["Different angles, expressions, poses, or moments may be meaningful."],
        removal_candidate_paths=[path for path in sorted(paths) if path != preferred] if safe else sorted(paths),
    )


def _similarity_components(
    visual_groups: list[list[str]],
    near_pairs: list[dict[str, object]],
) -> list[list[str]]:
    near_edges = {
        frozenset((str(pair["left"]), str(pair["right"])))
        for pair in near_pairs
    }
    components = []
    for group in visual_groups:
        if any(
            frozenset((left, right)) in near_edges
            for index, left in enumerate(group)
            for right in group[index + 1 :]
        ):
            components.append(sorted(group))
    return components


def build_recommendations(
    images: list[ImageRecord],
    exact_groups: list[list[str]],
    near_pairs: list[dict[str, object]],
    visual_groups: list[list[str]],
    quality_results: list[QualityResult],
) -> list[Recommendation]:
    records = {image.path: image for image in images}
    quality = {result.path: result for result in quality_results}
    recommendations: list[Recommendation] = []
    exact_paths = {path for group in exact_groups for path in group}

    for group in exact_groups:
        recommendations.append(
            _ranked_recommendation(
                "exact_duplicate",
                group,
                records,
                quality,
                "high",
                ["Exact byte identity is proven by matching SHA-256 hashes."],
                auto_remove=True,
            )
        )

    burst_components = [
        [path for path in group if path not in exact_paths]
        for group in _similarity_components(visual_groups, near_pairs)
    ]
    burst_components = [group for group in burst_components if len(group) >= 2]
    burst_paths = {path for group in burst_components for path in group}

    for pair in near_pairs:
        paths = [str(pair["left"]), str(pair["right"])]
        if any(path in exact_paths for path in paths):
            continue
        if all(path in burst_paths for path in paths):
            continue

        recommendations.append(
            _ranked_recommendation(
                "near_duplicate_candidate",
                paths,
                records,
                quality,
                "medium",
                [
                    f"Perceptual hash distance: {pair.get('hash_distance', 'unknown')}.",
                    "Different angles, poses, expressions, or moments may still be meaningful.",
                ],
                auto_remove=True,
            )
        )

    for group in burst_components:
        recommendations.append(
            _ranked_recommendation(
                "possible_burst",
                group,
                records,
                quality,
                "medium",
                [
                    "Perceptual proximity and consistent local features suggest a burst-like sequence.",
                    "Review the full sequence before deciding what to keep.",
                ],
                auto_remove=False,
            )
        )

    for group in visual_groups:
        if any(set(group) == set(burst) for burst in burst_components):
            continue
        recommendations.append(_similarity_recommendation(group, records, quality))

    for result in quality_results:
        if result.warnings or result.error:
            recommendations.append(
                Recommendation(
                    category="quality_review",
                    paths=[result.path],
                    reasons=[
                        WARNING_EXPLANATIONS.get(warning, warning)
                        for warning in result.warnings
                    ]
                    + ([result.error] if result.error else []),
                    confidence="low",
                    uncertainty=["A quality warning does not mean the moment should be discarded."],
                    removal_candidate_paths=[],
                )
            )
    return recommendations
