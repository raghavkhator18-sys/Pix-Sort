from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from pixsort.config import AnalysisConfig
from pixsort.pipeline import analyze_directory


THRESHOLDS = (8, 12, 16, 20, 24)


def create_screenshot_fixture(directory: Path) -> dict[str, Path]:
    """Create deterministic local fixtures for pHash behavior testing."""
    base = Image.new("RGB", (320, 240), "white")
    draw = ImageDraw.Draw(base)
    draw.rectangle((18, 18, 302, 222), outline="black", width=5)
    draw.rectangle((40, 45, 280, 95), fill=(30, 100, 210))
    draw.text((55, 58), "WIFI SETTINGS", fill="white")
    draw.line((40, 130, 280, 130), fill="black", width=4)
    draw.ellipse((55, 155, 95, 195), fill=(30, 180, 80))
    draw.rectangle((125, 155, 255, 195), fill=(220, 60, 60))

    paths = {"base": directory / "base.png"}
    base.save(paths["base"])
    paths["exact"] = directory / "exact.png"
    paths["exact"].write_bytes(paths["base"].read_bytes())

    paths["resized"] = directory / "resized.jpg"
    base.resize((640, 480), Image.Resampling.LANCZOS).save(
        paths["resized"], quality=72
    )

    paths["cropped"] = directory / "cropped.png"
    base.crop((10, 10, 310, 230)).resize((320, 240), Image.Resampling.LANCZOS).save(
        paths["cropped"]
    )

    source = cv2.imread(str(paths["base"]))
    height, width = source.shape[:2]
    source_corners = np.float32(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
    )
    target_corners = np.float32(
        [[18, 8], [width - 20, 20], [width - 2, height - 18], [10, height - 2]]
    )
    transform = cv2.getPerspectiveTransform(source_corners, target_corners)
    perspective = cv2.warpPerspective(
        source, transform, (width, height), borderValue=(255, 255, 255)
    )
    paths["perspective"] = directory / "perspective.png"
    cv2.imwrite(str(paths["perspective"]), perspective)

    unrelated = Image.new("RGB", (320, 240), (238, 238, 238))
    unrelated_draw = ImageDraw.Draw(unrelated)
    unrelated_draw.ellipse((30, 30, 180, 190), fill=(230, 150, 20))
    unrelated_draw.polygon(
        [(210, 30), (295, 210), (180, 210)], fill=(110, 40, 180)
    )
    paths["unrelated"] = directory / "unrelated.png"
    unrelated.save(paths["unrelated"])
    return paths


def pair_names(report, left: Path, right: Path) -> bool:
    target = {str(left), str(right)}
    return any({pair["left"], pair["right"]} == target for pair in report.near_duplicate_pairs)


@pytest.mark.parametrize("threshold", THRESHOLDS)
def test_exact_copy_is_separate_and_resized_copy_is_detected(
    tmp_path: Path, threshold: int
) -> None:
    paths = create_screenshot_fixture(tmp_path)
    report = analyze_directory(
        tmp_path, AnalysisConfig(perceptual_hash_distance=threshold)
    )

    assert sorted(report.exact_duplicate_groups) == [
        sorted([str(paths["base"]), str(paths["exact"])])
    ]
    assert not pair_names(report, paths["base"], paths["exact"])
    assert pair_names(report, paths["base"], paths["resized"])


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [(8, False), (12, True), (16, True), (20, True), (24, True)],
)
def test_crop_and_perspective_variants_across_thresholds(
    tmp_path: Path, threshold: int, expected: bool
) -> None:
    paths = create_screenshot_fixture(tmp_path)
    report = analyze_directory(
        tmp_path, AnalysisConfig(perceptual_hash_distance=threshold)
    )

    assert pair_names(report, paths["base"], paths["cropped"]) is expected
    assert pair_names(report, paths["base"], paths["perspective"]) is expected


@pytest.mark.parametrize("threshold", THRESHOLDS)
def test_unrelated_fixture_has_no_false_positive_pair(
    tmp_path: Path, threshold: int
) -> None:
    paths = create_screenshot_fixture(tmp_path)
    report = analyze_directory(
        tmp_path, AnalysisConfig(perceptual_hash_distance=threshold)
    )

    assert not pair_names(report, paths["base"], paths["unrelated"])
    assert not pair_names(report, paths["cropped"], paths["unrelated"])
    assert not pair_names(report, paths["perspective"], paths["unrelated"])
