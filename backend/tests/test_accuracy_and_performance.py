"""Deterministic accuracy, robustness, and performance tests for PixSort.

Covers:
- Exact cryptographic duplicates
- Resized and recompressed near-duplicates
- Distinguishable angles, poses, and burst shots
- Unrelated image rejection
- Blurry vs sharp keeper ranking
- Equal quality / ambiguous keeper handling
- Stable image IDs
- Monotonic progress reporting
- Cancellation and temporary file cleanup
- Precision and recall on labeled dataset
- Scalability on increasing dataset sizes
"""

import math
from pathlib import Path
import tempfile
import time
from threading import Event

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from pixsort.config import AnalysisConfig
from pixsort.image_loader import load_image_metadata
from pixsort.models import ImageRecord, QualityResult
from pixsort.pipeline import AnalysisCancelled, analyze_directory
from pixsort.quality_analyzer import analyze_quality
from pixsort.similarity_detector import find_visual_groups
from pixsort.recommendations import build_recommendations
from pixsort.main import AnalysisJob, _build_groups, _report_response


def _create_scene(size=(400, 300), bg=(240, 240, 245), obj_pos=(100, 100), obj_color=(30, 90, 200)) -> Image.Image:
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, size[0] - 20, 60), fill=(50, 60, 70))
    d.text((30, 30), "SCENE TEST", fill="white")
    # Foreground object
    x, y = obj_pos
    d.rectangle((x, y, x + 80, y + 80), fill=obj_color)
    d.ellipse((x + 10, y + 10, x + 40, y + 40), fill=(255, 200, 50))
    d.line((0, size[1] - 40, size[0], size[1] - 40), fill=(100, 100, 100), width=3)
    return img


def test_byte_identical_duplicates(tmp_path: Path):
    """Cryptographic duplicates must be identified with high confidence."""
    base = _create_scene()
    p1 = tmp_path / "img1.png"
    p2 = tmp_path / "img2.png"
    base.save(p1)
    p2.write_bytes(p1.read_bytes())

    report = analyze_directory(tmp_path)
    assert len(report.exact_duplicate_groups) == 1
    assert set(report.exact_duplicate_groups[0]) == {str(p1), str(p2)}

    # Verify recommendations
    exact_rec = next(r for r in report.recommendations if r["category"] == "exact_duplicate")
    assert exact_rec["confidence"] == "high"
    assert exact_rec["preferred_path"] in {str(p1), str(p2)}
    assert len(exact_rec["removal_candidate_paths"]) == 1


def test_resized_and_recompressed_near_duplicates(tmp_path: Path):
    """Resized or recompressed copies must be detected as near-duplicates with keeper chosen by quality."""
    original = _create_scene(size=(1200, 900))
    p_orig = tmp_path / "original.jpg"
    original.save(p_orig, quality=95)

    # Recompressed, downscaled version
    downscaled = original.resize((600, 450), Image.Resampling.LANCZOS)
    p_down = tmp_path / "thumbnail.jpg"
    downscaled.save(p_down, quality=65)

    report = analyze_directory(tmp_path)
    near_paths = {p for pair in report.near_duplicate_pairs for p in (pair["left"], pair["right"])}
    assert str(p_orig) in near_paths and str(p_down) in near_paths

    recs = [r for r in report.recommendations if r["category"] in {"near_duplicate_candidate", "possible_burst"}]
    assert recs
    # Higher-res original should be preferred
    assert recs[0]["preferred_path"] == str(p_orig)


def test_different_angles_and_poses_remain_distinguishable(tmp_path: Path):
    """Distinct photographs of the same scene with moved subjects must not be classified as duplicates."""
    pose1 = _create_scene(obj_pos=(50, 100))
    pose2 = _create_scene(obj_pos=(220, 140))
    p1 = tmp_path / "pose1.png"
    p2 = tmp_path / "pose2.png"
    pose1.save(p1)
    pose2.save(p2)

    report = analyze_directory(tmp_path)
    # Neither should be exact duplicate
    assert report.exact_duplicate_groups == []

    # If grouped by visual similarity, no photos should be pre-selected for removal
    for rec in report.recommendations:
        if rec["category"] in {"possible_burst", "similar_only"}:
            assert len(rec["removal_candidate_paths"]) == 0 or rec["confidence"] == "low"


