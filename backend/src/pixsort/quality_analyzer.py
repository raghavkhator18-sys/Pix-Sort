"""Explainable image-quality review metrics with resolution normalization and caching."""

from typing import Any
import cv2
import numpy as np
from PIL import Image, ImageOps

from .config import AnalysisConfig
from .models import QualityResult


def analyze_quality(
    path: str,
    config: AnalysisConfig,
    image_obj: Image.Image | None = None,
) -> QualityResult:
    """Evaluate image sharpness, exposure, and resolution."""
    try:
        if image_obj is not None:
            oriented = ImageOps.exif_transpose(image_obj)
            width, height = oriented.size
        else:
            with Image.open(path) as raw_image:
                oriented = ImageOps.exif_transpose(raw_image)
                width, height = oriented.size

        # Normalize resolution to max 1024 to make Laplacian variance consistent across sensor sizes
        max_dim = max(width, height)
        if max_dim > 1024:
            scale = 1024.0 / max_dim
            proc_img = oriented.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.Resampling.BILINEAR,
            )
        else:
            proc_img = oriented

        gray = np.asarray(proc_img.convert("L"), dtype=np.uint8)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        brightness = float(gray.mean())

        warnings: list[str] = []
        if sharpness < config.blur_threshold:
            warnings.append("possibly_blurry")
        if brightness < config.dark_threshold:
            warnings.append("possibly_underexposed")
        elif brightness > config.bright_threshold:
            warnings.append("possibly_overexposed")

        if (
            width < config.min_width
            or height < config.min_height
            or width * height < config.min_pixels
        ):
            warnings.append("low_resolution")

        return QualityResult(
            path=path,
            sharpness=round(sharpness, 2),
            mean_brightness=round(brightness, 2),
            warnings=warnings,
        )
    except (OSError, ValueError) as exc:
        return QualityResult(path=path, error=f"Quality analysis failed: {exc}")
