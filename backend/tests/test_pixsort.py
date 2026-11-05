from pathlib import Path
from threading import Event

import numpy as np
import pytest
from PIL import Image, ImageFilter

from pixsort.config import AnalysisConfig
from pixsort.pipeline import AnalysisCancelled, analyze_directory
from pixsort.image_loader import load_image_metadata
from pixsort.quality_analyzer import analyze_quality
from pixsort.models import ImageRecord, QualityResult
from pixsort.pipeline import AnalysisReport
from pixsort.recommendations import build_recommendations
from pixsort.similarity_detector import find_visual_groups
from fixture_helpers import get_fixture_dir


def save_image(path: Path, color: tuple[int, int, int], size=(80, 60)) -> None:
    Image.new("RGB", size, color).save(path)


def test_pipeline_empty_directory(tmp_path: Path) -> None:
    report = analyze_directory(tmp_path)
    assert report.summary == {"discovered": 0, "processed": 0, "errors": 0}
    assert report.exact_duplicate_groups == []


def test_invalid_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        analyze_directory(tmp_path / "missing")


def test_cancellation_stops_before_processing(tmp_path: Path) -> None:
    save_image(tmp_path / "photo.png", (20, 100, 200))
    cancelled = Event()
    cancelled.set()
    with pytest.raises(AnalysisCancelled):
        analyze_directory(tmp_path, cancel_event=cancelled)


def test_progress_callback_reports_reading_progress(tmp_path: Path) -> None:
    save_image(tmp_path / "photo.png", (20, 100, 200))
    events: list[tuple[int, int, str]] = []
    analyze_directory(tmp_path, progress_callback=lambda done, total, message: events.append((done, total, message)))
    assert events
    assert events[-1][0] == events[-1][1]
    assert all(
        previous[0] <= current[0] <= current[1]
        for previous, current in zip(events, events[1:])
    )
    assert events[0][0] == 0
    assert events[-1][2] == "Preparing results"


def test_progress_uses_image_count_and_keeps_post_processing_below_completion(
    tmp_path: Path,
) -> None:
    for index in range(13):
        save_image(tmp_path / f"photo-{index:02d}.png", (20 + index, 100, 200))
    events: list[tuple[int, int, str]] = []
    analyze_directory(
        tmp_path,
        progress_callback=lambda done, total, stage: events.append((done, total, stage)),
    )
    assert events[0] == (0, 13, "Loading images")
    image_events = [event for event in events if "/" in event[2]]
    assert [event[0] for event in image_events] == list(range(1, 14))
    assert all(event[1] == 13 for event in events)
    assert [event[2] for event in events[-4:-1]] == [
        "Detecting duplicates",
        "Detecting near duplicates",
        "Detecting visual similarity",
    ]
    assert events[-1][2] == "Preparing results"


