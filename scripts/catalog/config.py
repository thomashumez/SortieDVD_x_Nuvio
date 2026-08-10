from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://www.guide-rapide.com/"
RSS_URL = "https://www.guide-rapide.com/fluxrss.xml"

ROOT_DIR = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT_DIR / "site"
CACHE_DIR = ROOT_DIR / "data" / "cache"
PAGE_CACHE_DIR = CACHE_DIR / "pages"
MOVIE_CACHE_DIR = CACHE_DIR / "movies"
IMDB_CACHE_DIR = CACHE_DIR / "imdb"
STATE_FILE = CACHE_DIR / "state.json"

REQUEST_TIMEOUT = 30
REQUEST_DELAY_SECONDS = 0.5
GUIDE_RAPIDE_HOST_SUFFIX = "guide-rapide.com"
METADATA_FAST_HOSTS = {
    "www.omdbapi.com",
    "omdbapi.com",
    "api.themoviedb.org",
    "www.themoviedb.org",
    "v3.sg.media-imdb.com",
    "www.imdb.com",
    "imdb.com",
}
START_YEAR = 2000
CURRENT_YEAR = datetime.now(timezone.utc).year
ARCHIVE_CACHE_TTL_HOURS = 20
MOVIE_RECHECK_DAYS = 45
IMDB_SUGGESTION_API = "https://v3.sg.media-imdb.com/suggestion"
OMDB_API_URL = "https://www.omdbapi.com/"
TMDB_API_URL = "https://api.themoviedb.org/3"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; GuideRapideNuvioBot/2.0; "
        "https://github.com/)"
    )
}

MONTHS = {
    "janvier": 1,
    "jan": 1,
    "janv": 1,
    "fevrier": 2,
    "fev": 2,
    "fevr": 2,
    "février": 2,
    "fév": 2,
    "mars": 3,
    "avril": 4,
    "avr": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "juil": 7,
    "aout": 8,
    "août": 8,
    "septembre": 9,
    "sept": 9,
    "octobre": 10,
    "oct": 10,
    "novembre": 11,
    "nov": 11,
    "decembre": 12,
    "décembre": 12,
    "dec": 12,
}


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def env_choice(name: str, default: str, choices: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{name} must be one of {allowed}, got {value!r}")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class BuildConfig:
    guide_rapide_request_timeout: int
    guide_rapide_delay_seconds: float
    metadata_api_request_timeout: int
    metadata_api_delay_seconds: float
    discovery_mode: str
    full_archive_pages: int
    incremental_archive_pages: int
    full_movie_fetch_per_run: int
    incremental_movie_fetch_per_run: int
    country_backfill_window_days: int
    max_country_backfill_per_run: int
    max_imdb_poster_refresh_per_run: int
    max_metadata_api_lookups_per_run: int
    unresolved_imdb_retry_days: int
    metadata_provider: str
    metadata_backfill_mode: str
    require_omdb_metadata: bool
    omdb_api_key: str
    omdb_api_keys_raw: str
    tmdb_api_key: str
    enable_imdb_suggestion_fallback: bool
    enable_imdb_html_fallback: bool

    @classmethod
    def from_env(cls) -> "BuildConfig":
        return cls(
            guide_rapide_request_timeout=env_int("GR_GUIDE_RAPIDE_TIMEOUT", REQUEST_TIMEOUT, minimum=1),
            guide_rapide_delay_seconds=env_float(
                "GR_GUIDE_RAPIDE_DELAY_SECONDS", REQUEST_DELAY_SECONDS
            ),
            metadata_api_request_timeout=env_int("GR_METADATA_API_TIMEOUT", 20, minimum=1),
            metadata_api_delay_seconds=env_float("GR_METADATA_API_DELAY_SECONDS", 0.1),
            discovery_mode=env_choice(
                "GR_DISCOVERY_MODE", "auto", {"auto", "full", "incremental"}
            ),
            full_archive_pages=env_int("GR_FULL_ARCHIVE_PAGES", 4000),
            incremental_archive_pages=env_int("GR_INCREMENTAL_ARCHIVE_PAGES", 150),
            full_movie_fetch_per_run=env_int("GR_FULL_MOVIE_FETCH_PER_RUN", 2500),
            incremental_movie_fetch_per_run=env_int("GR_INCREMENTAL_MOVIE_FETCH_PER_RUN", 100),
            country_backfill_window_days=env_int("GR_COUNTRY_BACKFILL_WINDOW_DAYS", 150),
            max_country_backfill_per_run=env_int("GR_MAX_COUNTRY_BACKFILL_PER_RUN", 120),
            max_imdb_poster_refresh_per_run=env_int("GR_MAX_IMDB_POSTER_REFRESH_PER_RUN", 80),
            max_metadata_api_lookups_per_run=env_int("GR_MAX_METADATA_API_LOOKUPS_PER_RUN", 120),
            unresolved_imdb_retry_days=env_int("GR_UNRESOLVED_IMDB_RETRY_DAYS", 7),
            metadata_provider=env_choice(
                "GR_METADATA_PROVIDER", "auto", {"auto", "imdb", "omdb", "tmdb"}
            ),
            metadata_backfill_mode=env_choice(
                "GR_METADATA_BACKFILL_MODE", "smart", {"off", "smart", "deep"}
            ),
            require_omdb_metadata=env_bool("GR_REQUIRE_OMDB_METADATA", False),
            omdb_api_key=os.getenv("GR_OMDB_API_KEY", "").strip(),
            omdb_api_keys_raw=os.getenv("GR_OMDB_API_KEYS", "").strip(),
            tmdb_api_key=os.getenv("GR_TMDB_API_KEY", "").strip(),
            enable_imdb_suggestion_fallback=env_bool(
                "GR_ENABLE_IMDB_SUGGESTION_FALLBACK", True
            ),
            enable_imdb_html_fallback=env_bool("GR_ENABLE_IMDB_HTML_FALLBACK", False),
        )
