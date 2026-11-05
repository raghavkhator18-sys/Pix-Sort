"""Configuration for PixSort analysis."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisConfig:
    supported_extensions: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
    perceptual_hash_distance: int = 8
    visual_similarity_threshold: float = 0.92
    visual_min_inliers: int = 12
    blur_threshold: float = 100.0
    dark_threshold: float = 45.0
    bright_threshold: float = 210.0
    min_width: int = 640
    min_height: int = 480
    min_pixels: int = 640 * 480
    recursive: bool = False