def test_group_members_are_disjoint_and_cover_detected_groups(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    unrelated = tmp_path / "unrelated.png"
    save_image(first, (20, 100, 200))
    second.write_bytes(first.read_bytes())
    save_image(unrelated, (220, 30, 30))

    report = analyze_directory(tmp_path)
    grouped = [path for group in report.exact_duplicate_groups for path in group]
    assert len(grouped) == len(set(grouped)) == 2
    assert str(unrelated) not in grouped


def test_duplicates_and_corrupt_files_are_reported(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "copy.png"
    save_image(first, (20, 100, 200))
    second.write_bytes(first.read_bytes())
    (tmp_path / "broken.png").write_bytes(b"not an image")
    before = first.read_bytes()
    report = analyze_directory(tmp_path)
    assert sorted(report.exact_duplicate_groups[0]) == sorted([str(first), str(second)])
    assert len(report.errors) == 1
    assert first.read_bytes() == before


def test_resized_copy_is_near_duplicate(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    resized = tmp_path / "resized.jpg"
    image = Image.new("RGB", (100, 80), (20, 100, 200))
    image.save(source)
    image.resize((200, 160)).save(resized, quality=85)
    report = analyze_directory(tmp_path)
    assert report.near_duplicate_pairs
    assert not report.exact_duplicate_groups
    assert any(
        item["category"] == "near_duplicate_candidate"
        for item in report.recommendations
    )


def test_exact_duplicates_get_quality_ranked_recommendation(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    save_image(first, (20, 100, 200), size=(100, 80))
    second.write_bytes(first.read_bytes())
    report = analyze_directory(tmp_path)
    exact = next(
        item for item in report.recommendations if item["category"] == "exact_duplicate"
    )
    assert exact["preferred_path"] in {str(first), str(second)}
    assert "sharpness" in " ".join(exact["reasons"]).lower()


def test_near_duplicate_recommendation_prefers_sharp_high_resolution_photo(
    tmp_path: Path,
) -> None:
    paths = [tmp_path / name for name in ("soft.png", "sharp.png", "medium.png")]
    records = [
        ImageRecord(str(paths[0]), paths[0].name, 1000, width=80, height=60),
        ImageRecord(str(paths[1]), paths[1].name, 2000, width=160, height=120),
        ImageRecord(str(paths[2]), paths[2].name, 1500, width=120, height=90),
    ]
    quality = [
        QualityResult(str(paths[0]), sharpness=2, mean_brightness=127),
        QualityResult(str(paths[1]), sharpness=20, mean_brightness=127),
        QualityResult(str(paths[2]), sharpness=8, mean_brightness=127),
    ]
    result = build_recommendations(
        records,
        [],
        [{"left": str(paths[0]), "right": str(paths[1]), "hash_distance": 3}],
        [],
        quality,
    )
    recommendation = next(item for item in result if item.category == "near_duplicate_candidate")
    assert recommendation.preferred_path == str(paths[1])
    assert recommendation.quality_scores[str(paths[1])] > recommendation.quality_scores[str(paths[0])]


def test_recommendations_tolerate_missing_quality_metrics(tmp_path: Path) -> None:
    paths = [tmp_path / "left.png", tmp_path / "right.png"]
    records = [
        ImageRecord(str(paths[0]), paths[0].name, 1000, width=100, height=80),
        ImageRecord(str(paths[1]), paths[1].name, 1200, width=120, height=96),
    ]
    quality = [
        QualityResult(str(paths[0]), sharpness=None, mean_brightness=None),
        QualityResult(str(paths[1]), sharpness=10, mean_brightness=120),
    ]

    recommendations = build_recommendations(
        records,
        [],
        [{"left": str(paths[0]), "right": str(paths[1]), "hash_distance": 4}],
        [],
        quality,
    )

    recommendation = next(
        item for item in recommendations if item.category == "near_duplicate_candidate"
    )
    assert recommendation.preferred_path in {str(path) for path in paths}
    assert all(score >= 0 for score in recommendation.quality_scores.values())


def test_visual_group_gets_keeper_and_safe_candidates_when_quality_winner_is_clear(
    tmp_path: Path,
) -> None:
    paths = [tmp_path / name for name in ("soft.png", "keeper.png", "medium.png")]
    records = [
        ImageRecord(str(paths[0]), paths[0].name, 1000, width=80, height=60),
        ImageRecord(str(paths[1]), paths[1].name, 2400, width=240, height=180),
        ImageRecord(str(paths[2]), paths[2].name, 1300, width=100, height=75),
    ]
    quality = [
        QualityResult(str(paths[0]), sharpness=1, mean_brightness=127),
        QualityResult(str(paths[1]), sharpness=30, mean_brightness=127),
        QualityResult(str(paths[2]), sharpness=4, mean_brightness=127),
    ]
    recommendations = build_recommendations(
        records, [], [], [[str(path) for path in paths]], quality
    )
    recommendation = next(item for item in recommendations if item.category == "similar_only")
    assert recommendation.preferred_path == str(paths[1])
    assert recommendation.confidence == "medium"
    assert set(recommendation.removal_candidate_paths) == {str(paths[0]), str(paths[2])}


def test_visual_group_with_no_clear_quality_winner_needs_review(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("left.png", "right.png")]
    records = [
        ImageRecord(str(paths[0]), paths[0].name, 1000, width=100, height=80),
        ImageRecord(str(paths[1]), paths[1].name, 1000, width=100, height=80),
    ]
    quality = [
        QualityResult(str(paths[0]), sharpness=10, mean_brightness=127),
        QualityResult(str(paths[1]), sharpness=10, mean_brightness=127),
    ]
    recommendation = next(
        item for item in build_recommendations(
            records, [], [], [[str(path) for path in paths]], quality
        )
        if item.category == "similar_only"
    )
    assert recommendation.preferred_path is None
    assert recommendation.confidence == "low"
    assert recommendation.removal_candidate_paths == [str(paths[0]), str(paths[1])]


def test_quality_flags_dark_and_blurred_images(tmp_path: Path) -> None:
    sharp = Image.fromarray(np.full((80, 80), 128, dtype=np.uint8))
    blurred = sharp.filter(ImageFilter.GaussianBlur(8))
    dark = Image.new("RGB", (40, 40), (5, 5, 5))
    blur_path, dark_path = tmp_path / "blur.png", tmp_path / "dark.png"
    blurred.save(blur_path)
    dark.save(dark_path)
    config = AnalysisConfig(blur_threshold=1, min_width=50, min_height=50)
    result = analyze_quality(str(dark_path), config)
    assert "possibly_underexposed" in result.warnings
    assert "low_resolution" in result.warnings


def test_visual_similarity_matches_related_pair_and_rejects_unrelated_pair(
    tmp_path: Path,
) -> None:
    source = get_fixture_dir()
    related_left = tmp_path / "related-left.jpeg"
    related_right = tmp_path / "related-right.jpeg"
    unrelated_left = tmp_path / "unrelated-left.jpeg"
    unrelated_right = tmp_path / "unrelated-right.jpeg"
    related_left.write_bytes((source / "10.jpeg").read_bytes())
    related_right.write_bytes((source / "11.jpeg").read_bytes())
    unrelated_left.write_bytes((source / "1.jpeg").read_bytes())
    unrelated_right.write_bytes((source / "5.jpeg").read_bytes())

    records = [
        load_image_metadata(path)
        for path in (related_left, related_right, unrelated_left, unrelated_right)
    ]
    groups = find_visual_groups(records, AnalysisConfig())

    assert sorted([str(related_left), str(related_right)]) in groups
    assert sorted([str(unrelated_left), str(unrelated_right)]) not in groups


def test_recommendations_keep_visual_only_differences_as_review_only(tmp_path: Path) -> None:
    source = get_fixture_dir()
    left = tmp_path / "angle-left.jpeg"
    right = tmp_path / "angle-right.jpeg"
    left.write_bytes((source / "9.jpeg").read_bytes())
    right.write_bytes((source / "10.jpeg").read_bytes())
    records = [load_image_metadata(left), load_image_metadata(right)]
    quality = [analyze_quality(str(left), AnalysisConfig()), analyze_quality(str(right), AnalysisConfig())]
    recommendations = build_recommendations(
        records,
        [],
        [],
        [[str(left), str(right)]],
        quality,
    )
    similar = next(item for item in recommendations if item.category == "similar_only")
    assert similar.preferred_path is None
    assert "angle" in " ".join(similar.reasons).lower()


def test_unique_blurry_moment_is_quality_review_not_discard(tmp_path: Path) -> None:
    path = tmp_path / "blurry-moment.png"
    image = Image.new("RGB", (80, 60), (120, 120, 120)).filter(ImageFilter.GaussianBlur(8))
    image.save(path)
    report = analyze_directory(tmp_path, AnalysisConfig(blur_threshold=1000))
    review = next(
        item for item in report.recommendations if item["category"] == "quality_review"
    )
    assert review["preferred_path"] is None
    assert "sharpness" in " ".join(review["reasons"]).lower()


def test_burst_like_pair_gets_possible_burst_category() -> None:
    source = get_fixture_dir()
    records = [load_image_metadata(source / name) for name in ("10.jpeg", "11.jpeg")]
    quality = [analyze_quality(record.path, AnalysisConfig()) for record in records]
    pair = {"left": records[0].path, "right": records[1].path, "hash_distance": 2}
    recommendations = build_recommendations(
        records,
        [],
        [pair],
        [[records[0].path, records[1].path]],
        quality,
    )
    burst = next(item for item in recommendations if item.category == "possible_burst")
    assert burst.preferred_path in {records[0].path, records[1].path}


def test_visual_similarity_evaluation_metrics_on_labeled_examples() -> None:
    source = get_fixture_dir()
    labels = {
        ("10.jpeg", "11.jpeg"): True,
        ("1.jpeg", "5.jpeg"): False,
        ("2.jpeg", "8.jpeg"): False,
        ("4.jpeg", "12.jpeg"): False,
    }
    records = [load_image_metadata(path) for path in source.glob("*.jpeg")]
    groups = find_visual_groups(records, AnalysisConfig())
    predicted = {
        tuple(sorted((left, right)))
        for group in groups
        for left in group
        for right in group
        if left < right
    }
    outcomes = {
        pair: tuple(sorted((str(source / pair[0]), str(source / pair[1])))) in predicted
        for pair in labels
    }
    true_positive = sum(outcomes[pair] and label for pair, label in labels.items())
    false_positive = sum(outcomes[pair] and not label for pair, label in labels.items())
    false_negative = sum(not outcomes[pair] and label for pair, label in labels.items())
    precision = true_positive / (true_positive + false_positive)
    recall = true_positive / (true_positive + false_negative)

    assert (true_positive, false_positive, false_negative) == (1, 0, 0)
    assert precision == 1.0
    assert recall == 1.0


def test_html_report_contains_all_review_sections_and_local_images(tmp_path: Path) -> None:
    image = tmp_path / "photo.png"
    save_image(image, (20, 100, 200))
    report = AnalysisReport(
        images=[ImageRecord(str(image), image.name, image.stat().st_size)],
        exact_duplicate_groups=[[str(image)]],
        near_duplicate_pairs=[{"left": str(image), "right": str(image), "hash_distance": 3}],
        visual_similarity_groups=[[str(image)]],
        quality=[QualityResult(str(image), sharpness=12.5, mean_brightness=100, warnings=["possibly_blurry"])],
        summary={"discovered": 1, "processed": 1, "errors": 0},
    )
    output = tmp_path / "review.html"
    report.write_html(output)
    html = output.read_text(encoding="utf-8")
    assert "Exact duplicate groups" in html
    assert "Perceptual near-duplicate pairs" in html
    assert "Visual similarity groups" in html
    assert "Image-quality warnings" in html
    assert "pHash distance: 3" in html
    assert image.resolve().as_uri() in html


def test_html_report_uses_file_uri_for_paths_with_spaces(tmp_path: Path) -> None:
    image = tmp_path / "folder with spaces" / "photo image.png"
    image.parent.mkdir()
    save_image(image, (20, 100, 200))
    report = AnalysisReport(
        images=[ImageRecord(str(image), image.name, image.stat().st_size)],
        visual_similarity_groups=[[str(image)]],
        summary={"discovered": 1, "processed": 1, "errors": 0},
    )
    output = tmp_path / "review.html"
    report.write_html(output)
    html = output.read_text(encoding="utf-8")
    assert image.resolve().as_uri() in html
    assert "%20" in image.resolve().as_uri()


def test_html_report_empty_state(tmp_path: Path) -> None:
    output = tmp_path / "empty.html"
    AnalysisReport(summary={"discovered": 0, "processed": 0, "errors": 0}).write_html(output)
    html = output.read_text(encoding="utf-8")
    assert "No duplicate, near-duplicate, visual-similarity" in html


def test_html_report_handles_missing_and_unreadable_images(tmp_path: Path) -> None:
    missing = tmp_path / "missing.png"
    unreadable = tmp_path / "broken.png"
    unreadable.write_bytes(b"not an image")
    report = AnalysisReport(
        images=[ImageRecord(str(unreadable), unreadable.name, unreadable.stat().st_size, error="Unreadable image")],
        exact_duplicate_groups=[[str(missing), str(unreadable)]],
        errors=[{"path": str(unreadable), "error": "Unreadable image"}],
        summary={"discovered": 2, "processed": 0, "errors": 1},
    )
    output = tmp_path / "unavailable.html"
    report.write_html(output)
    html = output.read_text(encoding="utf-8")
    assert "Image file is unavailable" in html
    assert "Unreadable image" in html
    assert "missing.png" in html
