"""Unified PixSort analysis pipeline, structured performance measurements, and JSON-ready report."""

import base64
from dataclasses import asdict, dataclass, field
import io
import json
import logging
from pathlib import Path
from threading import Event
import time
from typing import Any, Callable

from PIL import Image, ImageOps

from .config import AnalysisConfig
from .duplicate_detector import find_exact_duplicates, sha256_file
from .image_loader import discover_images, load_image_metadata
from .models import ImageRecord, QualityResult
from .perceptual_detector import calculate_phash, find_near_duplicates
from .quality_analyzer import analyze_quality
from .recommendations import build_recommendations
from .similarity_detector import find_visual_groups

logger = logging.getLogger("pixsort.pipeline")


class AnalysisCancelled(Exception):
    """Raised when a caller cancels an in-progress analysis."""


ProgressCallback = Callable[[int, int, str], None]


@dataclass
class AnalysisReport:
    images: list[ImageRecord] = field(default_factory=list)
    exact_duplicate_groups: list[list[str]] = field(default_factory=list)
    near_duplicate_pairs: list[dict[str, object]] = field(default_factory=list)
    visual_similarity_groups: list[list[str]] = field(default_factory=list)
    quality: list[QualityResult] = field(default_factory=list)
    recommendations: list[dict[str, object]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    timing_ms: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write_json(self, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def write_html(self, output: Path) -> None:
        from .html_report import render_html

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            render_html(
                self.images,
                self.exact_duplicate_groups,
                self.near_duplicate_pairs,
                self.visual_similarity_groups,
                self.quality,
                self.errors,
                self.summary,
                self.recommendations,
            ),
            encoding="utf-8",
        )


def _generate_thumbnail_data_url(image: Image.Image) -> str | None:
    """Generate base64 thumbnail from already opened oriented image."""
    try:
        thumb = image.convert("RGB")
        thumb.thumbnail((320, 240))
        buffer = io.BytesIO()
        thumb.save(buffer, format="JPEG", quality=82)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except (OSError, ValueError):
        return None


def analyze_directory(
    directory: str | Path,
    config: AnalysisConfig | None = None,
    cancel_event: Event | None = None,
    progress_callback: ProgressCallback | None = None,
) -> AnalysisReport:
    config = config or AnalysisConfig()
    root = Path(directory)
    if not root.is_dir():
        raise NotADirectoryError(f"Input directory does not exist: {root}")

    start_total_time = time.perf_counter()
    timings: dict[str, float] = {}

    def check_cancelled() -> None:
        if cancel_event and cancel_event.is_set():
            raise AnalysisCancelled("Analysis cancelled by user.")

    check_cancelled()
    t_start = time.perf_counter()
    image_paths = list(discover_images(root, config))
    total = len(image_paths)

    def progress(processed: int, stage: str) -> None:
        if progress_callback:
            progress_callback(processed, total, stage)

    if progress_callback:
        progress(0, "Loading images")

    records: list[ImageRecord] = []
    quality: list[QualityResult] = []

    # Single-pass decoding: sha256 + oriented metadata + phash + quality + thumbnail
    t_decode_start = time.perf_counter()
    for index, path in enumerate(image_paths, 1):
        check_cancelled()
        try:
            file_size = path.stat().st_size
            sha256_digest = sha256_file(path)
            with Image.open(path) as raw_image:
                fmt = raw_image.format
                oriented = ImageOps.exif_transpose(raw_image)
                width, height = oriented.size
                mode = oriented.mode
                phash_val = calculate_phash(str(path), image_obj=oriented)
                quality_res = analyze_quality(str(path), config, image_obj=oriented)
                thumb_url = _generate_thumbnail_data_url(oriented)

            record = ImageRecord(
                path=str(path),
                filename=path.name,
                size_bytes=file_size,
                sha256=sha256_digest,
                width=width,
                height=height,
                format=fmt,
                mode=mode,
                perceptual_hash=phash_val,
                thumbnail=thumb_url,
            )
            records.append(record)
            quality.append(quality_res)
            progress(index, f"Analyzing quality ({index}/{total})")
        except (OSError, ValueError) as exc:
            record = ImageRecord(
                path=str(path),
                filename=path.name,
                size_bytes=0,
                error=f"Unreadable image: {exc}",
            )
            records.append(record)
            progress(index, f"Loading images ({index}/{total})")

    timings["image_decoding_and_quality_ms"] = round((time.perf_counter() - t_decode_start) * 1000, 2)

    errors = [
        {"path": record.path, "error": record.error or ""}
        for record in records
        if record.error
    ]
    check_cancelled()

    # Stage: Exact duplicates
    t_exact_start = time.perf_counter()
    if progress_callback:
        progress(total, "Detecting duplicates")
    try:
        exact_groups = find_exact_duplicates(records)
    except (OSError, ValueError) as exc:
        errors.append({"path": str(root), "error": f"Exact duplicate detection failed: {exc}"})
        exact_groups = []
    timings["exact_duplicates_ms"] = round((time.perf_counter() - t_exact_start) * 1000, 2)
    check_cancelled()

    # Stage: Near duplicates
    t_near_start = time.perf_counter()
    if progress_callback:
        progress(total, "Detecting near duplicates")
    try:
        near_pairs = find_near_duplicates(records, config)
    except (OSError, ValueError) as exc:
        errors.append({"path": str(root), "error": f"Near duplicate detection failed: {exc}"})
        near_pairs = []
    timings["near_duplicates_ms"] = round((time.perf_counter() - t_near_start) * 1000, 2)
    check_cancelled()

    # Stage: Visual similarity
    t_visual_start = time.perf_counter()
    if progress_callback:
        progress(total, "Detecting visual similarity")
    try:
        visual_groups = find_visual_groups(records, config)
    except (OSError, ValueError) as exc:
        errors.append({"path": str(root), "error": f"Similarity analysis failed: {exc}"})
        visual_groups = []
    timings["visual_similarity_ms"] = round((time.perf_counter() - t_visual_start) * 1000, 2)
    check_cancelled()

    # Stage: Recommendations
    t_rec_start = time.perf_counter()
    if progress_callback:
        progress(total, "Preparing results")
    built_recommendations = build_recommendations(
        records, exact_groups, near_pairs, visual_groups, quality
    )
    timings["recommendations_ms"] = round((time.perf_counter() - t_rec_start) * 1000, 2)
    timings["total_pipeline_ms"] = round((time.perf_counter() - start_total_time) * 1000, 2)

    logger.info(
        "Analysis complete for %d images in %.2fms. Breakdown: %s",
        total,
        timings["total_pipeline_ms"],
        timings,
    )

    return AnalysisReport(
        images=records,
        exact_duplicate_groups=exact_groups,
        near_duplicate_pairs=near_pairs,
        visual_similarity_groups=visual_groups,
        quality=quality,
        recommendations=[rec.to_dict() for rec in built_recommendations],
        errors=errors,
        summary={
            "discovered": len(records),
            "processed": sum(record.error is None for record in records),
            "errors": len(errors),
        },
        timing_ms=timings,
    )
