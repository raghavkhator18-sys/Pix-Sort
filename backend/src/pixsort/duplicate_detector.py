"""Exact SHA-256 duplicate detection."""

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .models import ImageRecord


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_exact_duplicates(records: Iterable[ImageRecord]) -> list[list[str]]:
    by_size: dict[int, list[ImageRecord]] = defaultdict(list)
    for record in records:
        if record.error is None:
            by_size[record.size_bytes].append(record)
    groups: list[list[str]] = []
    for candidates in by_size.values():
        by_hash: dict[str, list[str]] = defaultdict(list)
        for record in candidates:
            if record.sha256:
                by_hash[record.sha256].append(record.path)
        groups.extend(sorted(paths) for paths in by_hash.values() if len(paths) > 1)
    return sorted(groups)
