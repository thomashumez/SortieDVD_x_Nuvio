from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import calendar
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, quote
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://www.guide-rapide.com/"
RSS_URL = "https://www.guide-rapide.com/fluxrss.xml"

ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT_DIR / "site"
CATALOG_DIR = OUTPUT_DIR / "catalog" / "movie"
META_DIR = OUTPUT_DIR / "meta" / "movie"
CACHE_DIR = ROOT_DIR / "data" / "cache"
PAGE_CACHE_DIR = CACHE_DIR / "pages"
MOVIE_CACHE_DIR = CACHE_DIR / "movies"
IMDB_CACHE_DIR = CACHE_DIR / "imdb"
STATE_FILE = CACHE_DIR / "state.json"

REQUEST_TIMEOUT = 30
REQUEST_DELAY_SECONDS = 0.5
DISCOVERY_MODE = os.getenv("GR_DISCOVERY_MODE", "auto").lower()
FULL_ARCHIVE_PAGES = int(os.getenv("GR_FULL_ARCHIVE_PAGES", "4000"))
INCREMENTAL_ARCHIVE_PAGES = int(os.getenv("GR_INCREMENTAL_ARCHIVE_PAGES", "150"))
FULL_MOVIE_FETCH_PER_RUN = int(os.getenv("GR_FULL_MOVIE_FETCH_PER_RUN", "2500"))
INCREMENTAL_MOVIE_FETCH_PER_RUN = int(os.getenv("GR_INCREMENTAL_MOVIE_FETCH_PER_RUN", "100"))
START_YEAR = 2000
CURRENT_YEAR = datetime.now(timezone.utc).year
ARCHIVE_CACHE_TTL_HOURS = 20
MOVIE_RECHECK_DAYS = 45
COUNTRY_BACKFILL_WINDOW_DAYS = int(os.getenv("GR_COUNTRY_BACKFILL_WINDOW_DAYS", "150"))
MAX_COUNTRY_BACKFILL_PER_RUN = int(os.getenv("GR_MAX_COUNTRY_BACKFILL_PER_RUN", "120"))
IMDB_SUGGESTION_API = "https://v3.sg.media-imdb.com/suggestion"
MAX_IMDB_POSTER_REFRESH_PER_RUN = int(os.getenv("GR_MAX_IMDB_POSTER_REFRESH_PER_RUN", "80"))

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


@dataclass
class Movie:
    id: str
    source_url: str
    guide_rapide_id: int
    title: str
    year: Optional[int]
    director: list[str]
    actors: list[str]
    runtime: str
    genres: list[str]
    synopsis: str
    rating: str
    voters: Optional[int]
    poster: str
    trailer_url: str
    imdb_id: str
    production_countries: list[str]
    dvd_release_date: str
    bluray_release_date: str
    release_type: str
    release_text: str
    released: str
    physical_available: bool
    checked_at: str


@dataclass
class ImdbMetadata:
    title: str = ""
    year: Optional[int] = None
    director: list[str] | None = None
    actors: list[str] | None = None
    poster: str = ""
    description: str = ""
    genres: list[str] | None = None
    rating: str = ""
    voters: Optional[int] = None
    runtime: str = ""
    trailer_url: str = ""


def normalize_text(value: str) -> str:
    return " ".join(value.split()).strip()


def strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )


def parse_int(value: str) -> Optional[int]:
    cleaned = re.sub(r"[^\d]", "", value)
    if not cleaned:
        return None
    return int(cleaned)


