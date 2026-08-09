from __future__ import annotations

import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import HEADERS, IMDB_CACHE_DIR, OUTPUT_DIR, STATE_FILE, BuildConfig
from .http import HttpMixin
from .metadata import MetadataMixin
from .models import Movie
from .output import OutputMixin
from .parser import ParserMixin
from .source import SourceMixin
from .utils import log, read_json, write_json


class GuideRapideBuilder(HttpMixin, MetadataMixin, SourceMixin, ParserMixin, OutputMixin):

    def __init__(self, config: Optional[BuildConfig] = None) -> None:
        self.start_ts = time.monotonic()
        self.config = config or BuildConfig.from_env()
        self.output_root = OUTPUT_DIR
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        retries = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.last_request_ts_by_bucket = {
            "guide_rapide": 0.0,
            "metadata_api": 0.0,
            "default": 0.0,
        }
        self.metadata_api_requests = 0
        self.metadata_budget_warning_emitted = False
        self.metadata_backfill_attempts = 0
        self.request_failures = 0

        self.state = read_json(STATE_FILE, default={"movies": {}, "last_run": ""})
        if not isinstance(self.state, dict):
            self.state = {"movies": {}, "last_run": ""}
        if not isinstance(self.state.get("movies"), dict):
            self.state["movies"] = {}

        self.imdb_cache: dict[str, dict] = read_json(IMDB_CACHE_DIR / "index.json", default={})
        if not isinstance(self.imdb_cache, dict):
            self.imdb_cache = {}

        self.metadata_provider = self.config.metadata_provider
        self.metadata_backfill_mode = self.config.metadata_backfill_mode
        self.omdb_api_keys = self.build_omdb_key_pool()
        self.omdb_unusable_keys: set[str] = set()

        if self.config.require_omdb_metadata:
            if self.metadata_provider != "omdb":
                raise ValueError(
                    "GR_REQUIRE_OMDB_METADATA=true requires GR_METADATA_PROVIDER=omdb"
                )
            if not self.omdb_api_keys:
                raise RuntimeError(
                    "GR_REQUIRE_OMDB_METADATA=true requires GR_OMDB_API_KEY or GR_OMDB_API_KEYS"
                )

    def close(self) -> None:
        self.session.close()

    def elapsed(self) -> str:
        seconds = int(time.monotonic() - self.start_ts)
        mins, sec = divmod(seconds, 60)
        return f"{mins:02d}:{sec:02d}"

    def build(self) -> None:
        log(f"[{self.elapsed()}] Build started")
        log(
            f"[{self.elapsed()}] Request profile: "
            f"guide-rapide(delay={self.config.guide_rapide_delay_seconds}s, "
            f"timeout={self.config.guide_rapide_request_timeout}s), "
            f"metadata-api(delay={self.config.metadata_api_delay_seconds}s, "
            f"timeout={self.config.metadata_api_request_timeout}s, "
            f"budget={self.config.max_metadata_api_lookups_per_run})"
        )
        run_profile = self.resolve_run_profile()
        discovered_urls = self.discover_film_urls(run_profile)
        movies = self.load_movies(
            discovered_urls,
            max_movie_fetch_per_run=int(run_profile["max_movie_fetch_per_run"]),
        )
        if not discovered_urls and not movies:
            raise RuntimeError(
                "Discovery returned no movie links and no cached movies are available; "
                "refusing to publish an empty catalog"
            )

        physical_movies = [m for m in movies if m.physical_available]
        physical_movies.sort(key=lambda m: (m.released, m.guide_rapide_id), reverse=True)

        # Keep one entry per canonical ID so stream providers are queried consistently.
        deduped_by_id: dict[str, Movie] = {}
        for movie in physical_movies:
            if movie.id not in deduped_by_id:
                deduped_by_id[movie.id] = movie
        physical_movies = list(deduped_by_id.values())

        catalog_defs, catalogs = self.build_catalogs(physical_movies)
        refreshed_posters, metadata_api_lookups = self.refresh_catalog_posters(catalogs)

        with tempfile.TemporaryDirectory(
            prefix=".site-build-", dir=OUTPUT_DIR.parent
        ) as temporary_output:
            self.output_root = Path(temporary_output)
            for catalog_id, entries in catalogs.items():
                self.write_catalog(catalog_id, entries)

            for movie in physical_movies:
                write_json(
                    self.output_root / "meta" / "movie" / f"{movie.id}.json",
                    {"meta": self.to_meta(movie)},
                )

            self.write_manifest(catalog_defs)
            self.write_index(
                total_movies=len(physical_movies),
                discovered_count=len(discovered_urls),
            )
            self.validate_output(catalog_defs, physical_movies)
            self.publish_output(self.output_root)

        self.output_root = OUTPUT_DIR
        self.persist_state()

        log(f"[{self.elapsed()}] Discovered movie links: {len(discovered_urls)}")
        log(f"[{self.elapsed()}] Physical movies exported: {len(physical_movies)}")
        log(f"[{self.elapsed()}] Catalogs exported: {len(catalog_defs)}")
        omdb_coverage = sum(
            1
            for movie in physical_movies
            if movie.metadata_source == "omdb" and movie.poster
        )
        log(
            f"[{self.elapsed()}] OMDb metadata/poster coverage: "
            f"{omdb_coverage}/{len(physical_movies)}"
        )
        log(
            f"[{self.elapsed()}] Catalog posters refreshed from metadata provider: "
            f"{refreshed_posters}"
        )
        log(
            f"[{self.elapsed()}] Metadata API lookups for poster/trailer backfill: "
            f"{metadata_api_lookups}"
        )
        log(f"[{self.elapsed()}] Metadata API requests total: {self.metadata_api_requests}")
        log(f"[{self.elapsed()}] Request failures: {self.request_failures}")
