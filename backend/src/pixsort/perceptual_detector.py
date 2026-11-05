"""Near-duplicate detection using validated pairwise perceptual hashes."""

from itertools import combinations
from typing import Iterable

import numpy as np
from PIL import Image, ImageOps
import imagehash

from .config import AnalysisConfig
from .models import ImageRecord


def calculate_phash(path: str, image_obj: Image.Image | None = None) -> str:
    """Calculate perceptual hash from oriented image with optional pre-loaded PIL object."""
    if image_obj is not None:
        return str(imagehash.phash(ImageOps.exif_transpose(image_obj)))
    with Image.open(path) as image:
        return str(imagehash.phash(ImageOps.exif_transpose(image)))


def find_near_duplicates(
    records: Iterable[ImageRecord], config: AnalysisConfig
) -> list[dict[str, object]]:
    valid = [record for record in records if record.error is None and record.perceptual_hash]
    results: list[dict[str, object]] = []
    degenerate_hashes = {"0000000000000000", "8000000000000000"}

    for left, right in combinations(valid, 2):
        distance = imagehash.hex_to_hash(left.perceptual_hash) - imagehash.hex_to_hash(right.perceptual_hash)
        if distance <= config.perceptual_hash_distance and left.sha256 != right.sha256:
            # Prevent false-positive near duplicate match between textureless/flat color images of different tones
            if left.perceptual_hash in degenerate_hashes and right.perceptual_hash in degenerate_hashes:
                try:
                    with Image.open(left.path) as img_l, Image.open(right.path) as img_r:
                        mean_l = float(np.asarray(ImageOps.exif_transpose(img_l).convert("L")).mean())
                        mean_r = float(np.asarray(ImageOps.exif_transpose(img_r).convert("L")).mean())
                        if abs(mean_l - mean_r) > 20.0:
                            continue
                except Exception:
                    pass

            results.append(
                {"left": left.path, "right": right.path, "hash_distance": distance}
            )
    return results
