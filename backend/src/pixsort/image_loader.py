"""Safe, non-recursive image discovery and metadata extraction."""

from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps

from .config import AnalysisConfig
from .models import ImageRecord


def discover_images(directory: Path, config: AnalysisConfig) -> Iterable[Path]:
    """Yield supported files directly inside *directory* unless recursion is enabled."""
    paths = directory.rglob("*") if config.recursive else directory.iterdir()
    extensions = {extension.lower() for extension in config.supported_extensions}
    for path in sorted(paths):
        if path.is_file() and path.suffix.lower() in extensions:
            yield path


def load_image_metadata(path: Path) -> ImageRecord:
    """Read metadata and validate an image without redundant file re-openings."""
    try:
        record = ImageRecord(path=str(path), filename=path.name, size_bytes=path.stat().st_size)
        with Image.open(path) as image:
            fmt = image.format
            oriented = ImageOps.exif_transpose(image)
            record.width, record.height = oriented.size
            record.format = fmt
            record.mode = oriented.mode
    except (OSError, ValueError) as exc:
        record = ImageRecord(
            path=str(path),
            filename=path.name,
            size_bytes=0,
            error=f"Unreadable image: {exc}",
        )
    return record
