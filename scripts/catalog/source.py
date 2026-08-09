from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from .config import (
    ARCHIVE_CACHE_TTL_HOURS,
    BASE_URL,
    CURRENT_YEAR,
    GUIDE_RAPIDE_HOST_SUFFIX,
    IMDB_CACHE_DIR,
    MOVIE_CACHE_DIR,
    MOVIE_RECHECK_DAYS,
    PAGE_CACHE_DIR,
    RSS_URL,
    START_YEAR,
)
from .models import Movie
from .utils import (
    atomic_write_text,
    log,
    parse_iso_date,
    parse_timestamp,
    read_json,
    subtract_months,
    write_json,
)

class SourceMixin:

    def cache_key(self, url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def fetch_archive_page(self, url: str) -> Optional[str]:
        key = self.cache_key(url)
        html_path = PAGE_CACHE_DIR / f"{key}.html"
        meta_path = PAGE_CACHE_DIR / f"{key}.json"

        meta = read_json(meta_path, default={})
        fetched_at = ""
        if isinstance(meta, dict):
            fetched_at = str(meta.get("fetched_at", ""))

        if html_path.exists() and fetched_at:
            fetched_dt = parse_timestamp(fetched_at)
            if fetched_dt:
                age_hours = (datetime.now(timezone.utc) - fetched_dt).total_seconds() / 3600
                if age_hours < ARCHIVE_CACHE_TTL_HOURS:
                    return html_path.read_text(encoding="utf-8")

        html = self.fetch_url(url)
        if html is None:
            return None

        PAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(html_path, html)
        write_json(meta_path, {"url": url, "fetched_at": datetime.now(timezone.utc).isoformat()})
        return html

    def build_seed_urls(self, full_scan: bool) -> list[str]:
        seeds = [
            urljoin(BASE_URL, "accueil.html"),
            urljoin(BASE_URL, "accueil-actualite-dvd-vod-bluray-a-la-une-note.html"),
            RSS_URL,
        ]

        if not full_scan:
            today = datetime.now(timezone.utc).date()
            recent_years = {today.year, max(START_YEAR, today.year - 1)}

            for year in sorted(recent_years):
                seeds.extend(
                    [
                        urljoin(BASE_URL, f"accueil-{year}-note.html"),
                        urljoin(BASE_URL, f"dvd-vente-{year}-tous-note.html"),
                        urljoin(BASE_URL, f"dvd-vente-{year}-fil-date.html"),
                        urljoin(BASE_URL, f"dvd-vente-{year}-futur-date.html"),
                    ]
                )

            for months_back in range(0, 13):
                month_date = subtract_months(today, months_back)
                seeds.append(urljoin(BASE_URL, f"accueil-{month_date.year}-{month_date.month:02d}-note.html"))
                seeds.append(urljoin(BASE_URL, f"accueil-{month_date.year}-{month_date.month}-note.html"))

            return list(dict.fromkeys(seeds))

        for year in range(START_YEAR, CURRENT_YEAR + 2):
            seeds.extend(
                [
                    urljoin(BASE_URL, f"accueil-{year}-tous-note.html"),
                    urljoin(BASE_URL, f"accueil-{year}-note.html"),
                    urljoin(BASE_URL, f"dvd-vente-{year}-tous-note.html"),
                    urljoin(BASE_URL, f"dvd-vente-{year}-tous-note-aeca1a.html"),
                    urljoin(BASE_URL, f"dvd-vente-{year}-fil-date.html"),
                    urljoin(BASE_URL, f"dvd-vente-{year}-futur-date.html"),
                ]
            )

        for month in range(1, 13):
            seeds.append(urljoin(BASE_URL, f"accueil-{CURRENT_YEAR}-{month:02d}-note.html"))
            seeds.append(urljoin(BASE_URL, f"accueil-{CURRENT_YEAR}-{month}-note.html"))

        return list(dict.fromkeys(seeds))

    def has_cached_movies(self) -> bool:
        return any(MOVIE_CACHE_DIR.glob("film-*.json"))

    def resolve_run_profile(self) -> dict[str, object]:
        mode = self.config.discovery_mode
        has_state = bool(self.state.get("last_run"))
        has_cache = self.has_cached_movies()

        if mode == "full":
            full_scan = True
        elif mode == "incremental":
            full_scan = False
        else:
            # Auto mode: first run is full bootstrap, following runs are incremental.
            full_scan = not (has_state and has_cache)

        profile_name = "full" if full_scan else "incremental"
        return {
            "mode": profile_name,
            "full_scan": full_scan,
            "max_archive_pages": (
                self.config.full_archive_pages
                if full_scan
                else self.config.incremental_archive_pages
            ),
            "max_movie_fetch_per_run": (
                self.config.full_movie_fetch_per_run
                if full_scan
                else self.config.incremental_movie_fetch_per_run
            ),
        }

    def is_internal(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host == GUIDE_RAPIDE_HOST_SUFFIX or host.endswith(f".{GUIDE_RAPIDE_HOST_SUFFIX}")

    def is_archive_like(self, url: str) -> bool:
        if not self.is_internal(url):
            return False

        path = urlparse(url).path.lower()
        if not path.endswith(".html") and "fluxrss.xml" not in path:
            return False

        archive_tokens = (
            "accueil",
            "dvd-vente",
            "sortie",
            "palmares",
            "top",
            "film-",
            "fluxrss",
        )
        return any(token in path for token in archive_tokens)

    def extract_film_id(self, url: str) -> Optional[int]:
        match = re.search(r"film-(\d+)\.html", url)
        if not match:
            return None
        return int(match.group(1))

    def discover_film_urls(self, run_profile: dict[str, object]) -> dict[int, str]:
        max_archive_pages = int(run_profile["max_archive_pages"])
        max_movie_fetch_per_run = int(run_profile["max_movie_fetch_per_run"])
        full_scan = bool(run_profile["full_scan"])
        mode = str(run_profile["mode"])

        log(
            f"[{self.elapsed()}] Discovery start: mode={mode}, full_scan={full_scan}, "
            f"max_archive_pages={max_archive_pages}, max_movie_fetch_per_run={max_movie_fetch_per_run}"
        )
        queue = deque(self.build_seed_urls(full_scan=full_scan))
        visited: set[str] = set()
        film_urls: dict[int, str] = {}

        processed = 0
        while queue and processed < max_archive_pages:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            processed += 1

            html = self.fetch_archive_page(url)
            if not html:
                continue

            if url.lower().endswith("fluxrss.xml"):
                film_urls.update(self.extract_movie_links_from_rss(html))
                continue

            soup = BeautifulSoup(html, "lxml")

            for a_tag in soup.select("a[href]"):
                href = a_tag.get("href") or ""
                full_url = urljoin(url, href).split("#", 1)[0]
                if not self.is_internal(full_url):
                    continue

                film_id = self.extract_film_id(full_url)
                if film_id is not None:
                    film_urls[film_id] = full_url
                    continue

                if self.is_archive_like(full_url) and full_url not in visited:
                    queue.append(full_url)

            if processed % 25 == 0:
                log(
                    f"[{self.elapsed()}] Discovery progress: {processed}/{max_archive_pages} pages, "
                    f"{len(film_urls)} movie links"
                )

        log(f"[{self.elapsed()}] Discovery done: {len(film_urls)} movie links")

        return film_urls

    def extract_movie_links_from_rss(self, xml_text: str) -> dict[int, str]:
        out: dict[int, str] = {}
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError:
            return out

        for elem in root.findall(".//item/link"):
            if not elem.text:
                continue
            link = elem.text.strip().split("#", 1)[0]
            if not self.is_internal(link):
                continue
            film_id = self.extract_film_id(link)
            if film_id is not None:
                out[film_id] = link
        return out

    def should_recheck_movie(self, film_id: int) -> bool:
        state_movies = self.state["movies"]
        marker = state_movies.get(str(film_id))
        if not isinstance(marker, dict):
            return True

        checked_at = marker.get("checked_at")
        if not isinstance(checked_at, str) or not checked_at:
            return True

        checked_dt = parse_timestamp(checked_at)
        if not checked_dt:
            return True

        age_days = (datetime.now(timezone.utc) - checked_dt).total_seconds() / 86400
        return age_days >= MOVIE_RECHECK_DAYS

    def read_cached_movie(self, film_id: int) -> Optional[Movie]:
        cache_file = MOVIE_CACHE_DIR / f"film-{film_id}.json"
        payload = read_json(cache_file, default=None)
        if not isinstance(payload, dict):
            return None

        payload.setdefault("production_countries", [])
        payload.setdefault("writers", [])
        payload.setdefault("production_companies", [])
        payload.setdefault("critic_ratings", {})
        payload.setdefault("content_rating", "")
        payload.setdefault("box_office", "")
        payload.setdefault("awards", "")
        payload.setdefault("metascore", "")

        try:
            return Movie(**payload)
        except TypeError:
            return None

    def write_cached_movie(self, movie: Movie) -> None:
        MOVIE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = MOVIE_CACHE_DIR / f"film-{movie.guide_rapide_id}.json"
        write_json(cache_file, asdict(movie))

    def write_imdb_cache(self) -> None:
        IMDB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        write_json(IMDB_CACHE_DIR / "index.json", self.imdb_cache)

    def needs_country_backfill(self, movie: Movie) -> bool:
        if movie.production_countries:
            return False
        if not movie.dvd_release_date:
            return False

        dvd_dt = parse_iso_date(movie.dvd_release_date)
        if not dvd_dt:
            return False

        age_days = (datetime.now(timezone.utc).date() - dvd_dt).days
        return age_days <= self.config.country_backfill_window_days

    def backfill_production_countries(self, movie: Movie) -> bool:
        html = self.fetch_url(movie.source_url)
        if not html:
            return False

        soup = BeautifulSoup(html, "lxml")
        text_blob = soup.get_text("\n", strip=True)
        countries = self.extract_production_countries(str(soup), text_blob)
        if not countries:
            return False

        movie.production_countries = countries
        movie.checked_at = datetime.now(timezone.utc).isoformat()
        self.state["movies"][str(movie.guide_rapide_id)] = {
            "checked_at": movie.checked_at,
            "source_url": movie.source_url,
        }
        self.write_cached_movie(movie)
        return True

    def load_movies(self, discovered_urls: dict[int, str], max_movie_fetch_per_run: int) -> list[Movie]:
        movies: dict[int, Movie] = {}
        fetched_this_run = 0
        stale_or_new = 0
        skipped_due_to_cap = 0
        backfilled_country_count = 0
        imdb_rehydrated_cache_count = 0

        log(f"[{self.elapsed()}] Movie phase start: discovered_urls={len(discovered_urls)}")

        # Keep known cached movies even if not rediscovered this run.
        for movie_file in MOVIE_CACHE_DIR.glob("film-*.json"):
            payload = read_json(movie_file, default=None)
            if not isinstance(payload, dict):
                continue
            payload.setdefault("production_countries", [])
            payload.setdefault("writers", [])
            payload.setdefault("production_companies", [])
            payload.setdefault("critic_ratings", {})
            payload.setdefault("content_rating", "")
            payload.setdefault("box_office", "")
            payload.setdefault("awards", "")
            payload.setdefault("metascore", "")
            try:
                cached = Movie(**payload)
            except TypeError:
                continue
            self.ensure_canonical_id(cached)
            if self.apply_imdb_metadata(cached, allow_network=False):
                imdb_rehydrated_cache_count += 1
            movies[cached.guide_rapide_id] = cached

        for film_id, url in sorted(discovered_urls.items(), reverse=True):
            cached = self.read_cached_movie(film_id)
            if cached and not self.should_recheck_movie(film_id):
                self.ensure_canonical_id(cached)
                if self.apply_imdb_metadata(cached, allow_network=False):
                    imdb_rehydrated_cache_count += 1
                if (
                    backfilled_country_count < self.config.max_country_backfill_per_run
                    and self.needs_country_backfill(cached)
                    and self.backfill_production_countries(cached)
                ):
                    backfilled_country_count += 1
                movies[film_id] = cached
                continue

            stale_or_new += 1

            if fetched_this_run >= max_movie_fetch_per_run:
                if cached:
                    movies[film_id] = cached
                skipped_due_to_cap += 1
                continue

            html = self.fetch_url(url)
            if not html:
                if cached:
                    movies[film_id] = cached
                continue

            fetched_this_run += 1
            if fetched_this_run % 10 == 0:
                log(
                    f"[{self.elapsed()}] Movie fetch progress: {fetched_this_run}/{max_movie_fetch_per_run} "
                    f"(candidates={stale_or_new}, capped_skips={skipped_due_to_cap})"
                )

            try:
                parsed = self.parse_movie(film_id, url, html)
            except Exception as exc:
                log(
                    f"[{self.elapsed()}] Movie parse failed: film_id={film_id}, "
                    f"error={type(exc).__name__}: {exc}"
                )
                parsed = None
            if not parsed:
                if cached:
                    movies[film_id] = cached
                continue

            self.ensure_canonical_id(parsed)
            self.write_cached_movie(parsed)
            self.state["movies"][str(film_id)] = {
                "checked_at": parsed.checked_at,
                "source_url": url,
            }
            movies[film_id] = parsed

        log(
            f"[{self.elapsed()}] Movie phase done: fetched={fetched_this_run}, "
            f"candidates={stale_or_new}, capped_skips={skipped_due_to_cap}, "
            f"country_backfill={backfilled_country_count}, "
            f"imdb_rehydrated_cache={imdb_rehydrated_cache_count}"
        )

        return list(movies.values())
