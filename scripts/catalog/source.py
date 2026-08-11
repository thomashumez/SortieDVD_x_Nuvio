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
    add_months,
    atomic_write_text,
    log,
    normalize_text,
    parse_iso_date,
    parse_timestamp,
    read_json,
    normalize_provider_image_url,
    subtract_months,
    write_json,
)

class SourceMixin:

    TMDB_SYNTHETIC_ID_OFFSET = 900_000_000

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
        payload.setdefault("tmdb_id", None)
        payload.setdefault("physical_release_date", "")
        payload.setdefault("cinema_release_date", "")
        if not payload.get("physical_release_date"):
            payload["physical_release_date"] = (
                str(payload.get("released") or "")
                or str(payload.get("dvd_release_date") or "")
                or str(payload.get("bluray_release_date") or "")
            )
        payload["poster"] = normalize_provider_image_url(str(payload.get("poster") or ""))

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
        if not self.is_internal(movie.source_url):
            return False
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
        cached_fresh_hits = 0
        parse_failures = 0
        fetch_failures = 0

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
            payload.setdefault("tmdb_id", None)
            payload.setdefault("physical_release_date", "")
            payload.setdefault("cinema_release_date", "")
            if not payload.get("physical_release_date"):
                payload["physical_release_date"] = (
                    str(payload.get("released") or "")
                    or str(payload.get("dvd_release_date") or "")
                    or str(payload.get("bluray_release_date") or "")
                )
            payload["poster"] = normalize_provider_image_url(str(payload.get("poster") or ""))
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
                cached_fresh_hits += 1
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
                fetch_failures += 1
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
                parse_failures += 1
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
        log(
            f"[{self.elapsed()}] Movie phase diagnostics: "
            f"fresh_cache_hits={cached_fresh_hits}, "
            f"fetch_failures={fetch_failures}, parse_failures={parse_failures}, "
            f"cache_size_loaded={len(movies)}"
        )

        return list(movies.values())

    def tmdb_synthetic_movie_id(self, tmdb_id: int) -> int:
        return self.TMDB_SYNTHETIC_ID_OFFSET + tmdb_id

    def parse_tmdb_release_dates(
        self,
        payload: dict,
        date_from_iso: Optional[str] = None,
        date_to_iso: Optional[str] = None,
    ) -> tuple[str, str, str, str]:
        movie_results = payload.get("results")
        if not isinstance(movie_results, list):
            return "", "", "", ""

        physical_dates: list[str] = []
        digital_dates: list[str] = []
        tv_dates: list[str] = []
        cinema_dates: list[str] = []
        for country_entry in movie_results:
            if not isinstance(country_entry, dict):
                continue
            for release in country_entry.get("release_dates", []):
                if not isinstance(release, dict):
                    continue
                release_raw = normalize_text(str(release.get("release_date") or ""))
                if len(release_raw) < 10:
                    continue
                day = release_raw[:10]
                release_type = release.get("type")
                in_window = True
                if date_from_iso and day < date_from_iso:
                    in_window = False
                if date_to_iso and day > date_to_iso:
                    in_window = False

                if release_type == 5 and in_window:
                    physical_dates.append(day)
                if release_type == 4 and in_window:
                    digital_dates.append(day)
                if release_type == 6 and in_window:
                    tv_dates.append(day)
                if release_type in {2, 3}:
                    cinema_dates.append(day)

        physical = min(physical_dates) if physical_dates else ""
        digital = min(digital_dates) if digital_dates else ""
        tv = min(tv_dates) if tv_dates else ""
        cinema = min(cinema_dates) if cinema_dates else ""
        return physical, digital, tv, cinema

    def tmdb_primary_release(self, physical_date: str, digital_date: str, tv_date: str) -> tuple[str, str]:
        if digital_date:
            return "digital", digital_date
        if physical_date:
            return "physical", physical_date
        if tv_date:
            return "tv", tv_date
        return "", ""

    def tmdb_release_text(self, physical_date: str, digital_date: str, tv_date: str) -> str:
        chunks: list[str] = []
        if physical_date:
            chunks.append(f"Physical: {physical_date}")
        if digital_date:
            chunks.append(f"Digital: {digital_date}")
        if tv_date:
            chunks.append(f"TV: {tv_date}")
        if not chunks:
            return ""
        return f"TMDB releases: {' | '.join(chunks)}"

    def merge_tmdb_release_text(self, base_text: str, tmdb_text: str) -> str:
        if not tmdb_text:
            return base_text
        parts = [part.strip() for part in (base_text or "").split("|") if part.strip()]
        kept = [
            part
            for part in parts
            if "tmdb" not in part.lower()
        ]
        if kept:
            return " | ".join([*kept, tmdb_text])
        return tmdb_text

    def resolve_tmdb_id_for_movie(self, movie: Movie) -> Optional[int]:
        if movie.tmdb_id is not None:
            return movie.tmdb_id
        if not movie.imdb_id:
            return None

        find_payload = self.fetch_tmdb_json(
            f"/find/{movie.imdb_id}",
            params={"external_source": "imdb_id"},
            include_default_language=False,
            allow_when_omdb=True,
        )
        if not isinstance(find_payload, dict):
            return None

        movie_results = find_payload.get("movie_results")
        if not isinstance(movie_results, list) or not movie_results:
            return None
        first = movie_results[0]
        if not isinstance(first, dict):
            return None
        tmdb_id = first.get("id")
        if not isinstance(tmdb_id, int):
            return None
        return tmdb_id

    def refresh_tmdb_release_dates_for_library(self, movies: list[Movie]) -> tuple[int, int]:
        if not self.should_use_tmdb(allow_when_omdb=True):
            return 0, 0

        checked = 0
        updated = 0
        candidates = 0

        for movie in movies:
            tmdb_id = self.resolve_tmdb_id_for_movie(movie)
            if tmdb_id is None:
                continue

            candidates += 1

            release_payload = self.fetch_tmdb_json(
                f"/movie/{tmdb_id}/release_dates",
                include_default_language=False,
                allow_when_omdb=True,
            )
            if not isinstance(release_payload, dict):
                continue

            checked += 1
            if checked % 50 == 0:
                log(
                    f"[{self.elapsed()}] TMDB release refresh progress: "
                    f"checked={checked}, updated={updated}, candidates={candidates}"
                )

            physical_date, digital_date, tv_date, cinema_date = self.parse_tmdb_release_dates(
                release_payload
            )
            selected_release_type, selected_release_date = self.tmdb_primary_release(
                physical_date,
                digital_date,
                tv_date,
            )
            tmdb_text = self.tmdb_release_text(physical_date, digital_date, tv_date)

            changed = False
            if movie.tmdb_id != tmdb_id:
                movie.tmdb_id = tmdb_id
                changed = True

            if selected_release_date and movie.physical_release_date != selected_release_date:
                movie.physical_release_date = selected_release_date
                movie.released = selected_release_date
                movie.dvd_release_date = selected_release_date
                movie.bluray_release_date = selected_release_date
                changed = True

            if selected_release_type and movie.release_type != selected_release_type:
                movie.release_type = selected_release_type
                changed = True

            merged_text = self.merge_tmdb_release_text(movie.release_text, tmdb_text)
            if merged_text != movie.release_text:
                movie.release_text = merged_text
                changed = True

            if cinema_date and movie.cinema_release_date != cinema_date:
                movie.cinema_release_date = cinema_date
                changed = True

            if changed:
                updated += 1
                movie.checked_at = datetime.now(timezone.utc).isoformat()
                if movie.guide_rapide_id < self.TMDB_SYNTHETIC_ID_OFFSET:
                    self.write_cached_movie(movie)

        log(
            f"[{self.elapsed()}] TMDB release-date refresh: checked={checked}, updated={updated}"
        )
        return checked, updated

    def discover_tmdb_physical_movies(self) -> list[Movie]:
        if not self.config.enable_tmdb_physical_discovery:
            return []
        if not self.should_use_tmdb(allow_when_omdb=True):
            return []

        today = datetime.now(timezone.utc).date()
        date_from = subtract_months(today, self.config.tmdb_discovery_past_months)
        date_to = add_months(today, self.config.tmdb_discovery_future_months)
        date_from_iso = date_from.isoformat()
        date_to_iso = date_to.isoformat()

        params = {
            "region": "FR",
            "with_release_type": "4|5|6",
            "with_origin_country": "FR",
            "release_date.gte": date_from_iso,
            "release_date.lte": date_to_iso,
            "sort_by": "release_date.asc",
            "include_adult": "false",
            "page": "1",
        }

        discovered: list[Movie] = []
        page = 1
        total_pages = 1

        while page <= total_pages and page <= self.config.tmdb_discovery_max_pages:
            params["page"] = str(page)
            payload = self.fetch_tmdb_json(
                "/discover/movie",
                params=params,
                include_default_language=True,
                allow_when_omdb=True,
            )
            if not isinstance(payload, dict):
                break

            total_pages_raw = payload.get("total_pages")
            if isinstance(total_pages_raw, int) and total_pages_raw > 0:
                total_pages = total_pages_raw

            results = payload.get("results")
            if not isinstance(results, list):
                break

            for item in results:
                if len(discovered) >= self.config.tmdb_discovery_max_movies_per_run:
                    break
                if not isinstance(item, dict):
                    continue

                tmdb_id = item.get("id")
                if not isinstance(tmdb_id, int):
                    continue

                release_payload = self.fetch_tmdb_json(
                    f"/movie/{tmdb_id}/release_dates",
                    include_default_language=False,
                    allow_when_omdb=True,
                )
                if not isinstance(release_payload, dict):
                    continue

                physical_date, digital_date, tv_date, cinema_date = self.parse_tmdb_release_dates(
                    release_payload,
                    date_from_iso,
                    date_to_iso,
                )

                selected_release_type, selected_release_date = self.tmdb_primary_release(
                    physical_date,
                    digital_date,
                    tv_date,
                )

                if not selected_release_date:
                    continue

                external_ids_payload = self.fetch_tmdb_json(
                    f"/movie/{tmdb_id}/external_ids",
                    include_default_language=False,
                    allow_when_omdb=True,
                )

                imdb_id = ""
                if isinstance(external_ids_payload, dict):
                    maybe_imdb_id = normalize_text(str(external_ids_payload.get("imdb_id") or ""))
                    if re.fullmatch(r"tt\d+", maybe_imdb_id):
                        imdb_id = maybe_imdb_id

                title = normalize_text(str(item.get("title") or item.get("name") or ""))
                if not title:
                    continue

                release_date = normalize_text(str(item.get("release_date") or ""))
                year = None
                year_match = re.match(r"(\d{4})", release_date)
                if year_match:
                    year = int(year_match.group(1))

                poster = self.tmdb_poster_url(str(item.get("poster_path") or ""))
                synopsis = normalize_text(str(item.get("overview") or ""))
                if not cinema_date and len(release_date) >= 10:
                    cinema_date = release_date[:10]

                tmdb_release_text = self.tmdb_release_text(
                    physical_date,
                    digital_date,
                    tv_date,
                )

                movie = Movie(
                    id=self.canonical_movie_id(
                        self.tmdb_synthetic_movie_id(tmdb_id),
                        imdb_id=imdb_id,
                    ),
                    source_url=f"https://www.themoviedb.org/movie/{tmdb_id}",
                    guide_rapide_id=self.tmdb_synthetic_movie_id(tmdb_id),
                    title=title,
                    year=year,
                    director=[],
                    actors=[],
                    runtime="",
                    genres=[],
                    synopsis=synopsis,
                    rating="",
                    voters=None,
                    poster=poster,
                    trailer_url="",
                    writers=[],
                    production_companies=[],
                    critic_ratings={},
                    content_rating="",
                    box_office="",
                    awards="",
                    metascore="",
                    imdb_id=imdb_id,
                    production_countries=["France"],
                    dvd_release_date=selected_release_date,
                    bluray_release_date=selected_release_date,
                    release_type=selected_release_type,
                    release_text=tmdb_release_text,
                    released=selected_release_date,
                    physical_available=True,
                    checked_at=datetime.now(timezone.utc).isoformat(),
                    tmdb_id=tmdb_id,
                    physical_release_date=selected_release_date,
                    cinema_release_date=cinema_date,
                )

                if movie.imdb_id:
                    self.apply_imdb_metadata(movie)

                discovered.append(movie)

            if len(discovered) >= self.config.tmdb_discovery_max_movies_per_run:
                break
            page += 1

        log(
            f"[{self.elapsed()}] TMDB discovery (physical/digital/tv): {len(discovered)} movies "
            f"between {date_from_iso} and {date_to_iso}"
        )
        return discovered

    def merge_tmdb_movie_data(self, base_movie: Movie, tmdb_movie: Movie) -> bool:
        changed = False

        if tmdb_movie.physical_release_date and base_movie.physical_release_date != tmdb_movie.physical_release_date:
            base_movie.physical_release_date = tmdb_movie.physical_release_date
            base_movie.released = tmdb_movie.physical_release_date
            changed = True

        if tmdb_movie.cinema_release_date and base_movie.cinema_release_date != tmdb_movie.cinema_release_date:
            base_movie.cinema_release_date = tmdb_movie.cinema_release_date
            changed = True

        if tmdb_movie.tmdb_id is not None and base_movie.tmdb_id != tmdb_movie.tmdb_id:
            base_movie.tmdb_id = tmdb_movie.tmdb_id
            changed = True

        if tmdb_movie.imdb_id and not base_movie.imdb_id:
            base_movie.imdb_id = tmdb_movie.imdb_id
            self.ensure_canonical_id(base_movie)
            changed = True

        if tmdb_movie.release_text and "TMDB" not in base_movie.release_text:
            if base_movie.release_text:
                base_movie.release_text = f"{base_movie.release_text} | {tmdb_movie.release_text}"
            else:
                base_movie.release_text = tmdb_movie.release_text
            changed = True

        if changed:
            base_movie.checked_at = datetime.now(timezone.utc).isoformat()
        return changed

    def merge_with_tmdb_movies(self, guide_movies: list[Movie], tmdb_movies: list[Movie]) -> list[Movie]:
        merged: list[Movie] = list(guide_movies)
        by_imdb_id: dict[str, Movie] = {
            movie.imdb_id: movie
            for movie in merged
            if movie.imdb_id
        }

        merged_count = 0
        appended_count = 0

        for tmdb_movie in tmdb_movies:
            target = None
            if tmdb_movie.imdb_id:
                target = by_imdb_id.get(tmdb_movie.imdb_id)

            if target:
                if self.merge_tmdb_movie_data(target, tmdb_movie):
                    merged_count += 1
                    if target.guide_rapide_id < self.TMDB_SYNTHETIC_ID_OFFSET:
                        self.write_cached_movie(target)
                continue

            merged.append(tmdb_movie)
            if tmdb_movie.imdb_id:
                by_imdb_id[tmdb_movie.imdb_id] = tmdb_movie
            appended_count += 1

        log(
            f"[{self.elapsed()}] TMDB merge summary: merged={merged_count}, appended={appended_count}"
        )
        return merged
