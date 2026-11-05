"""Serializable data models used by PixSort."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ImageRecord:
    path: str
    filename: str
    size_bytes: int
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    format: str | None = None
    mode: str | None = None
    perceptual_hash: str | None = None
    thumbnail: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QualityResult:
    path: str
    sharpness: float | None = None
    mean_brightness: float | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Recommendation:
    category: str
    paths: list[str] = field(default_factory=list)
    preferred_path: str | None = None
    quality_scores: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    confidence: str = "low"
    uncertainty: list[str] = field(default_factory=list)
    removal_candidate_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