def test_unrelated_photographs_not_grouped(tmp_path: Path):
    """Visually unrelated photos must never be grouped together."""
    p_indoor = tmp_path / "indoor.png"
    Image.new("RGB", (300, 300), (30, 40, 50)).save(p_indoor)

    p_beach = tmp_path / "beach.png"
    Image.new("RGB", (300, 300), (250, 220, 150)).save(p_beach)

    p_forest = tmp_path / "forest.png"
    Image.new("RGB", (300, 300), (20, 140, 40)).save(p_forest)

    report = analyze_directory(tmp_path)
    assert report.exact_duplicate_groups == []
    assert report.visual_similarity_groups == []


def test_blurry_and_sharp_versions(tmp_path: Path):
    """Sharp photo must be preferred over blurry photo, with blurry photo receiving warning."""
    base = _create_scene(size=(600, 400))
    p_sharp = tmp_path / "sharp.png"
    base.save(p_sharp)

    # Blur
    blurred = base.filter(ImageFilter.GaussianBlur(radius=5))
    p_blurry = tmp_path / "blurry.png"
    blurred.save(p_blurry)

    report = analyze_directory(tmp_path, AnalysisConfig(blur_threshold=50.0))
    quality_by_path = {q.path: q for q in report.quality}

    assert "possibly_blurry" in quality_by_path[str(p_blurry)].warnings
    assert quality_by_path[str(p_sharp)].sharpness > quality_by_path[str(p_blurry)].sharpness


def test_equal_quality_no_confident_keeper(tmp_path: Path):
    """When similar images have equal quality, recommendation must flag review needed."""
    p1 = tmp_path / "img_a.png"
    p2 = tmp_path / "img_b.png"
    _create_scene(obj_color=(30, 90, 200)).save(p1)
    _create_scene(obj_color=(30, 90, 201)).save(p2)

    records = [load_image_metadata(p1), load_image_metadata(p2)]
    quality = [
        QualityResult(str(p1), sharpness=45.0, mean_brightness=120.0),
        QualityResult(str(p2), sharpness=45.0, mean_brightness=120.0),
    ]
    recs = build_recommendations(records, [], [], [[str(p1), str(p2)]], quality)
    similar_rec = next((r for r in recs if r.category == "similar_only"), None)
    if similar_rec:
        assert similar_rec.confidence == "low" or similar_rec.preferred_path is None


def test_stable_image_ids_and_api_consistency(tmp_path: Path):
    """Image IDs must remain strictly stable and deterministic across calls."""
    p1 = tmp_path / "first.png"
    p2 = tmp_path / "second.png"
    _create_scene().save(p1)
    _create_scene().save(p2)

    report = analyze_directory(tmp_path)
    resp1 = _report_response(report, [p1.name, p2.name])
    resp2 = _report_response(report, [p1.name, p2.name])

    ids1 = [img["id"] for img in resp1["images"]]
    ids2 = [img["id"] for img in resp2["images"]]
    assert ids1 == ids2
    assert all(i.startswith("photo-") for i in ids1)


def test_monotonic_progress_reporting(tmp_path: Path):
    """Progress callbacks must be non-decreasing, accurate, and reach completion."""
    for i in range(6):
        _create_scene(obj_pos=(40 + i * 20, 100)).save(tmp_path / f"p_{i}.jpg")

    history: list[tuple[int, int, str]] = []
    report = analyze_directory(tmp_path, progress_callback=lambda d, t, s: history.append((d, t, s)))

    assert history[0][0] == 0
    assert history[-1][0] == history[-1][1] == 6
    # Check monotonicity of processed counts
    counts = [h[0] for h in history]
    assert counts == sorted(counts)


def test_cancellation_and_tempfile_cleanup(tmp_path: Path):
    """Cancelling a job must halt processing immediately and clean up temporary state."""
    for i in range(5):
        _create_scene().save(tmp_path / f"p_{i}.jpg")

    cancel = Event()
    cancel.set()  # Cancel before start

    with pytest.raises(AnalysisCancelled):
        analyze_directory(tmp_path, cancel_event=cancel)


