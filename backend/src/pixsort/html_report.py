"""HTML rendering for local PixSort analysis reports."""

from html import escape
from pathlib import Path

from .models import ImageRecord, QualityResult


def _image_lookup(images: list[ImageRecord]) -> dict[str, ImageRecord]:
    return {image.path: image for image in images}


def _thumbnail(path: str, image: ImageRecord | None) -> str:
    filename = image.filename if image else Path(path).name
    escaped_name = escape(filename)
    resolved_path = Path(path).resolve()
    if image and image.error is None and resolved_path.is_file():
        source = resolved_path.as_uri()
        return (
            f'<div class="thumb"><img src="{source}" alt="{escaped_name}" '
            'onerror="this.hidden=true;this.nextElementSibling.hidden=false">'
            f'<span class="thumb-placeholder" hidden>Unavailable<br>{escaped_name}</span></div>'
        )
    reason = escape(image.error if image and image.error else "Image file is unavailable")
    return (
        f'<div class="thumb"><span class="thumb-placeholder">{escaped_name}'
        f"<br><small>{reason}</small></span></div>"
    )


def _image_card(path: str, images: dict[str, ImageRecord]) -> str:
    image = images.get(path)
    filename = image.filename if image else Path(path).name
    return (
        '<article class="image-card">'
        f"{_thumbnail(path, image)}"
        f'<div class="filename">{escape(filename)}</div>'
        f'<div class="path">{escape(path)}</div>'
        "</article>"
    )


def _group(title: str, paths: list[str], images: dict[str, ImageRecord]) -> str:
    cards = "".join(_image_card(path, images) for path in paths)
    return f'<div class="group"><h3>{escape(title)}</h3><div class="cards">{cards}</div></div>'