def parse_french_date(raw: str) -> Optional[datetime]:
    value = normalize_text(raw).lower()
    value = value.replace("1er", "1")
    value = strip_accents(value)
    value = re.sub(r"\b(vers|environ|sortie|prevue|prévue)\b", " ", value)
    match = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", value)
    if not match:
        return None

    day = int(match.group(1))
    month = MONTHS.get(match.group(2))
    year = int(match.group(3))
    if not month:
        return None

    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def dt_to_iso(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.date().isoformat()


def parse_iso_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def split_list(value: str) -> list[str]:
    parts = [normalize_text(x) for x in re.split(r"\s*,\s*", value)]
    return [x for x in parts if x]


def normalize_image_url(url: str) -> str:
    cleaned = normalize_text(url)
    if not cleaned:
        return ""
    if cleaned.startswith("http://www.guide-rapide.com/"):
        return "https://www.guide-rapide.com/" + cleaned.removeprefix("http://www.guide-rapide.com/")
    if cleaned.startswith("http://guide-rapide.com/"):
        return "https://guide-rapide.com/" + cleaned.removeprefix("http://guide-rapide.com/")
    return cleaned


def subtract_months(src: date, months: int) -> date:
    month_index = src.month - 1 - months
    year = src.year + (month_index // 12)
    month = (month_index % 12) + 1
    day = min(src.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def log(message: str) -> None:
    print(message, flush=True)


class GuideRapideBuilder:
    def __init__(self) -> None:
        self.start_ts = time.monotonic()
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

        self.last_request_ts = 0.0

        self.state = read_json(STATE_FILE, default={"movies": {}, "last_run": ""})
        if not isinstance(self.state, dict):
            self.state = {"movies": {}, "last_run": ""}
        if not isinstance(self.state.get("movies"), dict):
            self.state["movies"] = {}

        self.imdb_cache: dict[str, dict] = read_json(IMDB_CACHE_DIR / "index.json", default={})
        if not isinstance(self.imdb_cache, dict):
            self.imdb_cache = {}

    def elapsed(self) -> str:
        seconds = int(time.monotonic() - self.start_ts)
        mins, sec = divmod(seconds, 60)
        return f"{mins:02d}:{sec:02d}"

    def throttle(self) -> None:
        now = time.time()
        wait_for = REQUEST_DELAY_SECONDS - (now - self.last_request_ts)
        if wait_for > 0:
            time.sleep(wait_for)

    def fetch_url(self, url: str) -> Optional[str]:
        self.throttle()
        self.last_request_ts = time.time()
        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            return response.text
        except requests.RequestException:
            return None

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
            try:
                fetched_dt = datetime.fromisoformat(fetched_at)
                age_hours = (datetime.now(timezone.utc) - fetched_dt).total_seconds() / 3600
                if age_hours < ARCHIVE_CACHE_TTL_HOURS:
                    return html_path.read_text(encoding="utf-8")
            except ValueError:
                pass

        html = self.fetch_url(url)
        if html is None:
            return None

        PAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
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
        mode = DISCOVERY_MODE if DISCOVERY_MODE in {"auto", "full", "incremental"} else "auto"
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
            "max_archive_pages": FULL_ARCHIVE_PAGES if full_scan else INCREMENTAL_ARCHIVE_PAGES,
            "max_movie_fetch_per_run": FULL_MOVIE_FETCH_PER_RUN if full_scan else INCREMENTAL_MOVIE_FETCH_PER_RUN,
        }

    def is_internal(self, url: str) -> bool:
        return urlparse(url).netloc.endswith("guide-rapide.com")

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
            link = elem.text.strip()
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

        try:
            checked_dt = datetime.fromisoformat(checked_at)
        except ValueError:
            return True

        age_days = (datetime.now(timezone.utc) - checked_dt).total_seconds() / 86400
        return age_days >= MOVIE_RECHECK_DAYS

    def read_cached_movie(self, film_id: int) -> Optional[Movie]:
        cache_file = MOVIE_CACHE_DIR / f"film-{film_id}.json"
        payload = read_json(cache_file, default=None)
        if not isinstance(payload, dict):
            return None

        payload.setdefault("production_countries", [])

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

    def extract_release_dates(self, soup: BeautifulSoup, raw_html: str) -> tuple[Optional[datetime], Optional[datetime], str]:
        dvd_date: Optional[datetime] = None
        bluray_date: Optional[datetime] = None
        release_text_parts: list[str] = []

        # Variant 1: explicit labels in plain text.
        combined = re.search(r"Date vente dvd\s+et\s+BLU\s*RAY\s*:\s*([^<\n]+)", raw_html, re.IGNORECASE)
        dvd_only = re.search(r"Date vente dvd\s*:\s*([^<\n]+)", raw_html, re.IGNORECASE)
        br_only = re.search(r"Date vente\s+BLU\s*RAY\s*:\s*([^<\n]+)", raw_html, re.IGNORECASE)

        if combined:
            text = normalize_text(combined.group(1))
            release_text_parts.append(f"DVD + Blu-ray: {text}")
            dt = parse_french_date(text)
            dvd_date = dt or dvd_date
            bluray_date = dt or bluray_date

        if dvd_only:
            text = normalize_text(dvd_only.group(1))
            release_text_parts.append(f"DVD: {text}")
            dt = parse_french_date(text)
            dvd_date = dt or dvd_date

        if br_only:
            text = normalize_text(br_only.group(1))
            release_text_parts.append(f"Blu-ray: {text}")
            dt = parse_french_date(text)
            bluray_date = dt or bluray_date

        # Variant 2: two-column date table currently used on many pages.
        for td in soup.find_all("td"):
            label_text = normalize_text(td.get_text(" ", strip=True)).lower()
            if "vente" not in label_text or ("dvd" not in label_text and "blu" not in label_text):
                continue

            row = td.find_parent("tr")
            if not row:
                continue

            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            labels = [normalize_text(x).lower() for x in cells[0].stripped_strings]
            values = [normalize_text(x) for x in cells[1].stripped_strings]
            if not labels or not values:
                continue

            for i, label in enumerate(labels):
                if i >= len(values):
                    break
                value = normalize_text(values[i]).lstrip(":").strip()
                if not value:
                    continue
                if "inconnue" in value.lower():
                    release_text_parts.append(f"{labels[i]}: {value}")
                    continue

                dt = parse_french_date(value)
                if not dt:
                    continue

                if "dvd" in label:
                    dvd_date = dt
                    release_text_parts.append(f"DVD: {value}")
                if "blu" in label:
                    bluray_date = dt
                    release_text_parts.append(f"Blu-ray: {value}")

        release_text = " | ".join(dict.fromkeys(release_text_parts))
        return dvd_date, bluray_date, release_text

    def extract_title(self, soup: BeautifulSoup) -> str:
        h2_name = soup.select_one('span[itemprop="name"]')
        if h2_name:
            name = normalize_text(h2_name.get_text(" ", strip=True))
            if name:
                return name

        h1 = soup.select_one("h1")
        if h1:
            name = normalize_text(h1.get_text(" ", strip=True))
            if name:
                return name

        title_tag = soup.select_one("title")
        if title_tag:
            text = normalize_text(title_tag.get_text(" ", strip=True))
            text = re.sub(r"\s+-\s+.*$", "", text)
            if text:
                return text

        return ""

    def extract_directors(self, soup: BeautifulSoup) -> list[str]:
        directors = []
        for span in soup.select('span[itemprop="director"] span[itemprop="name"]'):
            value = normalize_text(span.get_text(" ", strip=True))
            if value:
                directors.append(value)
        if directors:
            return list(dict.fromkeys(directors))

        raw = normalize_text(soup.get_text(" ", strip=True))
        match = re.search(r"par\s*:\s*([^\n]+?)\s*(Avec:|Dur[eé]e:|Genre:)", raw, re.IGNORECASE)
        if not match:
            return []
        return split_list(match.group(1))

    def extract_actors(self, soup: BeautifulSoup) -> list[str]:
        actors = []
        for a_tag in soup.select('a[href*="acteur-"]'):
            value = normalize_text(a_tag.get_text(" ", strip=True))
            if value:
                actors.append(value)
        return list(dict.fromkeys(actors))

    def extract_runtime(self, soup: BeautifulSoup, text_blob: str) -> str:
        duration = soup.select_one('span[itemprop="duration"]')
        if duration:
            return normalize_text(duration.get_text(" ", strip=True))

        match = re.search(r"Dur[ée]e\s*:\s*([0-9hmin\s]+)", text_blob, re.IGNORECASE)
        return normalize_text(match.group(1)) if match else ""

    def extract_genres(self, soup: BeautifulSoup, text_blob: str) -> list[str]:
        genre_span = soup.select_one('span[itemprop="genre"]')
        if genre_span:
            return split_list(genre_span.get_text(" ", strip=True))

        match = re.search(r"Genre\s*:\s*([^\n]+)", text_blob, re.IGNORECASE)
        if not match:
            return []
        return split_list(match.group(1))

    def extract_synopsis(self, soup: BeautifulSoup, raw_html: str) -> str:
        story = soup.select_one('div[itemprop="description"]')
        if story:
            text = normalize_text(story.get_text(" ", strip=True))
            if text:
                return text

        match = re.search(
            r"Synopsis(?: usuel)?\s*:\s*(.+?)(?:<br\s*/?>\s*<br\s*/?>|</td>|$)",
            raw_html,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return ""
        return normalize_text(re.sub(r"<[^>]+>", " ", match.group(1)))

    def extract_rating(self, soup: BeautifulSoup) -> tuple[str, Optional[int]]:
        rating = ""
        voters = None

        rating_node = soup.select_one('[itemprop="ratingValue"]')
        if rating_node:
            rating = normalize_text(rating_node.get_text(" ", strip=True)).replace(",", ".")

        voters_node = soup.select_one('[itemprop="ratingCount"]')
        if voters_node:
            voters = parse_int(voters_node.get_text(" ", strip=True))

        return rating, voters

    def extract_poster(self, soup: BeautifulSoup, page_url: str) -> str:
        og = soup.select_one('meta[property="og:image"]')
        if og and og.get("content"):
            return normalize_image_url(urljoin(page_url, og["content"]))

        for img in soup.select("img[src]"):
            src = img.get("src") or ""
            alt = (img.get("alt") or "").lower()
            lowered_src = src.lower()
            if any(token in lowered_src for token in ("imdb-", "allocine", "favicon", "logo/")):
                continue

            width = parse_int(str(img.get("width") or "")) or 0
            height = parse_int(str(img.get("height") or "")) or 0

            if "img/affiches/" in lowered_src:
                return normalize_image_url(urljoin(page_url, src))
            if ("sortie" in alt or "affiche" in lowered_src) and (width >= 180 or height >= 260):
                return normalize_image_url(urljoin(page_url, src))

        fallback = soup.select_one("img[src]")
        if fallback and fallback.get("src"):
            return normalize_image_url(urljoin(page_url, fallback["src"]))

        return "https://www.guide-rapide.com/IMG/divers/favicon.ico"

    def extract_imdb_id(self, soup: BeautifulSoup) -> str:
        imdb_link = soup.select_one('a[href*="imdb.com/title/"]')
        if not imdb_link or not imdb_link.get("href"):
            return ""
        match = re.search(r"/title/(tt\d+)", imdb_link["href"])
        return match.group(1) if match else ""

    def lookup_imdb_id_by_title(self, title: str, year: Optional[int]) -> str:
        cleaned_title = normalize_text(title)
        if not cleaned_title:
            return ""

        cache_key = f"search::{cleaned_title.lower()}::{year or ''}"
        cached = self.imdb_cache.get(cache_key)
        if isinstance(cached, dict):
            imdb_id = str(cached.get("imdb_id", ""))
            return imdb_id if re.fullmatch(r"tt\d+", imdb_id) else ""

        normalized_query = strip_accents(cleaned_title).lower()
        safe_query = re.sub(r"[^a-z0-9 ]+", " ", normalized_query)
        safe_query = normalize_text(safe_query)
        if not safe_query:
            self.imdb_cache[cache_key] = {"imdb_id": ""}
            return ""

        first = safe_query[0]
        encoded_query = quote(safe_query)
        url = f"{IMDB_SUGGESTION_API}/{first}/{encoded_query}.json"
        raw = self.fetch_url(url)
        if not raw:
            self.imdb_cache[cache_key] = {"imdb_id": ""}
            return ""

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.imdb_cache[cache_key] = {"imdb_id": ""}
            return ""

        candidates = payload.get("d") if isinstance(payload, dict) else None
        if not isinstance(candidates, list):
            self.imdb_cache[cache_key] = {"imdb_id": ""}
            return ""

        best_id = ""
        query_norm = strip_accents(cleaned_title).lower()

        for item in candidates:
            if not isinstance(item, dict):
                continue
            imdb_id = str(item.get("id", ""))
            if not re.fullmatch(r"tt\d+", imdb_id):
                continue

            item_title = normalize_text(str(item.get("l", "")))
            if not item_title:
                continue

            item_norm = strip_accents(item_title).lower()
            if query_norm not in item_norm and item_norm not in query_norm:
                continue

            item_year = item.get("y")
            if isinstance(item_year, int) and year is not None and abs(item_year - year) > 1:
                continue

            best_id = imdb_id
            break

        self.imdb_cache[cache_key] = {"imdb_id": best_id}
        return best_id

    def canonical_movie_id(self, guide_rapide_id: int, imdb_id: str) -> str:
        if imdb_id and re.fullmatch(r"tt\d+", imdb_id):
            return imdb_id
        return f"gr-film-{guide_rapide_id}"

    def ensure_canonical_id(self, movie: Movie) -> None:
        movie.id = self.canonical_movie_id(movie.guide_rapide_id, movie.imdb_id)

    def extract_production_countries(self, raw_html: str, text_blob: str) -> list[str]:
        html_match = re.search(
            r"Film\s+r[ée]alis[ée]?\s+en\s*<strong>\d{4}</strong>\s*,\s*(.+?)\s*,\s*par\s*:",
            raw_html,
            re.IGNORECASE | re.DOTALL,
        )
        country_blob = ""
        if html_match:
            country_blob = normalize_text(re.sub(r"<[^>]+>", " ", html_match.group(1)))
        else:
            text_match = re.search(
                r"Film\s+r[ée]alis[ée]?\s+en\s+\d{4}\s*,\s*(.+?)\s*,\s*par\s*:",
                text_blob,
                re.IGNORECASE,
            )
            if text_match:
                country_blob = normalize_text(text_match.group(1))

        if not country_blob:
            return []

        countries = [
            normalize_text(part)
            for part in re.split(r"\s*(?:/|\||;|\+|\set\s|\s&\s)\s*", country_blob, flags=re.IGNORECASE)
            if normalize_text(part)
        ]
        return list(dict.fromkeys(countries))

    def is_french_production(self, movie: Movie) -> bool:
        for country in movie.production_countries:
            normalized = strip_accents(country).lower()
            if "france" in normalized or "francais" in normalized:
                return True
        return False

    def extract_trailer_url(self, raw_html: str) -> str:
        text = raw_html.replace("\\/", "/")
        match = re.search(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)[^\"'<>\s]+", text)
        if not match:
            return ""
        return match.group(0)

    def parse_movie(self, film_id: int, url: str, html: str) -> Optional[Movie]:
        soup = BeautifulSoup(html, "lxml")
        raw_html = str(soup)
        text_blob = soup.get_text("\n", strip=True)

        title = self.extract_title(soup)
        if not title:
            return None

        year_match = re.search(r"(?:Film r[ée]alis[ée]? en|Film DTV,?)\s*<strong>(\d{4})</strong>", raw_html, re.IGNORECASE)
        if not year_match:
            year_match = re.search(r"\b(19\d{2}|20\d{2})\b", text_blob)
        year = int(year_match.group(1)) if year_match else None

        dvd_dt, bluray_dt, release_text = self.extract_release_dates(soup, raw_html)
        physical_available = bool(dvd_dt or bluray_dt)

        release_type = ""
        if dvd_dt and bluray_dt:
            release_type = "dvd+bluray"
        elif bluray_dt:
            release_type = "bluray"
        elif dvd_dt:
            release_type = "dvd"

        released_dt: Optional[datetime] = None
        if dvd_dt and bluray_dt:
            released_dt = min(dvd_dt, bluray_dt)
        else:
            released_dt = dvd_dt or bluray_dt

        rating, voters = self.extract_rating(soup)
        imdb_id = self.extract_imdb_id(soup)
        if not imdb_id:
            imdb_id = self.lookup_imdb_id_by_title(title, year)

        movie = Movie(
            id=self.canonical_movie_id(film_id, imdb_id=imdb_id),
            source_url=url,
            guide_rapide_id=film_id,
            title=title,
            year=year,
            director=self.extract_directors(soup),
            actors=self.extract_actors(soup),
            runtime=self.extract_runtime(soup, text_blob),
            genres=self.extract_genres(soup, text_blob),
            synopsis=self.extract_synopsis(soup, raw_html),
            rating=rating,
            voters=voters,
            poster=self.extract_poster(soup, url),
            trailer_url=self.extract_trailer_url(raw_html),
            imdb_id=imdb_id,
            production_countries=self.extract_production_countries(raw_html, text_blob),
            dvd_release_date=dt_to_iso(dvd_dt),
            bluray_release_date=dt_to_iso(bluray_dt),
            release_type=release_type,
            release_text=release_text,
            released=dt_to_iso(released_dt),
            physical_available=physical_available,
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

        self.apply_imdb_metadata(movie)
        return movie

    def fetch_imdb_metadata(self, imdb_id: str) -> ImdbMetadata:
        if not imdb_id:
            return ImdbMetadata()

        cached = self.imdb_cache.get(imdb_id)
        if isinstance(cached, dict):
            try:
                return ImdbMetadata(**cached)
            except TypeError:
                pass

        url = f"https://www.imdb.com/title/{imdb_id}/"
        html = self.fetch_url(url)
        if not html:
            return ImdbMetadata()

        soup = BeautifulSoup(html, "lxml")
        data = {}
        for tag in soup.select('script[type="application/ld+json"]'):
            text = tag.string or tag.get_text("", strip=True)
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue

            if isinstance(parsed, dict) and parsed.get("@type") == "Movie":
                data = parsed
                break
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get("@type") == "Movie":
                        data = item
                        break
                if data:
                    break

        if not data:
            return ImdbMetadata()

        rating = ""
        voters = None
        aggregate = data.get("aggregateRating")
        if isinstance(aggregate, dict):
            rating_raw = aggregate.get("ratingValue")
            if isinstance(rating_raw, (int, float, str)):
                rating = str(rating_raw)
            voters_raw = aggregate.get("ratingCount")
            if isinstance(voters_raw, (int, float, str)):
                voters = parse_int(str(voters_raw))

        runtime = ""
        duration_raw = data.get("duration")
        if isinstance(duration_raw, str):
            m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", duration_raw)
            if m:
                h = int(m.group(1) or 0)
                mins = int(m.group(2) or 0)
                if h and mins:
                    runtime = f"{h}h{mins:02d}"
                elif h:
                    runtime = f"{h}h"
                elif mins:
                    runtime = f"{mins}min"

        genres: list[str] = []
        genre_raw = data.get("genre")
        if isinstance(genre_raw, str):
            genres = [normalize_text(genre_raw)]
        elif isinstance(genre_raw, list):
            genres = [normalize_text(str(x)) for x in genre_raw if normalize_text(str(x))]

        directors: list[str] = []
        director_raw = data.get("director")
        if isinstance(director_raw, dict):
            name = normalize_text(str(director_raw.get("name") or ""))
            if name:
                directors.append(name)
        elif isinstance(director_raw, list):
            for item in director_raw:
                if not isinstance(item, dict):
                    continue
                name = normalize_text(str(item.get("name") or ""))
                if name:
                    directors.append(name)

        actors: list[str] = []
        actor_raw = data.get("actor")
        if isinstance(actor_raw, dict):
            name = normalize_text(str(actor_raw.get("name") or ""))
            if name:
                actors.append(name)
        elif isinstance(actor_raw, list):
            for item in actor_raw:
                if not isinstance(item, dict):
                    continue
                name = normalize_text(str(item.get("name") or ""))
                if name:
                    actors.append(name)

        year = None
        date_published = normalize_text(str(data.get("datePublished") or ""))
        year_match = re.match(r"(\d{4})", date_published)
        if year_match:
            year = int(year_match.group(1))

        trailer_url = ""
        trailer_raw = data.get("trailer")
        if isinstance(trailer_raw, dict):
            trailer_url = normalize_text(
                str(trailer_raw.get("embedUrl") or trailer_raw.get("url") or "")
            )

        meta = ImdbMetadata(
            title=normalize_text(str(data.get("name") or "")),
            year=year,
            director=list(dict.fromkeys(directors)) or None,
            actors=list(dict.fromkeys(actors)) or None,
            poster=normalize_image_url(str(data.get("image") or "").strip()),
            description=normalize_text(str(data.get("description") or "")),
            genres=genres,
            rating=rating,
            voters=voters,
            runtime=runtime,
            trailer_url=trailer_url,
        )
        self.imdb_cache[imdb_id] = asdict(meta)
        return meta

    def apply_imdb_metadata(self, movie: Movie) -> bool:
        if not movie.imdb_id:
            return False

        imdb = self.fetch_imdb_metadata(movie.imdb_id)
        changed = False

        if imdb.title and movie.title != imdb.title:
            movie.title = imdb.title
            changed = True
        if imdb.year is not None and movie.year != imdb.year:
            movie.year = imdb.year
            changed = True
        if imdb.director and movie.director != imdb.director:
            movie.director = imdb.director
            changed = True
        if imdb.actors and movie.actors != imdb.actors:
            movie.actors = imdb.actors
            changed = True
        if imdb.poster and movie.poster != imdb.poster:
            movie.poster = imdb.poster
            changed = True
        if imdb.description and movie.synopsis != imdb.description:
            movie.synopsis = imdb.description
            changed = True
        if imdb.genres and movie.genres != imdb.genres:
            movie.genres = imdb.genres
            changed = True
        if imdb.rating and movie.rating != imdb.rating:
            movie.rating = imdb.rating
            changed = True
        if imdb.voters is not None and movie.voters != imdb.voters:
            movie.voters = imdb.voters
            changed = True
        if imdb.runtime and movie.runtime != imdb.runtime:
            movie.runtime = imdb.runtime
            changed = True
        if imdb.trailer_url and movie.trailer_url != imdb.trailer_url:
            movie.trailer_url = imdb.trailer_url
            changed = True

        return changed

    def should_refresh_poster_from_imdb(self, movie: Movie) -> bool:
        if not movie.imdb_id:
            return False
        if not movie.poster:
            return True
        lowered = movie.poster.lower()
        if lowered.startswith("http://"):
            return True
        if "guide-rapide.com" in lowered:
            return True
        return False

    def refresh_catalog_posters(self, catalogs: dict[str, list[Movie]]) -> int:
        refreshed = 0
        seen_ids: set[str] = set()

        for entries in catalogs.values():
            for movie in entries:
                if refreshed >= MAX_IMDB_POSTER_REFRESH_PER_RUN:
                    return refreshed
                if movie.id in seen_ids:
                    continue
                seen_ids.add(movie.id)

                movie.poster = normalize_image_url(movie.poster)
                if not self.should_refresh_poster_from_imdb(movie):
                    continue

                imdb = self.fetch_imdb_metadata(movie.imdb_id)
                if not imdb.poster:
                    continue

                if movie.poster != imdb.poster:
                    movie.poster = imdb.poster
                    self.write_cached_movie(movie)
                    refreshed += 1

        return refreshed

    def needs_country_backfill(self, movie: Movie) -> bool:
        if movie.production_countries:
            return False
        if not movie.dvd_release_date:
            return False

        dvd_dt = parse_iso_date(movie.dvd_release_date)
        if not dvd_dt:
            return False

        age_days = (datetime.now(timezone.utc).date() - dvd_dt).days
        return age_days <= COUNTRY_BACKFILL_WINDOW_DAYS

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
            try:
                cached = Movie(**payload)
            except TypeError:
                continue
            self.ensure_canonical_id(cached)
            if self.apply_imdb_metadata(cached):
                imdb_rehydrated_cache_count += 1
            movies[cached.guide_rapide_id] = cached

        for film_id, url in sorted(discovered_urls.items(), reverse=True):
            cached = self.read_cached_movie(film_id)
            if cached and not self.should_recheck_movie(film_id):
                self.ensure_canonical_id(cached)
                if self.apply_imdb_metadata(cached):
                    imdb_rehydrated_cache_count += 1
                if (
                    backfilled_country_count < MAX_COUNTRY_BACKFILL_PER_RUN
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

            parsed = self.parse_movie(film_id, url, html)
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

    def release_info_text(self, movie: Movie) -> str:
        parts = []
        if movie.dvd_release_date:
            parts.append(f"DVD: {movie.dvd_release_date}")
        if movie.bluray_release_date:
            parts.append(f"Blu-ray: {movie.bluray_release_date}")
        if movie.year:
            parts.append(f"Production: {movie.year}")
        return " | ".join(parts)

    def to_meta_preview(self, movie: Movie) -> dict:
        preview = {
            "id": movie.id,
            "type": "movie",
            "name": movie.title,
            "poster": movie.poster,
            "releaseInfo": self.release_info_text(movie),
        }
        if movie.imdb_id:
            preview["imdb_id"] = movie.imdb_id
        if movie.released:
            preview["released"] = movie.released
        if movie.genres:
            preview["genres"] = movie.genres
        if movie.rating:
            preview["imdbRating"] = movie.rating
        if movie.synopsis:
            preview["description"] = movie.synopsis[:500]
        return preview

    def to_meta(self, movie: Movie) -> dict:
        description_parts = []
        if movie.synopsis:
            description_parts.append(movie.synopsis)
        if movie.release_text:
            description_parts.append(movie.release_text)

        meta = {
            "id": movie.id,
            "type": "movie",
            "name": movie.title,
            "poster": movie.poster,
            "background": movie.poster,
            "description": "\n\n".join(description_parts).strip(),
            "genres": movie.genres,
            "director": movie.director,
            "cast": movie.actors,
            "releaseInfo": self.release_info_text(movie),
            "country": " / ".join(movie.production_countries) if movie.production_countries else "",
            "language": "fr",
            "logo": "https://www.guide-rapide.com/IMG/divers/favicon.ico",
            "links": [{"name": "Guide-Rapide", "category": "source", "url": movie.source_url}],
        }

        if movie.released:
            meta["released"] = movie.released
        if movie.rating:
            meta["imdbRating"] = movie.rating
        if movie.runtime:
            meta["runtime"] = movie.runtime
        if movie.voters is not None:
            meta["votes"] = str(movie.voters)
        if movie.imdb_id:
            meta["imdb_id"] = movie.imdb_id
            meta["links"].append(
                {
                    "name": "IMDb",
                    "category": "imdb",
                    "url": f"https://www.imdb.com/title/{movie.imdb_id}/",
                }
            )
        if movie.trailer_url:
            meta["trailers"] = [{"source": "youtube", "type": "Trailer", "url": movie.trailer_url}]

        return meta

    def write_catalog(self, catalog_id: str, movies: list[Movie]) -> None:
        payload = {"metas": [self.to_meta_preview(m) for m in movies]}
        write_json(CATALOG_DIR / f"{catalog_id}.json", payload)

    def build_catalogs(self, movies: list[Movie]) -> tuple[list[dict], dict[str, list[Movie]]]:
        today = datetime.now(timezone.utc).date().isoformat()
        last_12_months_cutoff = subtract_months(datetime.now(timezone.utc).date(), 12).isoformat()

        physical_movies = [m for m in movies if m.physical_available]
        physical_past = [m for m in physical_movies if m.released and m.released <= today]
        physical_future = [m for m in physical_movies if m.released and m.released > today]

        physical_past.sort(key=lambda m: m.released, reverse=True)
        physical_future.sort(key=lambda m: m.released)

        catalog_defs = [
            {
                "type": "movie",
                "id": "dvd-12-mois-production-francaise",
                "name": "DVD 12 mois - Production francaise",
            },
            {
                "type": "movie",
                "id": "dvd-12-mois-international",
                "name": "DVD 12 mois - International",
            },
        ]

        dvd_recent = [
            m
            for m in physical_past
            if m.dvd_release_date and m.dvd_release_date >= last_12_months_cutoff
        ]
        dvd_recent.sort(key=lambda m: m.dvd_release_date, reverse=True)

        dvd_recent_fr = [m for m in dvd_recent if self.is_french_production(m)]
        dvd_recent_international = [m for m in dvd_recent if not self.is_french_production(m)]

        catalogs: dict[str, list[Movie]] = {
            "dvd-12-mois-production-francaise": dvd_recent_fr,
            "dvd-12-mois-international": dvd_recent_international,
        }

        if physical_future:
            catalog_defs.append(
                {"type": "movie", "id": "prochaines-sorties", "name": "Prochaines sorties"}
            )
            catalogs["prochaines-sorties"] = physical_future

        return catalog_defs, catalogs

    def clean_output_dirs(self) -> None:
        CATALOG_DIR.mkdir(parents=True, exist_ok=True)
        META_DIR.mkdir(parents=True, exist_ok=True)

        for old in CATALOG_DIR.glob("*.json"):
            old.unlink()
        for old in META_DIR.glob("*.json"):
            old.unlink()

    def write_manifest(self, catalogs: list[dict]) -> None:
        manifest = {
            "id": "org.guiderapide.nuvio",
            "version": datetime.now(timezone.utc).strftime("%Y.%m.%d"),
            "name": "Sortie DVD Tracker",
            "description": "Catalogues francais de sorties DVD et Blu-ray issus de Guide-Rapide.",
            "logo": "https://www.guide-rapide.com/IMG/divers/favicon.ico",
            "background": "https://www.guide-rapide.com/IMG/divers/favicon.ico",
            "resources": [
                "catalog",
                {"name": "meta", "types": ["movie"], "idPrefixes": ["tt", "gr-film-"]},
            ],
            "types": ["movie"],
            "catalogs": catalogs,
        }
        write_json(OUTPUT_DIR / "manifest.json", manifest)

    def write_index(self, total_movies: int, discovered_count: int) -> None:
        html = [
            "<!doctype html>",
            '<html lang="fr">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>Guide-Rapide Nuvio Catalog</title>",
            "<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;line-height:1.45}code{background:#f3f3f3;padding:2px 6px;border-radius:4px}</style>",
            "</head>",
            "<body>",
            "<h1>Guide-Rapide Nuvio Catalog</h1>",
            "<p>Manifest URL: <code>manifest.json</code></p>",
            f"<p>Films physiques indexes: <strong>{total_movies}</strong></p>",
            f"<p>Liens films decouverts pendant le crawl: <strong>{discovered_count}</strong></p>",
            "<ul>",
            '<li><a href="manifest.json">manifest.json</a></li>',
            '<li><a href="catalog/movie/dvd-12-mois-production-francaise.json">DVD 12 mois - Production francaise</a></li>',
            '<li><a href="catalog/movie/dvd-12-mois-international.json">DVD 12 mois - International</a></li>',
            "</ul>",
            "</body>",
            "</html>",
        ]
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / "index.html").write_text("\n".join(html), encoding="utf-8")

    def persist_state(self) -> None:
        self.state["last_run"] = datetime.now(timezone.utc).isoformat()
        write_json(STATE_FILE, self.state)
        self.write_imdb_cache()

    def build(self) -> None:
        log(f"[{self.elapsed()}] Build started")
        run_profile = self.resolve_run_profile()
        discovered_urls = self.discover_film_urls(run_profile)
        movies = self.load_movies(
            discovered_urls,
            max_movie_fetch_per_run=int(run_profile["max_movie_fetch_per_run"]),
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
        refreshed_posters = self.refresh_catalog_posters(catalogs)

        self.clean_output_dirs()
        for catalog_id, entries in catalogs.items():
            self.write_catalog(catalog_id, entries)

        for movie in physical_movies:
            write_json(META_DIR / f"{movie.id}.json", {"meta": self.to_meta(movie)})

        self.write_manifest(catalog_defs)
        self.write_index(total_movies=len(physical_movies), discovered_count=len(discovered_urls))
        self.persist_state()

        log(f"[{self.elapsed()}] Discovered movie links: {len(discovered_urls)}")
        log(f"[{self.elapsed()}] Physical movies exported: {len(physical_movies)}")
        log(f"[{self.elapsed()}] Catalogs exported: {len(catalog_defs)}")
        log(f"[{self.elapsed()}] Catalog posters refreshed from IMDb: {refreshed_posters}")


def main() -> int:
    builder = GuideRapideBuilder()
    builder.build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