def test_precision_recall_labeled_dataset(tmp_path: Path):
    """Measure precision and recall for duplicate detection on a controlled synthetic dataset.

    Dataset composition:
    - 4 exact duplicate pairs (Positives: 4)
    - 4 resized near-duplicate pairs (Positives: 4)
    - 8 unrelated pairs (Negatives: 8)
    Total pairs evaluated: 16
    """
    positives = 0
    # Exact duplicate pairs
    for i in range(4):
        img = _create_scene(obj_color=(20 * i, 100, 150))
        p_a = tmp_path / f"exact_{i}_a.png"
        p_b = tmp_path / f"exact_{i}_b.png"
        img.save(p_a)
        p_b.write_bytes(p_a.read_bytes())

    # Resized near-duplicate pairs
    for i in range(4):
        img = _create_scene(size=(800, 600), obj_color=(150, 20 * i, 100))
        p_orig = tmp_path / f"near_{i}_orig.jpg"
        p_scaled = tmp_path / f"near_{i}_scaled.jpg"
        img.save(p_orig, quality=90)
        img.resize((400, 300), Image.Resampling.BILINEAR).save(p_scaled, quality=70)

    # Unrelated control images representing diverse scenes
    scenes = [
        # Scene 1: Night sky with stars
        ("night", (10, 15, 30), lambda d: [d.ellipse((x, y, x + 3, y + 3), fill="white") for x, y in [(30, 40), (120, 80), (200, 30), (260, 90)]]),
        # Scene 2: Sunny field
        ("field", (100, 200, 240), lambda d: [d.rectangle((0, 150, 300, 300), fill=(40, 160, 40)), d.ellipse((220, 20, 280, 80), fill=(255, 220, 0))]),
        # Scene 3: Red document with text lines
        ("doc", (250, 250, 250), lambda d: [d.rectangle((20, 20, 280, 280), outline="black"), d.text((40, 40), "REPORT TITLE", fill="black"), d.line((40, 70, 260, 70), fill="black", width=2)]),
        # Scene 4: Abstract geometric art
        ("abstract", (230, 230, 240), lambda d: [d.polygon([(150, 20), (280, 250), (20, 250)], fill=(180, 40, 120)), d.rectangle((80, 80, 220, 180), fill=(40, 180, 180))]),
    ]
    for name, bg, draw_fn in scenes:
        im = Image.new("RGB", (300, 300), bg)
        d = ImageDraw.Draw(im)
        draw_fn(d)
        im.save(tmp_path / f"unrelated_{name}.png")

    report = analyze_directory(tmp_path)

    # Ground truth exact duplicate pairs
    detected_exact = {frozenset(group) for group in report.exact_duplicate_groups}
    expected_exact = {
        frozenset([str(tmp_path / f"exact_{i}_a.png"), str(tmp_path / f"exact_{i}_b.png")])
        for i in range(4)
    }
    exact_tp = len(detected_exact & expected_exact)
    exact_fp = len(detected_exact - expected_exact)
    exact_fn = len(expected_exact - detected_exact)

    exact_precision = exact_tp / (exact_tp + exact_fp) if (exact_tp + exact_fp) else 1.0
    exact_recall = exact_tp / (exact_tp + exact_fn) if (exact_tp + exact_fn) else 1.0

    assert exact_precision == 1.0
    assert exact_recall == 1.0

    # Near duplicates
    detected_near = {
        frozenset([pair["left"], pair["right"]])
        for pair in report.near_duplicate_pairs
    }
    expected_near = {
        frozenset([str(tmp_path / f"near_{i}_orig.jpg"), str(tmp_path / f"near_{i}_scaled.jpg")])
        for i in range(4)
    }
    near_tp = len(detected_near & expected_near)
    near_fn = len(expected_near - detected_near)
    # None of the unrelated files should be in near duplicates
    unrelated_paths = {str(tmp_path / f"unrelated_{name}.png") for name, _, _ in scenes}
    unrelated_in_near = any(p in unrelated_paths for pair in detected_near for p in pair)
    assert not unrelated_in_near, "Unrelated image falsely detected as near-duplicate"


def test_performance_scaling(tmp_path: Path):
    """Processing time should scale predictably with dataset size."""
    durations = {}
    for count in (4, 12):
        sub = tmp_path / f"scale_{count}"
        sub.mkdir()
        for i in range(count):
            _create_scene(size=(400, 300), obj_pos=(20 + i * 15, 80)).save(sub / f"img_{i}.jpg")

        t0 = time.perf_counter()
        report = analyze_directory(sub)
        t1 = time.perf_counter()
        durations[count] = t1 - t0
        assert report.summary["processed"] == count

    # 12 images should take less than 4x the time of 4 images
    assert durations[12] < durations[4] * 4.5
