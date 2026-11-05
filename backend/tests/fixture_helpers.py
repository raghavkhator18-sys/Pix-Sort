"""Resolve existing image fixtures or create isolated fallback fixtures."""

from atexit import register
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np
from PIL import Image, ImageDraw

_temporary_directory: TemporaryDirectory[str] | None = None


def _create_fallback_fixtures(directory: Path) -> None:
    base = Image.new("RGB", (320, 240), "white")
    draw = ImageDraw.Draw(base)
    draw.rectangle((18, 18, 302, 222), outline="black", width=5)
    draw.rectangle((40, 45, 280, 95), fill=(30, 100, 210))
    draw.text((55, 58), "WIFI SETTINGS", fill="white")
    draw.line((40, 130, 280, 130), fill="black", width=4)
    draw.ellipse((55, 155, 95, 195), fill=(30, 180, 80))
    draw.rectangle((125, 155, 255, 195), fill=(220, 60, 60))
    base.save(directory / "10.jpeg")

    source = cv2.imread(str(directory / "10.jpeg"))
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
    cv2.imwrite(str(directory / "11.jpeg"), perspective)

    for name, background, shape, box in (
        ("1.jpeg", (20, 20, 20), (240, 240, 240), (20, 30, 300, 210)),
        ("5.jpeg", (240, 240, 240), (20, 20, 20), (20, 30, 300, 210)),
        ("2.jpeg", (15, 80, 150), (240, 180, 30), (30, 30, 150, 210)),
        ("8.jpeg", (240, 180, 30), (15, 80, 150), (170, 30, 290, 210)),
        ("4.jpeg", (120, 20, 120), (240, 240, 240), (30, 30, 290, 100)),
        ("12.jpeg", (240, 240, 240), (120, 20, 120), (30, 140, 290, 210)),
    ):
        image = Image.new("RGB", (320, 240), background)
        image_draw = ImageDraw.Draw(image)
        image_draw.rectangle(box, fill=shape)
        image.save(directory / name)

    base.save(directory / "9.jpeg")


def get_fixture_dir() -> Path:
    """Return the project fixture directory, or an isolated fallback if absent."""
    project_fixtures = Path(__file__).parents[2] / "tests" / "Images"
    legacy_fixtures = Path(__file__).parent / "Images"
    for candidate in (project_fixtures, legacy_fixtures):
        if candidate.is_dir():
            return candidate

    global _temporary_directory
    if _temporary_directory is None:
        _temporary_directory = TemporaryDirectory(prefix="pixsort-test-images-")
        _create_fallback_fixtures(Path(_temporary_directory.name))
        register(_temporary_directory.cleanup)
    return Path(_temporary_directory.name)
