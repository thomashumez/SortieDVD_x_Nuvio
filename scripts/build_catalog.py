from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.catalog.builder import GuideRapideBuilder
from scripts.catalog.config import BuildConfig, MOVIE_CACHE_DIR, OUTPUT_DIR
from scripts.catalog.utils import parse_french_date, parse_timestamp, read_json, write_json
from datetime import timezone


def main() -> int:
    builder = GuideRapideBuilder()
    try:
        builder.build()
        return 0
    finally:
        builder.close()


if __name__ == "__main__":
    raise SystemExit(main())