def render_html(
    images: list[ImageRecord],
    exact_duplicate_groups: list[list[str]],
    near_duplicate_pairs: list[dict[str, object]],
    visual_similarity_groups: list[list[str]],
    quality: list[QualityResult],
    errors: list[dict[str, str]],
    summary: dict[str, int],
    recommendations: list[dict[str, object]] | None = None,
) -> str:
    image_map = _image_lookup(images)
    exact_html = "".join(
        _group(f"Group {index}", paths, image_map)
        for index, paths in enumerate(exact_duplicate_groups, 1)
    )
    near_html = ""
    for index, pair in enumerate(near_duplicate_pairs, 1):
        left, right = str(pair["left"]), str(pair["right"])
        distance = escape(str(pair.get("hash_distance", "")))
        near_html += (
            f'<div class="group"><h3>Pair {index} '
            f'<span class="score">pHash distance: {distance}</span></h3>'
            f'<div class="cards">{_image_card(left, image_map)}'
            f"{_image_card(right, image_map)}</div></div>"
        )
    visual_html = "".join(
        _group(f"Group {index}", paths, image_map)
        for index, paths in enumerate(visual_similarity_groups, 1)
    )
    quality_html = ""
    for result in quality:
        if not result.warnings and not result.error:
            continue
        image = image_map.get(result.path)
        details = []
        if result.sharpness is not None:
            details.append(f"Sharpness: {result.sharpness:.2f}")
        if result.mean_brightness is not None:
            details.append(f"Brightness: {result.mean_brightness:.2f}")
        details.extend(result.warnings)
        if result.error:
            details.append(result.error)
        quality_html += (
            '<div class="quality-item">'
            f"{_thumbnail(result.path, image)}"
            f'<div><strong>{escape(image.filename if image else Path(result.path).name)}</strong>'
            f'<div class="warnings">{escape(", ".join(details))}</div></div></div>'
        )

    total_matches = (
        len(exact_duplicate_groups)
        + len(near_duplicate_pairs)
        + len(visual_similarity_groups)
        + sum(bool(result.warnings or result.error) for result in quality)
    )
    empty_html = (
        '<div class="empty">No duplicate, near-duplicate, visual-similarity, '
        "or image-quality matches were detected.</div>"
        if total_matches == 0
        else ""
    )
    error_html = "".join(
        f'<li><strong>{escape(item["path"])}</strong>: {escape(item["error"])}</li>'
        for item in errors
    )
    recommendation_html = ""
    for item in recommendations or []:
        paths = item.get("paths", [])
        preferred = item.get("preferred_path")
        scores = item.get("quality_scores", {})
        score_html = "".join(
            f"<li>{escape(Path(str(path)).name)}: {escape(str(score))}</li>"
            for path, score in scores.items()
        )
        preferred_html = (
            f"<p><strong>Preferred review candidate:</strong> "
            f"{escape(Path(str(preferred)).name)}</p>"
            if preferred
            else "<p><strong>No preferred photo:</strong> review each meaningful difference.</p>"
        )
        reasons = "".join(f"<li>{escape(str(reason))}</li>" for reason in item.get("reasons", []))
        uncertainty = "".join(
            f"<li>{escape(str(note))}</li>" for note in item.get("uncertainty", [])
        )
        recommendation_html += (
            '<article class="recommendation">'
            f'<h3>{escape(str(item.get("category", "review")))}</h3>'
            f"<p><strong>Confidence:</strong> {escape(str(item.get('confidence', 'low')))}</p>"
            f"{preferred_html}"
            f"<p><strong>Photos:</strong> {escape(', '.join(Path(str(path)).name for path in paths))}</p>"
            f"<details><summary>Reasons and uncertainty</summary><ul>{reasons}{uncertainty}</ul></details>"
            f"{f'<details><summary>Quality scores</summary><ul>{score_html}</ul></details>' if score_html else ''}"
            "</article>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PixSort visual review</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 0; background: #f5f7fa; color: #17202a; }}
main {{ max-width: 1180px; margin: auto; padding: 2rem; }}
h1 {{ margin-bottom: .25rem; }} h2 {{ border-bottom: 2px solid #d8dee6; padding-bottom: .5rem; }}
.summary {{ display: flex; gap: 1rem; flex-wrap: wrap; margin: 1.5rem 0; }}
.stat, .group, .quality-item, .empty {{ background: white; border: 1px solid #d8dee6; border-radius: 8px; padding: 1rem; }}
.recommendation {{ background: white; border: 1px solid #d8dee6; border-left: 4px solid #52606d; border-radius: 8px; padding: 1rem; margin: 1rem 0; }}
.stat strong {{ display: block; font-size: 1.5rem; }} .group {{ margin: 1rem 0; }}
.cards {{ display: flex; gap: 1rem; flex-wrap: wrap; }} .image-card {{ width: 190px; }}
.thumb {{ height: 130px; background: #e9edf2; border-radius: 6px; overflow: hidden; display: flex; align-items: center; justify-content: center; text-align: center; }}
.thumb img {{ width: 100%; height: 100%; object-fit: cover; }} .thumb-placeholder {{ padding: .5rem; color: #5d6875; }}
.filename {{ font-weight: 600; overflow-wrap: anywhere; margin-top: .5rem; }} .path {{ color: #687482; font-size: .75rem; overflow-wrap: anywhere; }}
.score {{ float: right; font-size: .9rem; font-weight: normal; color: #52606d; }} .quality-item {{ display: flex; gap: 1rem; margin: .75rem 0; align-items: center; }}
.quality-item .thumb {{ width: 120px; min-width: 120px; height: 85px; }} .warnings {{ color: #8a4b08; margin-top: .4rem; }}
.empty {{ color: #52606d; text-align: center; padding: 2rem; }} .errors {{ color: #8b1e1e; }}
</style>
</head>
<body><main>
<h1>PixSort visual review</h1>
<p>Local-only report. Original images were not changed or uploaded.</p>
<div class="summary">
<div class="stat"><strong>{summary.get("discovered", 0)}</strong>discovered</div>
<div class="stat"><strong>{summary.get("processed", 0)}</strong>processed</div>
<div class="stat"><strong>{summary.get("errors", 0)}</strong>errors</div>
</div>
{empty_html}
<section><h2>Recommendations</h2>{recommendation_html or '<p>None generated.</p>'}</section>
<section><h2>Exact duplicate groups</h2>{exact_html or '<p>None detected.</p>'}</section>
<section><h2>Perceptual near-duplicate pairs</h2>{near_html or '<p>None detected.</p>'}</section>
<section><h2>Visual similarity groups</h2>{visual_html or '<p>None detected.</p>'}</section>
<section><h2>Image-quality warnings</h2>{quality_html or '<p>None detected.</p>'}</section>
<section><h2>Processing errors</h2>{f'<ul class="errors">{error_html}</ul>' if error_html else '<p>None.</p>'}</section>
</main></body></html>
"""
