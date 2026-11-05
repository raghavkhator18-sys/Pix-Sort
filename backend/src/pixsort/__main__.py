"""Command-line interface for PixSort."""

import argparse
from pathlib import Path

from .pipeline import analyze_directory


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze images without modifying them.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--html-output",
        type=Path,
        help="HTML visual review path (default: <output>/pixsort-report.html)",
    )
    parser.add_argument("--recursive", action="store_true")
    args = parser.parse_args()
    from .config import AnalysisConfig

    report = analyze_directory(args.input, AnalysisConfig(recursive=args.recursive))
    report.write_json(args.output / "pixsort-report.json")
    report.write_html(args.html_output or args.output / "pixsort-report.html")
    print(
        f"Analyzed {report.summary['processed']} of {report.summary['discovered']} images; "
        f"{len(report.exact_duplicate_groups)} exact duplicate groups, "
        f"{len(report.near_duplicate_pairs)} near-duplicate pairs."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
